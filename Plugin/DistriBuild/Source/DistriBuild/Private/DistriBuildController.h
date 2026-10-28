#pragma once

#include "CoreMinimal.h"
#include "DistributedBuildControllerInterface.h"
#include "DistriBuildAgentPool.h"
#include "DistriBuildConfig.h"
#include "DistriBuildModule.h"
#include <atomic>

class FQueuedThreadPool;

/**
 * Shader compiler back end. The engine's distributed shader compiler hands every batch of
 * ShaderCompileWorker jobs to the registered IDistributedBuildController (the same hook the
 * engine's XGE controller uses). Each job is shipped to an agent that has the same engine
 * version installed; the agent runs its own ShaderCompileWorker and returns the output file.
 * Jobs that cannot be placed remotely, or fail there, run on this machine.
 */
class FDistriBuildController : public IDistributedBuildController
{
public:
	FDistriBuildController(FDistriBuildAgentPool& InPool, const FDistriBuildConfig& InConfig);
	virtual ~FDistriBuildController() override;

	// IDistributedBuildController
	virtual bool SupportsDynamicReloading() override { return false; }
	virtual void InitializeController() override;
	virtual bool IsSupported() override;
	virtual const FString GetName() override { return TEXT("DistriBuild"); }
	virtual TFuture<FDistributedBuildTaskResult> EnqueueTask(const FTaskCommandData& CommandData) override;

	void ApplyConfig(const FDistriBuildConfig& InConfig);
	void Shutdown();

	bool IsActive() const { return bInitialized; }
	FString GetEngineKey() const;
	FDistriBuildStats GetStats() const;

	/** Runs on a worker thread. */
	FDistributedBuildTaskResult Execute(const FTaskCommandData& Command);

private:
	EDistriBuildOutcome RunRemote(const FDistriBuildAgent& Agent, const FTaskCommandData& Command,
		const FString& InputPath, const FString& OutputPath, const FDistriBuildConfig& Cfg);
	FDistributedBuildTaskResult RunLocal(const FTaskCommandData& Command, const FDistriBuildConfig& Cfg);
	FDistriBuildConfig GetConfig() const;

	FDistriBuildAgentPool& Pool;

	mutable FCriticalSection ConfigLock;
	FDistriBuildConfig Config;

	FQueuedThreadPool* Workers = nullptr;
	bool bInitialized = false;
	bool bSupportChecked = false;
	bool bSupported = false;

	FCriticalSection LocalLock;
	int32 LocalRunning = 0;

	std::atomic<int64> RemoteSucceeded { 0 };
	std::atomic<int64> RemoteFailed { 0 };
	std::atomic<int64> LocalExecuted { 0 };
	std::atomic<int64> BytesSent { 0 };
	std::atomic<int64> BytesReceived { 0 };
	std::atomic<int64> RemoteMilliseconds { 0 };
	std::atomic<int32> Queued { 0 };
	std::atomic<int32> Running { 0 };
};
