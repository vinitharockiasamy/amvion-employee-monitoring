[CmdletBinding()]
param(
    [string] $OutputPath,
    [switch] $Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ProductName = 'Employee Monitoring Standalone Agent (EMP-SA)'
$ProductVersion = '1.2.2'
$Manufacturer = 'Enterprise Monitoring Systems'
$UpgradeCode = '{B9F5C8E2-6D03-4F7B-8A4C-3E2F9B8D7C65}'
$ProductCode = '{A2604C56-7A6C-4E10-B8D9-3F0D2C1A965B}'

function New-DeterministicGuid([string] $Text) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [byte[]]$sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text))[0..15]
        $bytes[7] = ($bytes[7] -band 0x0F) -bor 0x50
        $bytes[8] = ($bytes[8] -band 0x3F) -bor 0x80
        return ([guid]::new($bytes)).ToString('B').ToUpperInvariant()
    } finally { $sha.Dispose() }
}

function Get-StableToken([string] $Text, [int] $Length = 24) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text))
        $hex = -join ($hash | ForEach-Object { $_.ToString('x2') })
        return $hex.Substring(0, $Length).ToUpperInvariant()
    } finally { $sha.Dispose() }
}

function Add-MsiTable($Database, [string] $Sql) {
    $view = $Database.OpenView($Sql)
    try { $view.Execute() }
    finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($view) }
}

function Add-MsiRow($Database, $Installer, [string] $Table, [object[]] $Values) {
    $view = $Database.OpenView("SELECT * FROM ``$Table``")
    $record = $null
    try {
        $record = $Installer.CreateRecord($Values.Count)
        for ($i = 0; $i -lt $Values.Count; $i++) {
            $field = $i + 1
            if ($null -eq $Values[$i]) { continue }
            elseif ($Values[$i] -is [int] -or $Values[$i] -is [long]) { $record.IntegerData($field) = [int]$Values[$i] }
            else { $record.StringData($field) = [string]$Values[$i] }
        }
        $view.Modify(1, $record)
    } finally {
        if ($null -ne $record) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($record) }
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($view)
    }
}

function Get-MsiQueryRows($Database, [string] $Sql) {
    $view = $Database.OpenView($Sql)
    $rows = [System.Collections.Generic.List[object]]::new()
    try {
        $null = $view.Execute()
        while ($true) {
            $row = $view.Fetch()
            if ($null -eq $row) { break }
            $fc = [int]$row.FieldCount()
            $values = [System.Collections.Generic.List[string]]::new()
            for ($i = 1; $i -le $fc; $i++) { $null = $values.Add([string]$row.StringData($i)) }
            $null = $rows.Add($values.ToArray())
            $null = [Runtime.InteropServices.Marshal]::FinalReleaseComObject($row)
        }
    } finally { $null = [Runtime.InteropServices.Marshal]::FinalReleaseComObject($view) }
    $result = New-Object PSObject -Property @{ Rows = $rows.ToArray() }
    return $result
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$installerRoot = Join-Path $projectRoot 'installer'
$agentRoot = Join-Path $installerRoot 'agent'
$runtimeRoot = Join-Path $installerRoot 'runtime'
if ([string]::IsNullOrWhiteSpace($OutputPath)) { $OutputPath = Join-Path $PSScriptRoot 'EMP-SA-enterprise-1.2.2.msi' }
$OutputPath = [IO.Path]::GetFullPath($OutputPath)

if (-not (Test-Path -LiteralPath $installerRoot -PathType Container)) { throw "installer/ was not found: $installerRoot" }
if (-not (Test-Path -LiteralPath (Join-Path $agentRoot 'main.py') -PathType Leaf)) { throw 'Required file installer/agent/main.py is missing.' }
if (-not (Test-Path -LiteralPath (Join-Path $runtimeRoot 'pythonw.exe') -PathType Leaf)) { throw 'Required file installer/runtime/pythonw.exe is missing.' }
if ($null -eq [Type]::GetTypeFromProgID('WindowsInstaller.Installer')) { throw 'WindowsInstaller.Installer COM is unavailable.' }
$makecab = Get-Command makecab.exe -ErrorAction SilentlyContinue
if ($null -eq $makecab) { $makecabPath = Join-Path $env:WINDIR 'System32\makecab.exe' }
else { $makecabPath = $makecab.Source }
if (-not (Test-Path -LiteralPath $makecabPath -PathType Leaf)) { throw 'makecab.exe was not found.' }
if ((Test-Path -LiteralPath $OutputPath) -and -not $Force) { throw "Output exists; use -Force to replace it: $OutputPath" }

# Reject obsolete DLP modules and references in the agent code before packaging.
foreach ($sourceFile in Get-ChildItem -LiteralPath $agentRoot -File -Recurse -Force -Filter '*.py') {
    if ($sourceFile.Name -match '(?i)^(fs_collector|dlp_realtime)\.py$') { throw "DLP module found: $($sourceFile.FullName)" }
    if ([IO.File]::ReadAllText($sourceFile.FullName) -match '(?i)\bfs_collector\b|\bdlp_realtime\b|\bdlp_(api|event|evidence|queue|sync)\b') {
        throw "DLP reference found in agent code: $($sourceFile.FullName)"
    }
}

$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('emp-sa-msi-' + [guid]::NewGuid().ToString('N'))
$stageRoot = Join-Path $tempRoot 'stage'
$cabRoot = Join-Path $tempRoot 'cab'
$tempMsi = Join-Path $tempRoot 'package.msi'
$installer = $null
$database = $null
$summary = $null
try {
    [void](New-Item -ItemType Directory -Path $stageRoot -Force)
    [void](New-Item -ItemType Directory -Path $cabRoot -Force)
    foreach ($name in @('agent', 'runtime')) {
        $source = Join-Path $installerRoot $name
        $destination = Join-Path $stageRoot $name
        [void](New-Item -ItemType Directory -Path $destination -Force)
        $prefix = $source.TrimEnd('\') + '\'
        foreach ($item in Get-ChildItem -LiteralPath $source -File -Recurse -Force) {
            $relative = $item.FullName.Substring($prefix.Length)
            if ($relative -match '(^|[\\/])__pycache__([\\/]|$)' -or $item.Extension -in @('.pyc', '.pyo', '.log')) { continue }
            if ($item.Name -in @('agent_state.json', 'agent.log')) { continue }
            $target = Join-Path $destination $relative
            [void](New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force)
            Copy-Item -LiteralPath $item.FullName -Destination $target
        }
    }
    $serverUrl = Join-Path $installerRoot 'server_url.txt'
    if (Test-Path -LiteralPath $serverUrl -PathType Leaf) { Copy-Item -LiteralPath $serverUrl -Destination (Join-Path $stageRoot 'server_url.txt') }

    # This embedded custom-action script is available from the MSI Binary table before InstallFiles.
    # It only terminates the matching agent process; it never deletes files or directories.
    $installStopVbs = @'
Function StopAgent()
  On Error Resume Next
  Dim fso, wmi, processes, proc, installDir, wantedExe, wantedMain, exePath, cmdLine
  Set fso = CreateObject("Scripting.FileSystemObject")
  installDir = Session.Property("INSTALLLOCATION")
  If Len(installDir) = 0 Then
    StopAgent = 1
    Exit Function
  End If
  wantedExe = LCase(fso.GetAbsolutePathName(installDir & "\runtime\pythonw.exe"))
  wantedMain = LCase(fso.GetAbsolutePathName(installDir & "\agent\main.py"))
  Set wmi = GetObject("winmgmts:\\.\root\cimv2")
  Set processes = wmi.ExecQuery("SELECT ProcessId, ExecutablePath, CommandLine FROM Win32_Process WHERE Name='pythonw.exe'")
  For Each proc In processes
    exePath = ""
    cmdLine = ""
    If Not IsNull(proc.ExecutablePath) Then exePath = LCase(fso.GetAbsolutePathName(proc.ExecutablePath))
    If Not IsNull(proc.CommandLine) Then cmdLine = LCase(proc.CommandLine)
    If exePath = wantedExe And InStr(1, cmdLine, wantedMain, 1) > 0 Then proc.Terminate 0
  Next
  StopAgent = 1
End Function
'@
    $stopAgentPath = Join-Path $tempRoot 'stop_agent.vbs'
    [IO.File]::WriteAllText($stopAgentPath, $installStopVbs, [Text.Encoding]::ASCII)

    # This separate helper remains uninstall-only. MSI removes the files it owns.
    $uninstallVbs = @'
Option Explicit
Dim fso, wmi, processes, proc, installDir, wantedExe, wantedMain, exePath, cmdLine
Set fso = CreateObject("Scripting.FileSystemObject")
installDir = fso.GetAbsolutePathName(WScript.Arguments(0))
wantedExe = LCase(fso.GetAbsolutePathName(installDir & "\runtime\pythonw.exe"))
wantedMain = LCase(fso.GetAbsolutePathName(installDir & "\agent\main.py"))
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set processes = wmi.ExecQuery("SELECT ProcessId, ExecutablePath, CommandLine FROM Win32_Process WHERE Name='pythonw.exe'")
For Each proc In processes
  exePath = ""
  cmdLine = ""
  If Not IsNull(proc.ExecutablePath) Then exePath = LCase(fso.GetAbsolutePathName(proc.ExecutablePath))
  If Not IsNull(proc.CommandLine) Then cmdLine = LCase(proc.CommandLine)
  If exePath = wantedExe And InStr(1, cmdLine, wantedMain, 1) > 0 Then
    On Error Resume Next
    proc.Terminate 0
    On Error GoTo 0
  End If
Next
'@
    $startVbs = @'
Option Explicit
Dim fso, shell, installDir, pythonw, mainScript, commandLine
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
installDir = fso.GetAbsolutePathName(WScript.Arguments(0))
pythonw = installDir & "\runtime\pythonw.exe"
mainScript = installDir & "\agent\main.py"
If Not fso.FileExists(pythonw) Then WScript.Quit 2
If Not fso.FileExists(mainScript) Then WScript.Quit 3
shell.CurrentDirectory = installDir
commandLine = Chr(34) & pythonw & Chr(34) & " " & Chr(34) & mainScript & Chr(34)
shell.Run commandLine, 0, False
'@
    [IO.File]::WriteAllText((Join-Path $stageRoot 'pre_uninstall.vbs'), $uninstallVbs, [Text.Encoding]::ASCII)
    [IO.File]::WriteAllText((Join-Path $stageRoot 'post_install.vbs'), $startVbs, [Text.Encoding]::ASCII)

    $files = @(Get-ChildItem -LiteralPath $stageRoot -File -Recurse -Force | Sort-Object FullName)
    if ($files.Count -eq 0 -or $files.Count -gt 32767) { throw "Invalid MSI file count: $($files.Count) (must be 1..32767)." }
    foreach ($file in $files) {
        if ($file.Name -match '(?i)^(fs_collector|dlp_realtime)\.py$') { throw "DLP module found in staged payload: $($file.FullName)" }
    }
    $stagePrefix = $stageRoot.TrimEnd('\') + '\'
    $dirPaths = [System.Collections.Generic.SortedSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($file in $files) { [void]$dirPaths.Add((Split-Path -Parent $file.FullName.Substring($stagePrefix.Length))) }
    [void]$dirPaths.Remove('')
    $dirIds = @{}
    $dirParents = @{}
    $dirDefault = @{}
    $dirRelativeById = @{}
    $dirIndex = 0
    foreach ($path in ($dirPaths | Sort-Object { ($_ -split '\\').Count }, { $_ })) {
        $dirIndex++
        $parts = $path -split '\\'
        $parent = if ($parts.Count -gt 1) { $parts[0..($parts.Count - 2)] -join '\' } else { '' }
        $dirIds[$path] = 'DIR_{0:D6}' -f $dirIndex
        $dirRelativeById[$dirIds[$path]] = $path
        $dirParents[$path] = if ($parent) { $dirIds[$parent] } else { 'INSTALLLOCATION' }
        $dirDefault[$path] = ('D{0:D6}|{1}' -f $dirIndex, $parts[-1])
        if ($parts[-1] -match '[|\x00-\x1F]') { throw "Unsupported folder name: $path" }
    }

    $fileRows = [System.Collections.Generic.List[object]]::new()
    $components = @{}
    $ddfPath = Join-Path $cabRoot 'payload.ddf'
    $cabPath = Join-Path $cabRoot 'payload.cab'
    $ddf = [System.Collections.Generic.List[string]]::new()
    $ddf.Add('.Set Cabinet=ON')
    $ddf.Add('.Set Compress=ON')
    $ddf.Add('.Set CompressionType=MSZIP')
    $ddf.Add('.Set CabinetNameTemplate=payload.cab')
    $ddf.Add('.Set DiskDirectoryTemplate=' + $cabRoot)
    $ddf.Add('.Set InfFileName=' + (Join-Path $cabRoot 'payload.inf'))
    $ddf.Add('.Set RptFileName=' + (Join-Path $cabRoot 'payload.rpt'))
    $ddf.Add('.Set MaxDiskSize=0')
    $index = 0
    foreach ($file in $files) {
        $index++
        $relative = $file.FullName.Substring($stagePrefix.Length).Replace('/', '\')
        $relativeDir = Split-Path -Parent $relative
        $dirId = if ($relativeDir) { $dirIds[$relativeDir] } else { 'INSTALLLOCATION' }
        if (-not $components.ContainsKey($dirId)) { $components[$dirId] = 'CMP_{0:D6}' -f $components.Count }
        $shortName = 'F{0:D6}.DAT' -f $index
        $key = 'FILE_' + (Get-StableToken $relative)
        $ddf.Add('"' + $file.FullName + '" ' + $key)
        $fileRows.Add([pscustomobject]@{ Key=$key; Component=$components[$dirId]; Directory=$dirId; Short=$shortName; Long=$file.Name; Size=[int]$file.Length; Sequence=$index })
    }
    [IO.File]::WriteAllLines($ddfPath, $ddf, [Text.Encoding]::ASCII)
    Push-Location $cabRoot
    try {
        $makecabOutput = & $makecabPath '/F' $ddfPath 2>&1
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $cabPath -PathType Leaf)) { throw "makecab failed: $($makecabOutput -join ' ')" }
    } finally { Pop-Location }

    $installer = New-Object -ComObject WindowsInstaller.Installer
    $database = $installer.OpenDatabase($tempMsi, 3)
    $tables = @(
        'CREATE TABLE `Property` (`Property` CHAR(72) NOT NULL, `Value` LONGCHAR NOT NULL PRIMARY KEY `Property`)',
        'CREATE TABLE `Directory` (`Directory` CHAR(72) NOT NULL, `Directory_Parent` CHAR(72), `DefaultDir` CHAR(255) NOT NULL PRIMARY KEY `Directory`)',
        'CREATE TABLE `Feature` (`Feature` CHAR(38) NOT NULL, `Feature_Parent` CHAR(38), `Title` CHAR(64) LOCALIZABLE, `Description` CHAR(255) LOCALIZABLE, `Display` SHORT, `Level` SHORT NOT NULL, `Directory_` CHAR(72) NOT NULL, `Attributes` SHORT NOT NULL PRIMARY KEY `Feature`)',
        'CREATE TABLE `Component` (`Component` CHAR(72) NOT NULL, `ComponentId` CHAR(38), `Directory_` CHAR(72) NOT NULL, `Attributes` SHORT NOT NULL, `Condition` CHAR(255), `KeyPath` CHAR(72) PRIMARY KEY `Component`)',
        'CREATE TABLE `FeatureComponents` (`Feature_` CHAR(38) NOT NULL, `Component_` CHAR(72) NOT NULL PRIMARY KEY `Feature_`, `Component_`)',
        'CREATE TABLE `File` (`File` CHAR(72) NOT NULL, `Component_` CHAR(72) NOT NULL, `FileName` CHAR(255) NOT NULL, `FileSize` LONG NOT NULL, `Version` CHAR(72), `Language` CHAR(20), `Attributes` SHORT NOT NULL, `Sequence` SHORT NOT NULL PRIMARY KEY `File`)',
        'CREATE TABLE `Media` (`DiskId` SHORT NOT NULL, `LastSequence` SHORT NOT NULL, `DiskPrompt` CHAR(64) LOCALIZABLE, `Cabinet` CHAR(255), `VolumeLabel` CHAR(32), `Source` CHAR(72) PRIMARY KEY `DiskId`)',
        'CREATE TABLE `Registry` (`Registry` CHAR(72) NOT NULL, `Root` SHORT NOT NULL, `Key` CHAR(255) NOT NULL, `Name` CHAR(255), `Value` LONGCHAR, `Component_` CHAR(72) NOT NULL PRIMARY KEY `Registry`)',
        'CREATE TABLE `CustomAction` (`Action` CHAR(72) NOT NULL, `Type` SHORT NOT NULL, `Source` CHAR(72), `Target` LONGCHAR PRIMARY KEY `Action`)',
        'CREATE TABLE `Binary` (`Name` CHAR(72) NOT NULL, `Data` OBJECT PRIMARY KEY `Name`)',
        'CREATE TABLE `Upgrade` (`UpgradeCode` CHAR(38) NOT NULL, `VersionMin` CHAR(20), `VersionMax` CHAR(20), `Language` CHAR(255), `Attributes` LONG NOT NULL, `Remove` CHAR(255), `ActionProperty` CHAR(72) NOT NULL PRIMARY KEY `UpgradeCode`, `VersionMin`, `VersionMax`, `Language`, `Attributes`)',
        'CREATE TABLE `LaunchCondition` (`Condition` CHAR(255) NOT NULL, `Description` CHAR(255) NOT NULL LOCALIZABLE PRIMARY KEY `Condition`)',
        'CREATE TABLE `InstallExecuteSequence` (`Action` CHAR(72) NOT NULL, `Condition` CHAR(255), `Sequence` SHORT PRIMARY KEY `Action`)',
        'CREATE TABLE `InstallUISequence` (`Action` CHAR(72) NOT NULL, `Condition` CHAR(255), `Sequence` SHORT PRIMARY KEY `Action`)'
    )
    foreach ($sql in $tables) { Add-MsiTable $database $sql }

    foreach ($entry in @(
        @('ProductName',$ProductName), @('ProductVersion',$ProductVersion), @('Manufacturer',$Manufacturer),
        @('ProductCode',$ProductCode), @('UpgradeCode',$UpgradeCode), @('ProductLanguage','1033'),
        @('ALLUSERS','2'), @('MSIINSTALLPERUSER','1'), @('INSTALLLEVEL','1'),
        @('SecureCustomProperties','OLDPRODUCTS;NEWERPRODUCTFOUND')
    )) { Add-MsiRow $database $installer 'Property' $entry }
    # Find and remove older related EMP-SA products (1.1.0..1.2.1); detect, but never remove, this or newer versions.
    Add-MsiRow $database $installer 'Upgrade' @($UpgradeCode,'1.1.0','1.2.2',$null,0,$null,'OLDPRODUCTS')
    Add-MsiRow $database $installer 'Upgrade' @($UpgradeCode,'1.2.2',$null,$null,3,$null,'NEWERPRODUCTFOUND')
    Add-MsiRow $database $installer 'LaunchCondition' @('NOT NEWERPRODUCTFOUND','A newer or same version of EMP-SA is already installed.')
    Add-MsiRow $database $installer 'Directory' @('TARGETDIR',$null,'SourceDir')
    Add-MsiRow $database $installer 'Directory' @('LocalAppDataFolder','TARGETDIR','.')
    Add-MsiRow $database $installer 'Directory' @('EMPLOYEE_MONITORING_DIR','LocalAppDataFolder','EMPLOYEE|EmployeeMonitoring')
    Add-MsiRow $database $installer 'Directory' @('INSTALLLOCATION','EMPLOYEE_MONITORING_DIR','Agent')
    foreach ($path in ($dirPaths | Sort-Object { ($_ -split '\\').Count }, { $_ })) {
        Add-MsiRow $database $installer 'Directory' @($dirIds[$path],$dirParents[$path],$dirDefault[$path])
    }
    Add-MsiRow $database $installer 'Feature' @('MainFeature',$null,'Employee Monitoring Agent','Live Monitoring agent and bundled runtime',1,1,'INSTALLLOCATION',0)

    foreach ($dirId in $components.Keys) {
        $component = $components[$dirId]
        $componentFiles = @($fileRows | Where-Object { $_.Component -eq $component })
        $stableDirectory = if ($dirId -eq 'INSTALLLOCATION') { '.' } else { $dirRelativeById[$dirId] }
        $componentGuid = New-DeterministicGuid "EMP-SA:component:$stableDirectory"
        Add-MsiRow $database $installer 'Component' @($component,$componentGuid,$dirId,256,$null,$componentFiles[0].Key)
        Add-MsiRow $database $installer 'FeatureComponents' @('MainFeature',$component)
    }
    foreach ($row in $fileRows) {
        Add-MsiRow $database $installer 'File' @($row.Key,$row.Component,($row.Short + '|' + $row.Long),$row.Size,$null,$null,512,$row.Sequence)
    }
    Add-MsiRow $database $installer 'Component' @('RunRegistryComponent',(New-DeterministicGuid 'EMP-SA:HKCU:Run'),'INSTALLLOCATION',4,$null,'RunAgentValue')
    Add-MsiRow $database $installer 'FeatureComponents' @('MainFeature','RunRegistryComponent')
    $runCommand = '"[INSTALLLOCATION]runtime\pythonw.exe" "[INSTALLLOCATION]agent\main.py"'
    Add-MsiRow $database $installer 'Registry' @('RunAgentValue',1,'Software\Microsoft\Windows\CurrentVersion\Run','EmployeeMonitoringAgent',$runCommand,'RunRegistryComponent')
    Add-MsiRow $database $installer 'Media' @(1,$fileRows.Count,'EMP-SA installation media','#payload.cab','EMP-SA',$null)

    $streamView = $database.OpenView('SELECT * FROM `_Streams`')
    $streamRecord = $installer.CreateRecord(2)
    try {
        $streamRecord.StringData(1) = 'payload.cab'
        $streamRecord.SetStream(2,$cabPath)
        $streamView.Modify(1,$streamRecord)
    } finally {
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($streamRecord)
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($streamView)
    }

    $binaryView = $database.OpenView('SELECT * FROM `Binary`')
    $binaryRecord = $installer.CreateRecord(2)
    try {
        $binaryRecord.StringData(1) = 'stop_agent.vbs'
        $binaryRecord.SetStream(2,$stopAgentPath)
        $binaryView.Modify(1,$binaryRecord)
    } finally {
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($binaryRecord)
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($binaryView)
    }

    $vbs = '"[SystemFolder]wscript.exe" //B //NoLogo "[INSTALLLOCATION]{0}" "[INSTALLLOCATION]"'
    Add-MsiRow $database $installer 'CustomAction' @('StopAgentBeforeRemove',34,'INSTALLLOCATION',($vbs -f 'pre_uninstall.vbs'))
    Add-MsiRow $database $installer 'CustomAction' @('StopAgentBeforeInstall',6,'stop_agent.vbs','StopAgent')
    Add-MsiRow $database $installer 'CustomAction' @('StartAgentAfterInstall',34,'INSTALLLOCATION',($vbs -f 'post_install.vbs'))
    foreach ($row in @(
        @('FindRelatedProducts',$null,200), @('LaunchConditions',$null,250), @('MigrateFeatureStates','OLDPRODUCTS',300),
        @('CostInitialize',$null,800), @('FileCost',$null,900), @('CostFinalize',$null,1000),
        @('InstallValidate',$null,1400), @('InstallInitialize',$null,1500),
        @('StopAgentBeforeInstall','NOT REMOVE="ALL"',1510), @('RemoveExistingProducts','OLDPRODUCTS',1520),
        @('ProcessComponents',$null,1600),
        @('UnpublishFeatures','REMOVE~="ALL"',1800), @('RemoveRegistryValues',$null,2600),
        @('StopAgentBeforeRemove','REMOVE="ALL"',3100),
        @('RemoveFiles',$null,3500), @('InstallFiles',$null,4000), @('WriteRegistryValues',$null,5000),
        @('RegisterUser',$null,6000), @('RegisterProduct',$null,6100), @('PublishFeatures',$null,6300),
        @('PublishProduct',$null,6400), @('InstallFinalize',$null,6600), @('StartAgentAfterInstall','NOT REMOVE="ALL"',6650)
    )) { Add-MsiRow $database $installer 'InstallExecuteSequence' $row }
    foreach ($row in @(@('FindRelatedProducts',$null,25),@('LaunchConditions',$null,100),@('CostInitialize',$null,800),@('FileCost',$null,900),@('CostFinalize',$null,1000),@('ExecuteAction',$null,1300))) {
        Add-MsiRow $database $installer 'InstallUISequence' $row
    }

    $summary = $database.SummaryInformation(20)
    $summary.Property(2) = 'Installation Database'
    $summary.Property(3) = $ProductName
    $summary.Property(4) = $Manufacturer
    $summary.Property(7) = 'x64;1033'
    $cabHash = (Get-FileHash -LiteralPath $cabPath -Algorithm SHA256).Hash
    $buildScriptHash = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash
    $summary.Property(9) = (New-DeterministicGuid ($cabHash + ':' + $buildScriptHash))
    $summary.Property(14) = 200
    $summary.Property(15) = 2
    $summary.Persist()
    $database.Commit()
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($summary); $summary = $null
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($database); $database = $null
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($installer); $installer = $null

    $installer = New-Object -ComObject WindowsInstaller.Installer
    $database = $installer.OpenDatabase($tempMsi,0)
    $fileCheck = (Get-MsiQueryRows $database 'SELECT `File` FROM `File`').Rows
    $featureCheck = (Get-MsiQueryRows $database 'SELECT `Feature` FROM `Feature`').Rows
    $componentCheck = (Get-MsiQueryRows $database 'SELECT `Component` FROM `Component`').Rows
    if ($fileCheck.Count -ne $fileRows.Count) { throw 'File table validation failed.' }
    if ($featureCheck.Count -ne 1) { throw 'Feature table validation failed.' }
    if ($componentCheck.Count -lt 1) { throw 'Component table validation failed.' }
    $mediaRows = (Get-MsiQueryRows $database 'SELECT `Cabinet` FROM `Media`').Rows
    $streamRows = (Get-MsiQueryRows $database "SELECT `Name` FROM `_Streams` WHERE `Name`='payload.cab'").Rows
    if ($mediaRows.Count -ne 1 -or $mediaRows[0][0] -ne '#payload.cab' -or $streamRows.Count -ne 1) { throw 'Embedded cabinet validation failed.' }
    $registryRows = (Get-MsiQueryRows $database 'SELECT `Value` FROM `Registry` WHERE `Registry`=''RunAgentValue''').Rows
    if ($registryRows.Count -ne 1 -or $registryRows[0][0] -ne $runCommand) { throw 'HKCU Run command validation failed.' }
    $binaryRows = (Get-MsiQueryRows $database "SELECT `Name` FROM `Binary` WHERE `Name`='stop_agent.vbs'").Rows
    if ($binaryRows.Count -ne 1) { throw 'Embedded stop_agent.vbs validation failed.' }
    $stopActionRows = (Get-MsiQueryRows $database "SELECT `Source`,`Target` FROM `CustomAction` WHERE `Action`='StopAgentBeforeInstall'").Rows
    if ($stopActionRows.Count -ne 1 -or $stopActionRows[0][0] -ne 'stop_agent.vbs' -or $stopActionRows[0][1] -ne 'StopAgent') {
        throw 'StopAgentBeforeInstall source/target validation failed.'
    }
    $upgradeRows = (Get-MsiQueryRows $database 'SELECT `UpgradeCode` FROM `Upgrade`').Rows
    if ($upgradeRows.Count -ne 2) { throw 'Major-upgrade table validation failed.' }
    $versionRows = (Get-MsiQueryRows $database "SELECT `Value` FROM `Property` WHERE `Property`='ProductVersion'").Rows
    $productCodeRows = (Get-MsiQueryRows $database "SELECT `Value` FROM `Property` WHERE `Property`='ProductCode'").Rows
    $upgradeCodeRows = (Get-MsiQueryRows $database "SELECT `Value` FROM `Property` WHERE `Property`='UpgradeCode'").Rows
    if ($versionRows.Count -ne 1 -or $versionRows[0][0] -ne $ProductVersion) { throw 'ProductVersion validation failed.' }
    if ($productCodeRows.Count -ne 1 -or $productCodeRows[0][0] -ne $ProductCode) { throw 'ProductCode validation failed.' }
    if ($upgradeCodeRows.Count -ne 1 -or $upgradeCodeRows[0][0] -ne $UpgradeCode) { throw 'UpgradeCode validation failed.' }
    $sequenceRows = (Get-MsiQueryRows $database 'SELECT `Action` FROM `InstallExecuteSequence`').Rows
    $sequenceNames = @($sequenceRows | ForEach-Object { $_[0] })
    foreach ($requiredAction in @('FindRelatedProducts','StopAgentBeforeInstall','RemoveExistingProducts','InstallFiles')) {
        if ($sequenceNames -notcontains $requiredAction) { throw "InstallExecuteSequence is missing $requiredAction." }
    }
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($database); $database = $null
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($installer); $installer = $null

    [void](New-Item -ItemType Directory -Path (Split-Path -Parent $OutputPath) -Force)
    if ((Test-Path -LiteralPath $OutputPath) -and $Force) { Remove-Item -LiteralPath $OutputPath -Force }
    Copy-Item -LiteralPath $tempMsi -Destination $OutputPath
    Write-Output "MSI: $OutputPath"
    Write-Output "SHA-256: $((Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256).Hash)"
} finally {
    foreach ($object in @($summary,$database,$installer)) {
        if ($null -ne $object -and [Runtime.InteropServices.Marshal]::IsComObject($object)) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($object) }
    }
    if (Test-Path -LiteralPath $tempRoot) { Remove-Item -LiteralPath $tempRoot -Recurse -Force }
}
