' Control Data Waste - Hidden Server Launcher
' يقفل فقط النسخة بتاعة المشروع ده

Set objShell = CreateObject("WScript.Shell")
Set objWMI   = GetObject("winmgmts:\\.\root\cimv2")
strPath      = objShell.CurrentDirectory
strAppPy     = strPath & "\app.py"

' ===== أقفل فقط العملية اللي بتشغّل app.py من نفس المجلد =====
Set colProc = objWMI.ExecQuery("SELECT * FROM Win32_Process WHERE Name='python.exe' OR Name='python3.exe'")
For Each proc In colProc
    cmdLine = proc.CommandLine
    If InStr(LCase(cmdLine), LCase(strAppPy)) > 0 Then
        proc.Terminate()
    End If
Next

' انتظر عشان العملية تنتهي
WScript.Sleep 1200

' ===== شغّل السيرفر الجديد في الخلفية =====
objShell.Run "python """ & strAppPy & """", 0, False
