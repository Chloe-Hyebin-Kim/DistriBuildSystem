// UnrealBuildTool only uses its XGE executor when a Windows service named "Incredibuild Agent" is
// running (XGE.IsAvailable in UE 4.26 and 5.8). This service exists to satisfy that check so UBT
// starts xgConsole.exe, which is DistriBuild's. It does nothing else, and is built and installed
// by install_client.bat with the C# compiler that ships with Windows.

using System.ServiceProcess;

class DistriBuildXgeShim : ServiceBase
{
	DistriBuildXgeShim()
	{
		ServiceName = "Incredibuild Agent";
	}

	static void Main()
	{
		Run(new DistriBuildXgeShim());
	}
}
