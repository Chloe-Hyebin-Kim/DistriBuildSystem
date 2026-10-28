using UnrealBuildTool;

public class DistriBuildEditor : ModuleRules
{
	public DistriBuildEditor(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;

		PrivateDependencyModuleNames.AddRange(new string[]
		{
			"Core",
			"CoreUObject",
			"Engine",
			"DeveloperSettings",
			"Slate",
			"SlateCore",
			"InputCore",
			"ToolMenus",
			"Settings",
			"EditorFramework",
			"UnrealEd",
			"WorkspaceMenuStructure",
			"DistriBuild",
		});
	}
}
