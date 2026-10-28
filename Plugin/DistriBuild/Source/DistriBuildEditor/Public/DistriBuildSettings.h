#pragma once

#include "CoreMinimal.h"
#include "Engine/DeveloperSettings.h"
#include "DistriBuildSettings.generated.h"

/**
 * Project Settings > Plugins > DistriBuild. Saved to DefaultEngine.ini so the whole team shares
 * the agent list; individual developers can still override with -DistriBuildAgents= / -NoDistriBuild.
 * Changes to agents apply immediately; whether shader compiling uses DistriBuild is decided at
 * editor startup.
 */
UCLASS(config = Engine, defaultconfig, meta = (DisplayName = "DistriBuild"))
class UDistriBuildSettings : public UDeveloperSettings
{
	GENERATED_BODY()

public:
	virtual FName GetCategoryName() const override { return TEXT("Plugins"); }

	/** Distribute shader compilation to DistriBuild agents. */
	UPROPERTY(config, EditAnywhere, Category = "General")
	bool bEnabled = true;

	/** Agents to use, as host or host:port (default port 31890). */
	UPROPERTY(config, EditAnywhere, Category = "Agents")
	TArray<FString> Agents;

	/** Find agents on the local subnet with a UDP broadcast. */
	UPROPERTY(config, EditAnywhere, Category = "Agents")
	bool bAutoDiscovery = true;

	UPROPERTY(config, EditAnywhere, Category = "Agents", meta = (ClampMin = "1", ClampMax = "65535"))
	int32 DiscoveryPort = 31891;

	/** Shared secret; must match the "token" in each agent's config. */
	UPROPERTY(config, EditAnywhere, Category = "Agents", meta = (PasswordField = true))
	FString AuthToken;

	/** Shader jobs in flight at once (remote + local fallback). Takes effect after an editor restart. */
	UPROPERTY(config, EditAnywhere, Category = "Scheduling", meta = (ClampMin = "1", ClampMax = "1024"))
	int32 MaxRemoteJobs = 128;

	/** Concurrent local ShaderCompileWorkers used when no agent is free. 0 = half the logical cores. */
	UPROPERTY(config, EditAnywhere, Category = "Scheduling", meta = (ClampMin = "0"))
	int32 LocalFallbackJobs = 0;

	/** Run jobs locally when no agent can take them or a remote run fails. */
	UPROPERTY(config, EditAnywhere, Category = "Scheduling")
	bool bFallbackToLocal = true;

	UPROPERTY(config, EditAnywhere, Category = "Scheduling", meta = (ClampMin = "0.2"))
	float ConnectTimeoutSeconds = 3.0f;

	UPROPERTY(config, EditAnywhere, Category = "Scheduling", meta = (ClampMin = "5"))
	float JobTimeoutSeconds = 300.0f;

	UPROPERTY(config, EditAnywhere, Category = "Scheduling", meta = (ClampMin = "1"))
	float RefreshIntervalSeconds = 10.0f;

	/** Engine key agents must offer (e.g. "5.4.4"). Empty = this editor's version. Set it for source builds whose agents register a custom key. */
	UPROPERTY(config, EditAnywhere, Category = "Advanced")
	FString EngineVersionOverride;

#if WITH_EDITOR
	virtual void PostEditChangeProperty(FPropertyChangedEvent& PropertyChangedEvent) override;
#endif
};
