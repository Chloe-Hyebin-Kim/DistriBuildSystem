#include "DistriBuildSourceBundle.h"

#include "DistriBuildConfig.h"
#include "Dom/JsonObject.h"
#include "HAL/FileManager.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Misc/SecureHash.h"
#include "Serialization/MemoryReader.h"

namespace
{
	static constexpr double ScanIntervalSeconds = 5.0;

	/** The head of a 4.26 job file, as written by FShaderCompileUtilities::DoWriteTasks. */
	bool ReadDirectoryMappings(const TArray<uint8>& JobData, TMap<FString, FString>& OutMappings)
	{
		FMemoryReader Reader(JobData);
		int32 InputVersion = 0;
		TMap<FString, uint32> FormatVersions;
		Reader << InputVersion;
		Reader << FormatVersions;
		Reader << OutMappings;
		return !Reader.IsError() && OutMappings.Num() > 0;
	}

	bool SameMappings(const TMap<FString, FString>& A, const TMap<FString, FString>& B)
	{
		if (A.Num() != B.Num())
		{
			return false;
		}
		for (const TPair<FString, FString>& Pair : A)
		{
			const FString* Other = B.Find(Pair.Key);
			if (!Other || *Other != Pair.Value)
			{
				return false;
			}
		}
		return true;
	}

	TArray<FString> SortedKeys(const TMap<FString, FString>& Mappings)
	{
		TArray<FString> Keys;
		Mappings.GetKeys(Keys);
		Keys.Sort();
		return Keys;
	}

	TArray<FString> ListFiles(const FString& Directory)
	{
		TArray<FString> Found;
		IFileManager::Get().FindFilesRecursive(Found, *Directory, TEXT("*"), true, false);
		Found.Sort();
		return Found;
	}

	void HashString(FSHA1& Sha, const FString& Text)
	{
		const FTCHARToUTF8 Utf8(*Text);
		Sha.Update(reinterpret_cast<const uint8*>(Utf8.Get()), Utf8.Length());
	}

	FString FinishHash(FSHA1& Sha)
	{
		FSHAHash Hash;
		Sha.Final();
		Sha.GetHash(Hash.Hash);
		return Hash.ToString();
	}

	/** Changes whenever a file is added, removed, resized or touched. */
	FString ScanSignature(const TMap<FString, FString>& Mappings)
	{
		IFileManager& FileManager = IFileManager::Get();
		FSHA1 Sha;
		for (const FString& Key : SortedKeys(Mappings))
		{
			HashString(Sha, Key);
			for (const FString& File : ListFiles(Mappings[Key]))
			{
				HashString(Sha, File);
				const int64 Size = FileManager.FileSize(*File);
				const int64 Time = FileManager.GetTimeStamp(*File).GetTicks();
				Sha.Update(reinterpret_cast<const uint8*>(&Size), sizeof(Size));
				Sha.Update(reinterpret_cast<const uint8*>(&Time), sizeof(Time));
			}
		}
		return FinishHash(Sha);
	}

	TSharedPtr<const FDistriBuildSourceBundle, ESPMode::ThreadSafe> BuildBundle(const TMap<FString, FString>& Mappings)
	{
		TSharedPtr<FDistriBuildSourceBundle, ESPMode::ThreadSafe> Bundle = MakeShared<FDistriBuildSourceBundle, ESPMode::ThreadSafe>();
		FSHA1 Sha;
		int64 TotalBytes = 0;
		for (const FString& Key : SortedKeys(Mappings))
		{
			const int32 Directory = Bundle->Directories.Num();
			Bundle->Directories.Add(Key, Directory);
			HashString(Sha, Key);

			const FString& Root = Mappings[Key];
			for (const FString& Path : ListFiles(Root))
			{
				FDistriBuildSourceBundle::FFile File;
				File.Directory = Directory;
				File.RelativePath = Path.Mid(Root.Len() + 1);
				if (!FFileHelper::LoadFileToArray(File.Data, *Path))
				{
					UE_LOG(LogDistriBuild, Warning, TEXT("Cannot read shader source %s"), *Path);
					return nullptr;
				}
				HashString(Sha, File.RelativePath);
				Sha.Update(File.Data.GetData(), File.Data.Num());
				TotalBytes += File.Data.Num();
				Bundle->Files.Add(MoveTemp(File));
			}
		}
		Bundle->Hash = FinishHash(Sha);
		UE_LOG(LogDistriBuild, Display, TEXT("DistriBuild: shader source bundle %s: %d directories, %d files, %lld KB"),
			*Bundle->Hash, Bundle->Directories.Num(), Bundle->Files.Num(), TotalBytes / 1024);
		return Bundle;
	}
}

void FDistriBuildSourceBundle::AddToRunFrame(FJsonObject& Run, bool bIncludeFiles, TArray<DistriBuildProtocol::FBlob>& Blobs) const
{
	Run.SetStringField(TEXT("source_bundle"), Hash);

	TSharedRef<FJsonObject> DirectoryObject = MakeShared<FJsonObject>();
	for (const TPair<FString, int32>& Pair : Directories)
	{
		DirectoryObject->SetNumberField(Pair.Key, Pair.Value);
	}
	Run.SetObjectField(TEXT("source_dirs"), DirectoryObject);

	if (!bIncludeFiles)
	{
		return;
	}
	TArray<TSharedPtr<FJsonValue>> Manifest;
	Manifest.Reserve(Files.Num());
	for (int32 Index = 0; Index < Files.Num(); ++Index)
	{
		const FFile& File = Files[Index];
		DistriBuildProtocol::FBlob& Blob = Blobs.AddDefaulted_GetRef();
		Blob.Name = FString::Printf(TEXT("src%06d"), Index);
		Blob.Data = File.Data;

		TSharedRef<FJsonObject> Entry = MakeShared<FJsonObject>();
		Entry->SetNumberField(TEXT("dir"), File.Directory);
		Entry->SetStringField(TEXT("path"), File.RelativePath);
		Entry->SetStringField(TEXT("blob"), Blob.Name);
		Manifest.Add(MakeShared<FJsonValueObject>(Entry));
	}
	Run.SetArrayField(TEXT("source_files"), Manifest);
}

TSharedPtr<const FDistriBuildSourceBundle, ESPMode::ThreadSafe> FDistriBuildSourceBundler::GetForJob(const TArray<uint8>& JobData)
{
	TMap<FString, FString> Mappings;
	if (!ReadDirectoryMappings(JobData, Mappings))
	{
		return nullptr;
	}
	for (TPair<FString, FString>& Pair : Mappings)
	{
		FPaths::NormalizeDirectoryName(Pair.Value);
	}

	FScopeLock Guard(&Lock);
	const bool bSameMappings = Current.IsValid() && SameMappings(Mappings, CurrentMappings);
	if (bSameMappings && FPlatformTime::Seconds() < NextScanTime)
	{
		return Current;
	}
	const FString Signature = ScanSignature(Mappings);
	if (!bSameMappings || Signature != CurrentSignature)
	{
		Current = BuildBundle(Mappings);
		CurrentMappings = MoveTemp(Mappings);
		CurrentSignature = Signature;
	}
	NextScanTime = FPlatformTime::Seconds() + ScanIntervalSeconds;
	return Current;
}
