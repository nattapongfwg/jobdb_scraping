' Launches run_webapp.cmd with NO visible window.
' Used as the action of the "JobDB Recruitment Board" scheduled task (see service.ps1).
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = root
' 0 = hidden window, True = wait so the task shows as "Running" while the server is up
sh.Run "cmd.exe /c """ & root & "\run_webapp.cmd""", 0, True
