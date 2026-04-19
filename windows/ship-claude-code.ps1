# goBrain — ship Windows Claude Code sessions into the Brain vault
#
# Copies finished Claude Code JSONL session files from this machine's
# ~/.claude/projects/ into the synced Brain vault at
# Brain/.claude-code-sources/pc/. Drive Client syncs that to the NAS and
# on to the Mac, where the ingester's Claude Code watcher picks them up and
# summarizes + embeds each session into the brain.
#
# Idempotent: only copies files that are (a) idle > IdleMinutes and
# (b) newer on source than on destination.
#
# Run on a schedule (every ~10 minutes is plenty). See install-shipper.ps1.

[CmdletBinding()]
param(
    # Where Claude Code writes its session JSONLs on this Windows machine.
    # Default is the standard location; override if you've installed elsewhere.
    [string]$Source = (Join-Path $env:USERPROFILE ".claude\projects"),

    # Synced Brain folder's landing zone for shipped PC sessions. Dot-prefixed
    # so Obsidian ignores it.
    [string]$Destination = (Join-Path $env:USERPROFILE "Brain\.claude-code-sources\pc"),

    # Only ship sessions that have been idle for at least this long (minutes).
    # Matches the Mac ingester's own 5-min idle window.
    [int]$IdleMinutes = 5,

    # Optional: a machine name tag. Prepended to project names to keep
    # multiple machines' sessions distinguishable if you ever ship from more
    # than one. Default: computer name.
    [string]$MachineTag = ($env:COMPUTERNAME).ToLower()
)

$ErrorActionPreference = "Stop"
$cutoff = (Get-Date).AddMinutes(-$IdleMinutes)

if (-not (Test-Path $Source)) {
    Write-Host "Source path not found: $Source"
    Write-Host "Claude Code may not have created any sessions yet on this machine."
    exit 0
}

if (-not (Test-Path $Destination)) {
    New-Item -Path $Destination -ItemType Directory -Force | Out-Null
}

$copiedCount = 0
$skippedCount = 0
$totalSeen = 0

Get-ChildItem -Path $Source -Recurse -Filter "*.jsonl" -File | ForEach-Object {
    $totalSeen++
    $file = $_

    # Skip files still being written to
    if ($file.LastWriteTime -gt $cutoff) {
        $skippedCount++
        return
    }

    # Relative path under Source (e.g., -D-code-foo\a1b2c3d4.jsonl)
    $rel = $file.FullName.Substring($Source.Length).TrimStart('\')

    # Prepend machine tag to the project name so Windows sessions never
    # collide with Mac sessions even if Claude Code reuses a UUID.
    # Path looks like <project>\<session>.jsonl; we rewrite to
    # <machineTag>_<project>\<session>.jsonl
    $parts = $rel.Split('\', 2)
    if ($parts.Length -ne 2) { return }
    $project = "$($MachineTag)_$($parts[0])"
    $session = $parts[1]
    $destFile = Join-Path (Join-Path $Destination $project) $session

    $destDir = Split-Path $destFile -Parent
    if (-not (Test-Path $destDir)) {
        New-Item -Path $destDir -ItemType Directory -Force | Out-Null
    }

    $needCopy = $true
    if (Test-Path $destFile) {
        $destInfo = Get-Item $destFile
        if ($destInfo.LastWriteTime -ge $file.LastWriteTime -and
            $destInfo.Length -eq $file.Length) {
            $needCopy = $false
        }
    }

    if ($needCopy) {
        Copy-Item -Path $file.FullName -Destination $destFile -Force
        $copiedCount++
    }
}

$timestamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
Write-Host "[$timestamp] goBrain shipper: seen=$totalSeen copied=$copiedCount idle-skipped=$skippedCount"
