; Read the same version as Python. No independent installer version constant.
#define StarlightVersionHandle FileOpen(AddBackslash(SourcePath) + "..\VERSION")
#if !StarlightVersionHandle
  #error VERSION is missing or unreadable.
#endif
#define MyAppVersion Trim(FileRead(StarlightVersionHandle))
#expr FileClose(StarlightVersionHandle)
#define StarlightDevPos Pos(".dev", MyAppVersion)
#define MyAppNumericVersion (StarlightDevPos ? Copy(MyAppVersion, 1, StarlightDevPos - 1) : MyAppVersion) + ".0"
