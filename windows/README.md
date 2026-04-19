# Windows — Claude Code session shipper

Gets this Windows PC's Claude Code terminal sessions into the goBrain pipeline.

## What it does

Every 10 minutes, copies any *finished* Claude Code session JSONL files
(idle for 5+ minutes) from `%USERPROFILE%\.claude\projects\` into
`%USERPROFILE%\Brain\.claude-code-sources\pc\` — a dot-prefixed folder
inside your synced Brain vault that Obsidian ignores.

Synology Drive Client already syncs Brain to the NAS. The Mac ingester's
Claude Code watcher reads the shipped JSONLs from the same path on the Mac
side and ingests them normally.

Idempotent: re-running the copy is a no-op unless the source file has been
updated. If the ingester has already seen a session, it's deduplicated by
`(source, source_id)` — you pay Gemma time only for net-new work.

## One-time install

Open **PowerShell** (no admin needed), navigate to this folder, run:

```powershell
cd C:\path\to\goBrain\windows
.\install-shipper.ps1
```

Verify the task is registered:

```powershell
Get-ScheduledTask -TaskName "goBrain-Ship-ClaudeCode"
Get-ScheduledTaskInfo -TaskName "goBrain-Ship-ClaudeCode"
```

## Backfill existing sessions

The first scheduled run already copies everything. If you want to force it
right now without waiting 10 minutes, start the task manually:

```powershell
Start-ScheduledTask -TaskName "goBrain-Ship-ClaudeCode"
```

Then watch `%USERPROFILE%\Brain\.claude-code-sources\pc\` populate. Drive Client
will push the files to the NAS and down to the Mac, where the ingester picks
them up.

## Uninstall

```powershell
Unregister-ScheduledTask -TaskName "goBrain-Ship-ClaudeCode" -Confirm:$false
```

## Changing paths or interval

If Claude Code stores its projects somewhere other than `%USERPROFILE%\.claude\projects\`
on your machine (e.g., `D:\.claude\projects`), edit `install-shipper.ps1` to call
`ship-claude-code.ps1` with an explicit `-Source` parameter.

To change the interval, re-run `install-shipper.ps1 -IntervalMinutes 15` (or
whatever you want).
