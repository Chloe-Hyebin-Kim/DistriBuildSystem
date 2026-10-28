#include "DistriBuildModule.h"

#include "DistriBuildAgentPool.h"
#include "DistriBuildConfig.h"
#include "DistriBuildController.h"
#include "Features/IModularFeatures.h"
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
