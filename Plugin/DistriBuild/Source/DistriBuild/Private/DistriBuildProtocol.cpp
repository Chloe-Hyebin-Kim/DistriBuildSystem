#include "DistriBuildProtocol.h"

#include "DistriBuildConfig.h"
#include "Misc/Compression.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "Policies/CondensedJsonPrintPolicy.h"
#include "SocketSubsystem.h"
#include "Sockets.h"
#include "IPAddress.h"

namespace DistriBuildProtocol
{
	static constexpr int64 MaxHeaderBytes = 16ll * 1024 * 1024;
	static constexpr int64 MaxBlobBytes = 1024ll * 1024 * 1024;

	static void WriteU32(TArray<uint8>& Out, uint32 Value)
	{
		Out.Add(uint8(Value & 0xff));
		Out.Add(uint8((Value >> 8) & 0xff));
		Out.Add(uint8((Value >> 16) & 0xff));
		Out.Add(uint8((Value >> 24) & 0xff));
	}

	static uint32 ReadU32(const uint8* In)
	{
		return uint32(In[0]) | (uint32(In[1]) << 8) | (uint32(In[2]) << 16) | (uint32(In[3]) << 24);
	}

	static bool SendAll(FSocket* Socket, const uint8* Data, int64 Size)
	{
		while (Size > 0)
		{
			const int32 Chunk = int32(FMath::Min<int64>(Size, 1 << 20));
			int32 Sent = 0;
			if (!Socket->Send(Data, Chunk, Sent) || Sent <= 0)
			{
				return false;
			}
			Data += Sent;
			Size -= Sent;
		}
		return true;
	}

	static bool RecvAll(FSocket* Socket, uint8* Data, int64 Size, double Deadline)
	{
		while (Size > 0)
		{
			const double Remaining = Deadline - FPlatformTime::Seconds();
			if (Remaining <= 0.0 || !Socket->Wait(ESocketWaitConditions::WaitForRead, FTimespan::FromSeconds(Remaining)))
			{
				return false;
			}
			int32 Read = 0;
			if (!Socket->Recv(Data, int32(FMath::Min<int64>(Size, 1 << 20)), Read) || Read <= 0)
			{
				return false; // closed or error
			}
			Data += Read;
			Size -= Read;
		}
		return true;
	}

	FSocket* Connect(const FString& Host, int32 Port, float TimeoutSeconds)
	{
		ISocketSubsystem* Subsystem = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM);
		if (!Subsystem)
		{
			return nullptr;
		}

		FAddressInfoResult Resolved = Subsystem->GetAddressInfo(*Host, *FString::FromInt(Port),
			EAddressInfoFlags::Default, NAME_None, ESocketType::SOCKTYPE_Streaming);
		if (Resolved.ReturnCode != SE_NO_ERROR || Resolved.Results.Num() == 0)
		{
			return nullptr;
		}
		TSharedRef<FInternetAddr> Address = Resolved.Results[0].Address;
		Address->SetPort(Port);

		FSocket* Socket = Subsystem->CreateSocket(NAME_Stream, TEXT("DistriBuild"), Address->GetProtocolType());
		if (!Socket)
		{
			return nullptr;
		}
		Socket->SetNoDelay(true);
		Socket->SetNonBlocking(true);
		Socket->Connect(*Address); // EWOULDBLOCK expected; completion is checked below

		const bool bConnected = Socket->Wait(ESocketWaitConditions::WaitForWrite, FTimespan::FromSeconds(TimeoutSeconds))
			&& Socket->GetConnectionState() == SCS_Connected;
		if (!bConnected)
		{
			Close(Socket);
			return nullptr;
		}
		Socket->SetNonBlocking(false);
		return Socket;
	}

	void Close(FSocket*& Socket)
	{
		if (Socket)
		{
			Socket->Close();
			ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->DestroySocket(Socket);
			Socket = nullptr;
		}
	}

	bool SendFrame(FSocket* Socket, const TSharedRef<FJsonObject>& Header, const TArray<FBlob>& Blobs, int64* OutBytesOnWire)
	{
		TArray<TArray<uint8>> Packed;
		TArray<TSharedPtr<FJsonValue>> Files;
		Packed.SetNum(Blobs.Num());

		for (int32 Index = 0; Index < Blobs.Num(); ++Index)
		{
			const TArray<uint8>& In = Blobs[Index].Data;
			TArray<uint8>& Out = Packed[Index];
			bool bZlib = false;
			if (In.Num() > 256)
			{
				// zlib's compressBound() plus slack; avoids CompressMemoryBound, which newer engines deprecate.
				int32 OutSize = In.Num() + (In.Num() >> 12) + (In.Num() >> 14) + (In.Num() >> 25) + 64;
				Out.SetNumUninitialized(OutSize);
				if (FCompression::CompressMemory(NAME_Zlib, Out.GetData(), OutSize, In.GetData(), In.Num(), COMPRESS_BiasSpeed)
					&& OutSize < In.Num())
				{
					Out.SetNum(OutSize);
					bZlib = true;
				}
			}
			if (!bZlib)
			{
				Out = In;
			}

			TSharedRef<FJsonObject> File = MakeShared<FJsonObject>();
			File->SetStringField(TEXT("name"), Blobs[Index].Name);
			File->SetNumberField(TEXT("size"), In.Num());
			File->SetNumberField(TEXT("csize"), Out.Num());
			File->SetStringField(TEXT("codec"), bZlib ? TEXT("zlib") : TEXT("raw"));
			Files.Add(MakeShared<FJsonValueObject>(File));
		}
		Header->SetArrayField(TEXT("files"), Files);

		FString Json;
		TSharedRef<TJsonWriter<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>> Writer = TJsonWriterFactory<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>::Create(&Json);
		if (!FJsonSerializer::Serialize(Header, Writer))
		{
			return false;
		}
		FTCHARToUTF8 Utf8(*Json);

		TArray<uint8> Prefix;
		Prefix.Append(reinterpret_cast<const uint8*>("DBLD"), 4);
		WriteU32(Prefix, Version);
		WriteU32(Prefix, uint32(Utf8.Length()));
		Prefix.Append(reinterpret_cast<const uint8*>(Utf8.Get()), Utf8.Length());

		int64 Total = Prefix.Num();
		if (!SendAll(Socket, Prefix.GetData(), Prefix.Num()))
		{
			return false;
		}
		for (const TArray<uint8>& Data : Packed)
		{
			if (!SendAll(Socket, Data.GetData(), Data.Num()))
			{
				return false;
			}
			Total += Data.Num();
		}
		if (OutBytesOnWire)
		{
			*OutBytesOnWire += Total;
		}
		return true;
	}

	bool RecvFrame(FSocket* Socket, float TimeoutSeconds, TSharedPtr<FJsonObject>& OutHeader, TArray<FBlob>& OutBlobs, int64* OutBytesOnWire)
	{
		const double Deadline = FPlatformTime::Seconds() + TimeoutSeconds;
		uint8 Prefix[12];
		if (!RecvAll(Socket, Prefix, sizeof(Prefix), Deadline) || FMemory::Memcmp(Prefix, "DBLD", 4) != 0)
		{
			return false;
		}
		const uint32 FrameVersion = ReadU32(Prefix + 4);
		const uint32 HeaderLength = ReadU32(Prefix + 8);
		if (FrameVersion != Version || HeaderLength > MaxHeaderBytes)
		{
			UE_LOG(LogDistriBuild, Warning, TEXT("Agent protocol mismatch (version %u)"), FrameVersion);
			return false;
		}

		TArray<uint8> HeaderBytes;
		HeaderBytes.SetNumUninitialized(HeaderLength);
		if (!RecvAll(Socket, HeaderBytes.GetData(), HeaderLength, Deadline))
		{
			return false;
		}
		FUTF8ToTCHAR Converted(reinterpret_cast<const ANSICHAR*>(HeaderBytes.GetData()), HeaderBytes.Num());
		const FString Json(Converted.Length(), Converted.Get());
		TSharedRef<TJsonReader<TCHAR>> Reader = TJsonReaderFactory<TCHAR>::Create(Json);
		if (!FJsonSerializer::Deserialize(Reader, OutHeader) || !OutHeader.IsValid())
		{
			return false;
		}

		int64 Total = sizeof(Prefix) + HeaderLength;
		OutBlobs.Reset();
		const TArray<TSharedPtr<FJsonValue>>* Files = nullptr;
		if (OutHeader->TryGetArrayField(TEXT("files"), Files))
		{
			for (const TSharedPtr<FJsonValue>& Value : *Files)
			{
				const TSharedPtr<FJsonObject>* File = nullptr;
				if (!Value.IsValid() || !Value->TryGetObject(File))
				{
					return false;
				}
				const int64 Size = int64((*File)->GetNumberField(TEXT("size")));
				const int64 CSize = int64((*File)->GetNumberField(TEXT("csize")));
				if (Size < 0 || CSize < 0 || Size > MaxBlobBytes || CSize > MaxBlobBytes)
				{
					return false;
				}

				TArray<uint8> Packed;
				Packed.SetNumUninitialized(int32(CSize));
				if (!RecvAll(Socket, Packed.GetData(), CSize, Deadline))
				{
					return false;
				}
				Total += CSize;

				FBlob& Blob = OutBlobs.AddDefaulted_GetRef();
				Blob.Name = (*File)->GetStringField(TEXT("name"));
				if ((*File)->GetStringField(TEXT("codec")) == TEXT("zlib"))
				{
					Blob.Data.SetNumUninitialized(int32(Size));
					if (!FCompression::UncompressMemory(NAME_Zlib, Blob.Data.GetData(), int32(Size), Packed.GetData(), int32(CSize)))
					{
						return false;
					}
				}
				else
				{
					if (CSize != Size)
					{
						return false;
					}
					Blob.Data = MoveTemp(Packed);
				}
			}
		}
		if (OutBytesOnWire)
		{
			*OutBytesOnWire += Total;
		}
		return true;
	}

	bool ParseAddress(const FString& Text, FString& OutHost, int32& OutPort)
	{
		FString Trimmed = Text.TrimStartAndEnd();
		OutPort = DefaultPort;
		int32 Colon = INDEX_NONE;
		if (Trimmed.FindLastChar(TEXT(':'), Colon) && Trimmed.Find(TEXT(":")) == Colon)
		{
			OutHost = Trimmed.Left(Colon);
			OutPort = FCString::Atoi(*Trimmed.Mid(Colon + 1));
		}
		else
		{
			OutHost = Trimmed;
		}
		return !OutHost.IsEmpty() && OutPort > 0 && OutPort < 65536;
	}
}
