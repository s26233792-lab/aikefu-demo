' 栀夏客服 Agent Windows 启动器（路径相对当前脚本，无用户目录硬编码）
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = baseDir
shell.Run """" & baseDir & "\start.bat""", 1, False
