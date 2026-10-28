#include "DistriBuildModule.h"

#include "DistriBuildAgentPool.h"
#include "DistriBuildConfig.h"
#include "DistriBuildController.h"
#include "Features/IModularFeatures.h"
#include "HAL/IConsoleManager.h"
#include "Modules/ModuleManager.h"

class FDistriBuildModule final : public IDistriBuildModule
{
public:
	virtual void StartupModule() override
	{
		FModuleManager::Get().LoadModuleChecked(TEXT("Sockets"));
		FModuleManager::Get().LoadModuleChecked(TEXT("Networking"));

		Config.Load();
		Pool = MakeUnique<FDistriBuildAgentPool>();
		Controller = MakeUnique<FDistriBuildController>(*Pool, Config);
		if (Config.bEnabled)
		{
			Pool->Start(Config);
		}
		IModularFeatures::Get().RegisterModularFeature(IDistributedBuildController::GetModularFeatureType(), Controller.Get());
	}

	virtual void ShutdownModule() override
	{
		if (Controller)
		{
			IModularFeatures::Get().UnregisterModularFeature(IDistributedBuildController::GetModularFeatureType(), Controller.Get());
			Controller->Shutdown();
		}
		if (Pool)
		{
			Pool->Shutdown();
		}
		Controller.Reset();
		Pool.Reset();
	}

	virtual bool IsControllerActive() const override
	{
		return Controller && Controller->IsActive();
	}

	virtual FString GetEngineKey() const override
	{
		return Config.GetEngineKey();
	}

	virtual void GetAgents(TArray<FDistriBuildAgentSnapshot>& OutAgents) const override
	{
		Pool->Snapshot(Config.GetEngineKey(), OutAgents);
	}

	virtual FDistriBuildStats GetStats() const override
	{
		return Controller->GetStats();
	}

	virtual void ReloadSettings() override
	{
		Config.Load();
		Controller->ApplyConfig(Config);
		Pool->ApplyConfig(Config);
		if (Config.bEnabled)
		{
			Pool->Start(Config);
			Pool->RequestRefresh();
		}
		else
		{
			Pool->Shutdown();
		}
	}

	virtual void RefreshAgents() override
	{
		Pool->RequestRefresh();
	}

private:
	FDistriBuildConfig Config;
	TUniquePtr<FDistriBuildAgentPool> Pool;
	TUniquePtr<FDistriBuildController> Controller;
};

IMPLEMENT_MODULE(FDistriBuildModule, DistriBuild)

static FAutoConsoleCommand GDistriBuildStatsCommand(
	TEXT("DistriBuild.Stats"),
	TEXT("Prints DistriBuild shader job counters, including why jobs ran locally, and the agent list."),
	FConsoleCommandDelegate::CreateLambda([]()
	{
		IDistriBuildModule* Module = IDistriBuildModule::GetPtr();
		if (!Module)
		{
			return;
		}
		const FDistriBuildStats S = Module->GetStats();
		UE_LOG(LogDistriBuild, Display, TEXT("DistriBuild: controller %s, engine key %s"),
			Module->IsControllerActive() ? TEXT("ACTIVE") : TEXT("NOT ACTIVE (shaders compile with the engine's normal local workers)"),
			*Module->GetEngineKey());
		UE_LOG(LogDistriBuild, Display, TEXT("  remote ok %lld, remote failed attempts %lld, sent %lld KB, received %lld KB"),
			S.RemoteSucceeded, S.RemoteFailed, S.BytesSent / 1024, S.BytesReceived / 1024);
		UE_LOG(LogDistriBuild, Display, TEXT("  ran locally %lld = no free compatible agent %lld + after remote failure %lld + disabled %lld"),
			S.LocalExecuted, S.LocalNoAgent, S.LocalAfterRemoteFailure, S.LocalDisabled);

		TArray<FDistriBuildAgentSnapshot> Agents;
		Module->GetAgents(Agents);
		for (const FDistriBuildAgentSnapshot& A : Agents)
		{
			UE_LOG(LogDistriBuild, Display, TEXT("  agent %-20s %-22s %s slots %d busy %d done %d failed %d engines [%s]%s"),
				*A.Name, *A.Address, A.bOnline ? TEXT("online ") : TEXT("OFFLINE"), A.Slots, A.InFlight, A.Completed, A.Failed,
				*FString::Join(A.Engines, TEXT(", ")), A.bCompatible ? TEXT("") : TEXT("  <- engine version does not match"));
		}
	}));
