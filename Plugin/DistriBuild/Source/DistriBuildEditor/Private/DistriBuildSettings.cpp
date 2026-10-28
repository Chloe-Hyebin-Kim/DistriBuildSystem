#include "DistriBuildSettings.h"

#include "DistriBuildModule.h"

#if WITH_EDITOR
void UDistriBuildSettings::PostEditChangeProperty(FPropertyChangedEvent& PropertyChangedEvent)
{
	Super::PostEditChangeProperty(PropertyChangedEvent);
	SaveConfig();                // updates GConfig, which the runtime module reads
	TryUpdateDefaultConfigFile(); // persists to DefaultEngine.ini for the team
	if (IDistriBuildModule* Module = IDistriBuildModule::GetPtr())
	{
		Module->ReloadSettings();
	}
}
#endif
