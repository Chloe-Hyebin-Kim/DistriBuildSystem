#!/usr/bin/env python3
"""Creates a small C++ project that includes the DistriBuild plugin, for checks with a real engine.

    python make_test_project.py <folder> <project name> 4.26|5.8

The module is not a unity build, so UBT produces one compile action per source file.
"""

import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.normpath(os.path.join(HERE, "..", "..", "Plugin", "DistriBuild"))
CLASSES = 6


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def main():
    root, name, version = sys.argv[1], sys.argv[2], sys.argv[3]
    ue5 = version.startswith("5")
    settings = ("DefaultBuildSettings = BuildSettingsVersion.Latest;\n\t\tIncludeOrderVersion = EngineIncludeOrderVersion.Latest;"
                if ue5 else "DefaultBuildSettings = BuildSettingsVersion.V2;")
    write(os.path.join(root, name + ".uproject"), json.dumps({
        "FileVersion": 3, "EngineAssociation": version,
        "Modules": [{"Name": name, "Type": "Runtime", "LoadingPhase": "Default"}],
        "Plugins": [{"Name": "DistriBuild", "Enabled": True}]}, indent=1))
    for suffix, kind in (("", "Game"), ("Editor", "Editor")):
        write(os.path.join(root, "Source", name + suffix + ".Target.cs"), """using UnrealBuildTool;
public class %(name)s%(suffix)sTarget : TargetRules
{
	public %(name)s%(suffix)sTarget(TargetInfo Target) : base(Target)
	{
		Type = TargetType.%(kind)s;
		%(settings)s
		ExtraModuleNames.Add("%(name)s");
	}
}
""" % {"name": name, "suffix": suffix, "kind": kind, "settings": settings})
    src = os.path.join(root, "Source", name)
    write(os.path.join(src, name + ".Build.cs"), """using UnrealBuildTool;
public class %(name)s : ModuleRules
{
	public %(name)s(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
		bUseUnity = false;
		PublicDependencyModuleNames.AddRange(new string[] { "Core", "CoreUObject", "Engine" });
	}
}
""" % {"name": name})
    write(os.path.join(src, name + ".cpp"), '#include "Modules/ModuleManager.h"\nIMPLEMENT_PRIMARY_GAME_MODULE(FDefaultGameModuleImpl, %s, "%s");\n' % (name, name))
    api = name.upper() + "_API"
    for i in range(CLASSES):
        write(os.path.join(src, "TestActor%d.h" % i), """#pragma once
#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "TestActor%(i)d.generated.h"

UCLASS()
class %(api)s ATestActor%(i)d : public AActor
{
	GENERATED_BODY()
public:
	virtual void Tick(float DeltaSeconds) override;
	UPROPERTY(EditAnywhere) float Speed = %(i)d.0f;
};
""" % {"i": i, "api": api})
        write(os.path.join(src, "TestActor%d.cpp" % i), """#include "TestActor%(i)d.h"
#include "Engine/World.h"

void ATestActor%(i)d::Tick(float DeltaSeconds)
{
	Super::Tick(DeltaSeconds);
	SetActorLocation(GetActorLocation() + FVector(Speed * DeltaSeconds, 0.0f, 0.0f));
}
""" % {"i": i})
    # A cache graph of its own: test runs must neither read nor fill the developer's real cache.
    write(os.path.join(root, "Config", "DefaultEngine.ini"), """[DistriBuildTestDDC]
MinimumDaysToKeepFile=7
Root=(Type=KeyLength, Length=120, Inner=AsyncPut)
AsyncPut=(Type=AsyncPut, Inner=Hierarchy)
Hierarchy=(Type=Hierarchical, Inner=Local)
Local=(Type=FileSystem, Path="%GAMEDIR%DerivedDataCache/Test")
""")
    target = os.path.join(root, "Plugins", "DistriBuild")
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(PLUGIN, target, ignore=shutil.ignore_patterns("Binaries", "Intermediate"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
