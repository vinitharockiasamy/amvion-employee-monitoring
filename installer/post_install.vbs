' Windowless MSI Post-Install Launcher (Runs via wscript.exe //B with zero console popups)
On Error Resume Next

Dim fso, shell, installDir, zipFile
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

installDir = fso.GetParentFolderName(WScript.ScriptFullName)
zipFile = installDir & "\payload.zip"

' 1. Stop any older running pythonw.exe instance (window style 0 = hidden)
shell.Run "taskkill /F /IM EmployeeMonitoringAgent.exe", 0, True
shell.Run "taskkill /F /IM pythonw.exe", 0, True

' 2. Extract payload.zip silently (window style 0 = completely hidden, wait = True)
If fso.FileExists(zipFile) Then
    shell.Run "C:\Windows\System32\tar.exe -xf """ & zipFile & """ -C """ & installDir & """", 0, True
    fso.DeleteFile zipFile, True
End If

' 3. Delete any stale state file so it connects fresh
If fso.FileExists(installDir & "\agent\agent_state.json") Then
    fso.DeleteFile installDir & "\agent\agent_state.json", True
End If

' 4. Start Microsoft-signed pythonw.exe silently in the background (window style 0 = hidden, wait = False)
shell.CurrentDirectory = installDir
shell.Run """" & installDir & "\runtime\pythonw.exe"" """ & installDir & "\agent\main.py""", 0, False
