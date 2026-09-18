Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\1\Doubao\chats\2026-09-14\new-chat\lol-music-agent"
WshShell.Run "cmd /k chcp 65001 >nul && python main.py", 7, False
