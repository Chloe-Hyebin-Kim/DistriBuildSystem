#pragma once

#include "CoreMinimal.h"

DECLARE_LOG_CATEGORY_EXTERN(LogDistriBuild, Log, All);

/**
 * Plain config read straight from GConfig. The runtime module loads at PostConfigInit, before
 * UObjects exist, so it cannot use the UDeveloperSettings class that the editor module exposes;
 * both read and write the same ini section.
 */
struct FDistriBuildConfig
{
	static const TCHAR* Section;

	bool bEnabled = true;
	TArray<FString> Agents;
	bool bAutoDiscovery = true;
	int32 DiscoveryPort = 31891;
	int32 MaxRemoteJobs = 128;
	int32 LocalFallbackJobs = 0;
	float ConnectTimeoutSeconds = 3.0f;
	float JobTimeoutSeconds = 300.0f;
	float RefreshIntervalSeconds = 10.0f;
	FString AuthToken;
	bool bFallbackToLocal = true;
	FString EngineVersionOverride;

	void Load();
	FString GetEngineKey() const;
	int32 GetLocalFallbackJobs() const;
};
