#pragma once

// Editor API differences between UE 4.26 and UE 5.x.

#include "Runtime/Launch/Resources/Version.h"

#if ENGINE_MAJOR_VERSION > 5 || (ENGINE_MAJOR_VERSION == 5 && ENGINE_MINOR_VERSION >= 1)
	#include "Styling/AppStyle.h"
	#define DISTRIBUILD_STYLE FAppStyle
	#define DISTRIBUILD_STYLE_SET_NAME FAppStyle::GetAppStyleSetName()
	#define DISTRIBUILD_ICON "Icons.Settings"
#else
	#include "EditorStyleSet.h"
	#define DISTRIBUILD_STYLE FEditorStyle
	#define DISTRIBUILD_STYLE_SET_NAME FEditorStyle::GetStyleSetName()
	#define DISTRIBUILD_ICON "EditorPreferences.TabIcon"
#endif

#if ENGINE_MAJOR_VERSION >= 5
	#define DISTRIBUILD_TOOLBAR_MENU "LevelEditor.LevelEditorToolBar.User"
#else
	#define DISTRIBUILD_TOOLBAR_MENU "LevelEditor.LevelEditorToolBar"
#endif
