' goBrain — hidden-window runner for ship-claude-code.ps1
'
' Reason this file exists:
'   Scheduled tasks that invoke powershell.exe directly create a visible
'   console window for ~1-2 seconds, which both (a) is visually annoying when
'   a user is working on their PC every 10 minutes, and (b) can register as
'   display-wake activity on Windows, preventing monitor sleep.
'
' This VBScript uses WScript.Shell.Run with the third parameter set to 0,
' which means "hidden window." wscript.exe (which runs .vbs files) doesn't
' create its own console window either, so the entire invocation is invisible.
'
' Scheduled by install-shipper.ps1; not intended to be launched manually.

Option Explicit

Dim oShell, sScriptDir, sPowerShellScript, sCmd

Set oShell = CreateObject("WScript.Shell")

' Path to the sibling .ps1 — resolved via this .vbs file's own location
sScriptDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
sPowerShellScript = sScriptDir & "ship-claude-code.ps1"

' Match the flags we were using before. -WindowStyle Hidden is redundant given
' we pass 0 to Run, but it's a belt-and-suspenders no-cost addition.
sCmd = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass " & _
       "-WindowStyle Hidden -File """ & sPowerShellScript & """"

' 0 = hidden window; False = don't wait for the process to finish (fire and forget).
' We don't need to wait — the task scheduler doesn't care about our return.
oShell.Run sCmd, 0, False
