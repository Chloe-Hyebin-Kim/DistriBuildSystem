#include "DistriBuildSettings.h"

#include "DistriBuildModule.h"
#include "Runtime/Launch/Resources/Version.h"

#if WITH_EDITOR
void UDistriBuildSettings::PostEditChangeProperty(FPropertyChangedEvent& PropertyChangedEvent)
{
	Super::PostEditChangeProperty(PropertyChangedEvent);
	SaveConfig();                // updates GConfig, which the runtime module reads
#if ENGINE_MAJOR_VERSION >= 5
	TryUpdateDefaultConfigFile(); // persists to DefaultEngine.ini for the team
#else
	UpdateDefaultConfigFile();
#endif
	if (IDistriBuildModule* Module = IDistriBuildModule::GetPtr())
	{
		Module->ReloadSettings();
	}
}
#endif
