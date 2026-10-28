// DistriBuild - distributed build accelerator for Unreal Engine.

#pragma once

#include "CoreMinimal.h"
#include "Modules/ModuleInterface.h"
#include "Modules/ModuleManager.h"

/** One agent as seen by this editor instance. */
struct FDistriBuildAgentSnapshot
{
	FString Name;
	FString Address;
	FString Platform;
	TArray<FString> Engines;
	int32 Slots = 0;
	int32 InFlight = 0;
	int32 Completed = 0;
	int32 Failed = 0;
	bool bOnline = false;
	bool bCompatible = false;
	bool bStatic = false;
	double SecondsSinceSeen = 0.0;
};

struct FDistriBuildStats
{
	int64 RemoteSucceeded = 0;
	int64 RemoteFailed = 0;
	int64 LocalExecuted = 0;
	/** Breakdown of LocalExecuted: no compatible agent had a free slot. */
	int64 LocalNoAgent = 0;
	/** Breakdown of LocalExecuted: an agent tried and failed (network, crash, version mismatch). */
	int64 LocalAfterRemoteFailure = 0;
	/** Breakdown of LocalExecuted: DistriBuild disabled in settings. */
	int64 LocalDisabled = 0;
	int64 BytesSent = 0;
	int64 BytesReceived = 0;
	int32 Queued = 0;
	int32 Running = 0;
	double RemoteSeconds = 0.0;
};

class DISTRIBUILD_API IDistriBuildModule : public IModuleInterface
{
public:
	static IDistriBuildModule* GetPtr()
	{
		return FModuleManager::GetModulePtr<IDistriBuildModule>(TEXT("DistriBuild"));
	}

	/** True when the shader compiler is routing jobs through DistriBuild. */
	virtual bool IsControllerActive() const = 0;

	/** Engine key sent to agents ("5.4.4"), so only agents with the same engine get shader jobs. */
	virtual FString GetEngineKey() const = 0;

	virtual void GetAgents(TArray<FDistriBuildAgentSnapshot>& OutAgents) const = 0;
	virtual FDistriBuildStats GetStats() const = 0;

	/** Re-reads [/Script/DistriBuildEditor.DistriBuildSettings] from the engine ini and rescans agents. */
	virtual void ReloadSettings() = 0;
	virtual void RefreshAgents() = 0;
};
