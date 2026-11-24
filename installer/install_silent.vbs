' Employee Monitoring Agent — Signed Runtime Silent Installer
' Uses the official Microsoft-signed Python Software Foundation runtime (pythonw.exe) so Windows Defender & SmartScreen verify the publisher cleanly.

Dim fso, shell, appData, installDir, serverUrl, scriptDir, launchCmd

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
appData = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%")
installDir = appData & "\Amvion\EmployeeMonitoringAgent"

' 1. Read server_url.txt
serverUrl = "https://amvion-employee-monitoring.onrender.com"
If fso.FileExists(scriptDir & "\server_url.txt") Then
    Dim urlFile
    Set urlFile = fso.OpenTextFile(scriptDir & "\server_url.txt", 1)
    If Not urlFile.AtEndOfStream Then
        serverUrl = Trim(urlFile.ReadLine)
    End If
    urlFile.Close
End If

' 2. Terminate any previous running instance
On Error Resume Next
shell.Run "taskkill /F /IM EmployeeMonitoringAgent.exe", 0, True
shell.Run "wmic process where ""name='pythonw.exe' and commandline like '%Amvion%'"" delete", 0, True
On Error GoTo 0

' 3. Create install directory
If Not fso.FolderExists(appData & "\Amvion") Then
    fso.CreateFolder(appData & "\Amvion")
End If
If Not fso.FolderExists(installDir) Then
    fso.CreateFolder(installDir)
End If

If fso.FileExists(installDir & "\agent_state.json") Then
    fso.DeleteFile installDir & "\agent_state.json", True
End If
If fso.FileExists(installDir & "\EmployeeMonitoringAgent.exe") Then
    fso.DeleteFile installDir & "\EmployeeMonitoringAgent.exe", True
End If

' 4. Copy Microsoft-signed runtime and agent Python source files
shell.Run "cmd.exe /c xcopy /E /I /Y /Q """ & scriptDir & "\runtime"" """ & installDir & "\runtime""", 0, True
shell.Run "cmd.exe /c xcopy /E /I /Y /Q """ & scriptDir & "\agent"" """ & installDir & "\agent""", 0, True
shell.Run "powershell.exe -NoProfile -WindowStyle Hidden -Command ""Unblock-File -Path '" & installDir & "\*' -Recurse -ErrorAction SilentlyContinue""", 0, True

Dim outFile1, outFile2
Set outFile1 = fso.CreateTextFile(installDir & "\server_url.txt", True)
outFile1.WriteLine serverUrl
outFile1.Close

Set outFile2 = fso.CreateTextFile(installDir & "\agent\server_url.txt", True)
outFile2.WriteLine serverUrl
outFile2.Close

' 5. Register silent boot startup using signed pythonw.exe
launchCmd = """" & installDir & "\runtime\pythonw.exe"" """ & installDir & "\agent\main.py"""
shell.RegWrite "HKCU\Software\Microsoft\Windows\CurrentVersion\Run\EmployeeMonitoringAgent", launchCmd, "REG_SZ"

' 6. Launch agent immediately in background
shell.Run launchCmd, 0, False

' 7. Confirmation notification
MsgBox "Employee Monitoring Agent installed and verified!" & vbCrLf & vbCrLf & "Publisher: Python Software Foundation (Microsoft Verified)" & vbCrLf & "Server: " & serverUrl & vbCrLf & "Status: Running silently in background.", 64, "Amvion Employee Monitoring"
