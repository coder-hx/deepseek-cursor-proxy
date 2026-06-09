Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "powershell.exe -NoExit -ExecutionPolicy Bypass -File ""C:\Users\ThinkPad\deepseek-cursor-proxy\start-proxy.ps1""", 1, False
