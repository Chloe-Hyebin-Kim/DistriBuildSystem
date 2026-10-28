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
			"Slate",
			"SlateCore",
			"InputCore",
			"ToolMenus",
			"Settings",
			"UnrealEd",
			"WorkspaceMenuStructure",
			"DistriBuild",
		});

		int Major = Target.Version.MajorVersion;
		int Minor = Target.Version.MinorVersion;

		// UDeveloperSettings lives in its own module from 4.26 on.
		if (Major >= 5 || Minor >= 26)
		{
			PrivateDependencyModuleNames.Add("DeveloperSettings");
		}

		// FEditorStyle (EditorStyle module) before 5.1, FAppStyle (SlateCore) after.
		if (Major < 5 || (Major == 5 && Minor < 1))
		{
			PrivateDependencyModuleNames.Add("EditorStyle");
		}
	}
}
