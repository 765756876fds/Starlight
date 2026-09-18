Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\1\Doubao\chats\2026-09-14\new-chat\lol-music-agent"
WshShell.Run "pythonw main.py", 0, False
