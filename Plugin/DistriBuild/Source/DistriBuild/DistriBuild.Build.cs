using UnrealBuildTool;

public class DistriBuild : ModuleRules
{
	public DistriBuild(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;

		PublicDependencyModuleNames.AddRange(new string[]
		{
			"Core",
		});

		PrivateDependencyModuleNames.AddRange(new string[]
		{
			"Sockets",
			"Networking",
			"Json",
			"DistributedBuildInterface",
		});
	}
}
