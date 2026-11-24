' Windowless MSI Pre-Uninstall Cleanup (Runs via wscript.exe //B with zero console popups)
On Error Resume Next

Dim fso, shell, installDir
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

installDir = fso.GetParentFolderName(WScript.ScriptFullName)

shell.Run "taskkill /F /IM EmployeeMonitoringAgent.exe", 0, True
shell.Run "taskkill /F /IM pythonw.exe", 0, True

If fso.FolderExists(installDir & "\runtime") Then
    fso.DeleteFolder installDir & "\runtime", True
End If
If fso.FolderExists(installDir & "\agent") Then
    fso.DeleteFolder installDir & "\agent", True
End If
