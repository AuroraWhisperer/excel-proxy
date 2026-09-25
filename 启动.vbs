Option Explicit
Dim files, shell, root, python, script, arguments
Set files = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
root = files.GetParentFolderName(WScript.ScriptFullName)
python = files.BuildPath(root, ".venv\Scripts\pythonw.exe")
script = files.BuildPath(root, "app\windows_launcher.py")
If Not files.FileExists(python) Then
    MsgBox "The Python environment is missing. See readme.md for setup instructions.", 16, "Excel Proxy"
    WScript.Quit 1
End If
arguments = " start"
If WScript.Arguments.Count > 0 Then
    If WScript.Arguments(0) = "--no-browser" Then arguments = arguments & " --no-browser"
End If
shell.CurrentDirectory = root
shell.Run """" & python & """ -B """ & script & """" & arguments, 0, False
