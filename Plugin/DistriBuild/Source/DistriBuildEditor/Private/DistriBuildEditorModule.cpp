#include "SDistriBuildMonitor.h"

#include "Framework/Application/SlateApplication.h"
#include "Framework/Docking/TabManager.h"
#include "Modules/ModuleManager.h"
#include "DistriBuildEditorCompat.h"
#include "ToolMenus.h"
#include "Widgets/Docking/SDockTab.h"
#include "WorkspaceMenuStructure.h"
#include "WorkspaceMenuStructureModule.h"

#define LOCTEXT_NAMESPACE "DistriBuildEditor"

static const FName DistriBuildTabName("DistriBuildMonitor");

class FDistriBuildEditorModule final : public IModuleInterface
{
public:
	virtual void StartupModule() override
	{
		FGlobalTabmanager::Get()->RegisterNomadTabSpawner(DistriBuildTabName,
			FOnSpawnTab::CreateLambda([](const FSpawnTabArgs&)
			{
				return SNew(SDockTab).TabRole(ETabRole::NomadTab)[SNew(SDistriBuildMonitor)];
			}))
			.SetDisplayName(LOCTEXT("TabTitle", "DistriBuild Monitor"))
			.SetTooltipText(LOCTEXT("TabTooltip", "Distributed build agents and statistics"))
			.SetGroup(WorkspaceMenu::GetMenuStructure().GetDeveloperToolsMiscCategory())
			.SetIcon(FSlateIcon(DISTRIBUILD_STYLE_SET_NAME, DISTRIBUILD_ICON));

		UToolMenus::RegisterStartupCallback(FSimpleMulticastDelegate::FDelegate::CreateRaw(this, &FDistriBuildEditorModule::RegisterMenus));
	}

	virtual void ShutdownModule() override
	{
		UToolMenus::UnRegisterStartupCallback(this);
		UToolMenus::UnregisterOwner(this);
		if (FSlateApplication::IsInitialized())
		{
			FGlobalTabmanager::Get()->UnregisterNomadTabSpawner(DistriBuildTabName);
		}
	}

private:
	void RegisterMenus()
	{
		FToolMenuOwnerScoped OwnerScoped(this);
		UToolMenu* Toolbar = UToolMenus::Get()->ExtendMenu(DISTRIBUILD_TOOLBAR_MENU);
		FToolMenuSection& Section = Toolbar->FindOrAddSection("DistriBuild");
		Section.AddEntry(FToolMenuEntry::InitToolBarButton(
			"DistriBuildMonitor",
			FUIAction(FExecuteAction::CreateLambda([]() { FGlobalTabmanager::Get()->TryInvokeTab(DistriBuildTabName); })),
			LOCTEXT("ToolbarLabel", "DistriBuild"),
			LOCTEXT("ToolbarTooltip", "Open the DistriBuild monitor"),
			FSlateIcon(DISTRIBUILD_STYLE_SET_NAME, DISTRIBUILD_ICON)));
	}
};

IMPLEMENT_MODULE(FDistriBuildEditorModule, DistriBuildEditor)

#undef LOCTEXT_NAMESPACE
