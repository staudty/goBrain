# goBrain — install the Windows Claude Code shipper as a Scheduled Task.
#
# Run this once. Creates (or updates) a task named "goBrain-Ship-ClaudeCode"
# that runs ship-claude-code.ps1 every IntervalMinutes (default 10).
#
# Must be run from PowerShell with enough privilege to register tasks in
# the current user's context. Default execution uses the current user, no
# admin rights needed.
#
# Uninstall with: Unregister-ScheduledTask -TaskName "goBrain-Ship-ClaudeCode" -Confirm:$false

[CmdletBinding()]
param(
    [int]$IntervalMinutes = 10,
    [string]$TaskName = "goBrain-Ship-ClaudeCode"
)

$ErrorActionPreference = "Stop"

$scriptPath = Join-Path $PSScriptRoot "ship-claude-code.ps1"
if (-not (Test-Path $scriptPath)) {
    Write-Error "Expected ship-claude-code.ps1 next to this installer at: $scriptPath"
    exit 1
}

Write-Host "Installing scheduled task: $TaskName"
Write-Host "  Runs every $IntervalMinutes minutes"
Write-Host "  Script: $scriptPath"

# Run PowerShell with the script in NonInteractive, no-profile, bypass-policy mode.
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$scriptPath`""

# Repeat every N minutes forever, starting 2 minutes from now.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

# Run whether logged in or not; wake up to run; don't kill if it runs long.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable:$false `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

# Interactive logon — runs as the current user when they're logged in. No
# admin rights required (unlike S4U which runs even without a logged-in user
# but requires the "Log on as a batch job" privilege).
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

# Replace any existing registration
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing task..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "goBrain: every $IntervalMinutes min, copy finished Claude Code JSONL sessions from ~/.claude/projects to the synced Brain vault for ingestion." `
        -ErrorAction Stop | Out-Null
} catch {
    Write-Error "Failed to register scheduled task: $_"
    Write-Host ""
    Write-Host "If this says 'Access is denied', try one of:"
    Write-Host "  1. Run PowerShell as Administrator and re-run this installer"
    Write-Host "  2. Or install the task manually via Task Scheduler GUI using ship-claude-code.ps1"
    exit 1
}

Write-Host ""
Write-Host "Installed."
Write-Host ""
Write-Host "Run it once now (useful to backfill existing sessions):"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ""
Write-Host "Verify status:"
Write-Host "  Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host ""
Write-Host "Uninstall:"
Write-Host "  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
