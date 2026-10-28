#include "DistriBuildConfig.h"

#include "HAL/PlatformMisc.h"
#include "Misc/CommandLine.h"
#include "Misc/ConfigCacheIni.h"
#include "Misc/EngineVersion.h"
#include "Misc/Parse.h"

DEFINE_LOG_CATEGORY(LogDistriBuild);

const TCHAR* FDistriBuildConfig::Section = TEXT("/Script/DistriBuildEditor.DistriBuildSettings");

void FDistriBuildConfig::Load()
{
	*this = FDistriBuildConfig();

	if (GConfig)
	{
		GConfig->GetBool(Section, TEXT("bEnabled"), bEnabled, GEngineIni);
		GConfig->GetArray(Section, TEXT("Agents"), Agents, GEngineIni);
		GConfig->GetBool(Section, TEXT("bAutoDiscovery"), bAutoDiscovery, GEngineIni);
		GConfig->GetInt(Section, TEXT("DiscoveryPort"), DiscoveryPort, GEngineIni);
		GConfig->GetInt(Section, TEXT("MaxRemoteJobs"), MaxRemoteJobs, GEngineIni);
		GConfig->GetInt(Section, TEXT("LocalFallbackJobs"), LocalFallbackJobs, GEngineIni);
		GConfig->GetFloat(Section, TEXT("ConnectTimeoutSeconds"), ConnectTimeoutSeconds, GEngineIni);
		GConfig->GetFloat(Section, TEXT("JobTimeoutSeconds"), JobTimeoutSeconds, GEngineIni);
		GConfig->GetFloat(Section, TEXT("RefreshIntervalSeconds"), RefreshIntervalSeconds, GEngineIni);
		GConfig->GetString(Section, TEXT("AuthToken"), AuthToken, GEngineIni);
		GConfig->GetBool(Section, TEXT("bFallbackToLocal"), bFallbackToLocal, GEngineIni);
		GConfig->GetString(Section, TEXT("EngineVersionOverride"), EngineVersionOverride, GEngineIni);
	}

	// Command line overrides: -DistriBuildAgents=host:port,host2  -NoDistriBuild  -DistriBuildToken=xyz
	FString AgentList;
	if (FParse::Value(FCommandLine::Get(), TEXT("-DistriBuildAgents="), AgentList, false))
	{
		AgentList.ParseIntoArray(Agents, TEXT(","), true);
	}
	FParse::Value(FCommandLine::Get(), TEXT("-DistriBuildToken="), AuthToken);
	if (FParse::Param(FCommandLine::Get(), TEXT("NoDistriBuild")))
	{
		bEnabled = false;
	}

	for (FString& Agent : Agents)
	{
		Agent.TrimStartAndEndInline();
	}
	Agents.RemoveAll([](const FString& A) { return A.IsEmpty(); });

	MaxRemoteJobs = FMath::Clamp(MaxRemoteJobs, 1, 1024);
	ConnectTimeoutSeconds = FMath::Max(0.2f, ConnectTimeoutSeconds);
	JobTimeoutSeconds = FMath::Max(5.0f, JobTimeoutSeconds);
	RefreshIntervalSeconds = FMath::Max(1.0f, RefreshIntervalSeconds);
}

FString FDistriBuildConfig::GetEngineKey() const
{
	if (!EngineVersionOverride.IsEmpty())
	{
		return EngineVersionOverride;
	}
	const FEngineVersion& V = FEngineVersion::Current();
	return FString::Printf(TEXT("%d.%d.%d"), V.GetMajor(), V.GetMinor(), V.GetPatch());
}

int32 FDistriBuildConfig::GetLocalFallbackJobs() const
{
	return LocalFallbackJobs > 0 ? LocalFallbackJobs : FMath::Max(1, FPlatformMisc::NumberOfCoresIncludingHyperthreads() / 2);
}
