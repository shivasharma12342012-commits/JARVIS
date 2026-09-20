' ============================================================================
'  Silently launches J.A.R.V.I.S. Desktop — no console window appears.
'
'  Double-click this file directly, or double-click the desktop shortcut made
'  by create_shortcut.vbs, which points here.
'
'  If something goes wrong, the details are written to jarvis_launcher.log
'  next to this script, and a message box tells you to check it.
' ============================================================================
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
batPath = here & "\run_jarvis.bat"
logPath = here & "\jarvis_launcher.log"

If Not fso.FileExists(batPath) Then
    MsgBox "Could not find run_jarvis.bat next to this script.", vbExclamation, "J.A.R.V.I.S."
    WScript.Quit 1
End If

' 0 = hidden window, True = wait for it to finish so we can read the exit code
exitCode = shell.Run("""" & batPath & """", 0, True)

If exitCode <> 0 Then
    details = ""
    If fso.FileExists(logPath) Then
        Set f = fso.OpenTextFile(logPath, 1)
        details = f.ReadAll
        f.Close
        ' Keep the message box readable: just the tail of the log.
        If Len(details) > 1200 Then
            details = "..." & Right(details, 1200)
        End If
    End If
    MsgBox "J.A.R.V.I.S. did not start (exit code " & exitCode & ")." & vbCrLf & vbCrLf & _
           "Details from jarvis_launcher.log:" & vbCrLf & details, _
           vbExclamation, "J.A.R.V.I.S."
End If
