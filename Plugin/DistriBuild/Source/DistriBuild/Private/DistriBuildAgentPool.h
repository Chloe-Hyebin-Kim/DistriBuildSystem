#pragma once

#include "CoreMinimal.h"
#include "HAL/Runnable.h"
#include "DistriBuildConfig.h"
#include "DistriBuildModule.h"

class FRunnableThread;
class FEvent;
class FJsonObject;

struct FDistriBuildAgent
{
	FString Host;
	int32 Port = 0;
	bool bStatic = false;

	FString Name;
	FString Platform;
	TArray<FString> Engines;
	int32 Slots = 0;
	int32 InFlight = 0;
	int32 Completed = 0;
	int32 Failed = 0;
	bool bOnline = false;
	double LastSeen = 0.0;
	double SkipUntil = 0.0;
	int32 ConsecutiveFailures = 0;
};

enum class EDistriBuildOutcome : uint8
{
	Success,
	Busy,          // agent had no free slot, try another
	NetworkError,  // could not talk to the agent
	JobFailed,     // agent ran the job but it failed
};

/** Keeps track of agents (static list + UDP discovery) and hands out slots. */
class FDistriBuildAgentPool : public FRunnable
{
public:
	FDistriBuildAgentPool();
	virtual ~FDistriBuildAgentPool() override;

	void Start(const FDistriBuildConfig& InConfig);
	void Shutdown();
	void ApplyConfig(const FDistriBuildConfig& InConfig);
	void RequestRefresh();
	/** Synchronous scan (static agents + discovery) on the calling thread. */
	void RefreshBlocking() { RefreshNow(); }

	/** Picks the least-loaded compatible agent and reserves one slot locally. */
	TSharedPtr<FDistriBuildAgent, ESPMode::ThreadSafe> Acquire(const FString& EngineKey);
	void Release(const TSharedPtr<FDistriBuildAgent, ESPMode::ThreadSafe>& Agent, EDistriBuildOutcome Outcome);

	void Snapshot(const FString& EngineKey, TArray<FDistriBuildAgentSnapshot>& Out) const;
	int32 CompatibleSlots(const FString& EngineKey) const;

	// FRunnable
	virtual uint32 Run() override;

private:
	void RefreshNow();
	bool QueryAgent(const FString& Host, int32 Port, TSharedPtr<FJsonObject>& OutInfo, const FDistriBuildConfig& Cfg);
	void Discover(const FDistriBuildConfig& Cfg, TArray<TPair<FString, TSharedPtr<FJsonObject>>>& OutReplies);
	void UpdateFromInfo(const FString& Host, const TSharedPtr<FJsonObject>& Info, bool bStatic);

	mutable FCriticalSection Lock;
	TArray<TSharedPtr<FDistriBuildAgent, ESPMode::ThreadSafe>> Agents;
	FDistriBuildConfig Config;

	FRunnableThread* Thread = nullptr;
	FEvent* WakeEvent = nullptr;
	TAtomic<bool> bStopping { false };
};
