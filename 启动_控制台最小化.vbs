' Adaptive: derive project root from this script's location (vbs %~dp0); survives moving the project.
Set fso = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.Run "cmd /k chcp 65001 >nul && python main.py", 7, False