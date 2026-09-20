' ============================================================================
'  Creates a J.A.R.V.I.S. shortcut on your Desktop, pointing at JARVIS.vbs
'  (the silent launcher — no console window), using jarvis_logo.ico as its
'  icon.
'
'  Run this once by double-clicking it. It assumes JARVIS.vbs, run_jarvis.bat
'  and jarvis_logo.ico are all sitting in the same folder as this script (the
'  JARVIS-main folder).
' ============================================================================
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
vbsPath = here & "\JARVIS.vbs"
batPath = here & "\run_jarvis.bat"
iconPath = here & "\jarvis_logo.ico"
desktopPath = shell.SpecialFolders("Desktop")

If Not fso.FileExists(vbsPath) Then
    MsgBox "Could not find JARVIS.vbs next to this script.", vbExclamation, "J.A.R.V.I.S."
    WScript.Quit 1
End If

If Not fso.FileExists(batPath) Then
    MsgBox "Could not find run_jarvis.bat next to this script.", vbExclamation, "J.A.R.V.I.S."
    WScript.Quit 1
End If

If Not fso.FileExists(iconPath) Then
    MsgBox "Could not find jarvis_logo.ico next to this script.", vbExclamation, "J.A.R.V.I.S."
    WScript.Quit 1
End If

Set shortcut = shell.CreateShortcut(desktopPath & "\J.A.R.V.I.S..lnk")
' wscript.exe runs the .vbs with no console window at all.
shortcut.TargetPath = "wscript.exe"
shortcut.Arguments = """" & vbsPath & """"
shortcut.WorkingDirectory = here
shortcut.IconLocation = iconPath & ", 0"
shortcut.WindowStyle = 1
shortcut.Description = "Launch J.A.R.V.I.S. Desktop"
shortcut.Save

MsgBox "Done! A J.A.R.V.I.S. shortcut with your logo is now on your Desktop." & vbCrLf & _
       "It opens straight to the app - no console window.", vbInformation, "J.A.R.V.I.S."
