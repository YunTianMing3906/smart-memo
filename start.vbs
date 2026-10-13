Option Explicit
Dim shell, files, baseDir, scriptPath, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
baseDir = files.GetParentFolderName(WScript.ScriptFullName)
scriptPath = files.BuildPath(baseDir, "desktop_demo.py")
shell.CurrentDirectory = baseDir
command = "pyw.exe -3.12 " & Chr(34) & scriptPath & Chr(34)
On Error Resume Next
shell.Run command, 0, False
If Err.Number <> 0 Then
    MsgBox "Install Python 3.12 with the Python Launcher, then try again. You can also use start.cmd to see startup errors.", vbExclamation, "Smart Memo"
End If
