# Register a logon task that runs the kakao-mcp daemon inside your Windows session
# (window messages do not cross sessions, so it cannot run as a service or from SSH).
# Safe to run again after an update.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Py)) { throw "create the venv first: py -3.13 -m venv .venv; .venv\Scripts\pip install -r requirements-win.txt" }
New-Item -ItemType Directory -Force (Join-Path $Root "data") | Out-Null
$Name = "kakao-mcp"
$action = New-ScheduledTaskAction -Execute $Py -Argument "-m kkt.win.server --http" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $Name
Write-Output "daemon task '$Name' registered and started: $Py -m kkt.win.server --http (in $Root)"
Write-Output "put your KakaoTalk name in $Root\data\me.txt so your own messages are recognised"
