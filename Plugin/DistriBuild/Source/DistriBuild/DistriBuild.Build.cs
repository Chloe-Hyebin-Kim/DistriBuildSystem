using System.IO;
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
		});

		// A header-only module from 5.0 on; 4.26 ships the same header without any module rules.
		if (Target.Version.MajorVersion >= 5)
		{
			PrivateDependencyModuleNames.Add("DistributedBuildInterface");
		}
		else
		{
			PrivateIncludePaths.Add(Path.Combine(EngineDirectory, "Source", "Developer", "DistributedBuildInterface", "Public"));
		}
	}
}
