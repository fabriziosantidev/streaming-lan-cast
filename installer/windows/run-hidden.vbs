' Streaming LAN Cast: start the local helper (control server)
' Works from any folder: it launches the .exe sitting next to this script.
dir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
CreateObject("WScript.Shell").Run """" & dir & "streaming-lan-cast-helper.exe"" --serve", 0, False
