#pragma once

#include "CoreMinimal.h"
#include "DistriBuildProtocol.h"

class FJsonObject;

/** Immutable copy of every shader source directory a 4.26 job refers to. */
struct FDistriBuildSourceBundle
{
	struct FFile
	{
		int32 Directory = 0;
		FString RelativePath;
		TArray<uint8> Data;
	};

	FString Hash;
	TMap<FString, int32> Directories; // virtual shader directory ("/Engine") -> index
	TArray<FFile> Files;

	/** Names the directories in a "run" frame; bIncludeFiles also ships the contents. */
	void AddToRunFrame(FJsonObject& Run, bool bIncludeFiles, TArray<DistriBuildProtocol::FBlob>& Blobs) const;
};

/**
 * UE 4.26 writes its shader directory mappings as absolute paths of this machine into every job
 * file, and ShaderCompileWorker reads .usf/.ush through them. An agent has neither those paths nor
 * necessarily the same files, so jobs ship with a bundle of the sources, and the agent points the
 * mappings at its cached copy. Shader edits are picked up by a periodic timestamp scan.
 */
class FDistriBuildSourceBundler
{
public:
	TSharedPtr<const FDistriBuildSourceBundle, ESPMode::ThreadSafe> GetForJob(const TArray<uint8>& JobData);

private:
	FCriticalSection Lock;
	TSharedPtr<const FDistriBuildSourceBundle, ESPMode::ThreadSafe> Current;
	TMap<FString, FString> CurrentMappings;
	FString CurrentSignature;
	double NextScanTime = 0.0;
};
