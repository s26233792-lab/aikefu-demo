' 千帆客服 Agent 桌面启动器（VBS，双击不闪黑框）
' 静默调用 start.bat 启动整套服务
Set shell = CreateObject("WScript.Shell")
baseDir = "C:\Users\Terrt\Downloads\ds\xhs-kefu-demo"
shell.CurrentDirectory = baseDir
' 用隐藏窗口方式运行 start.bat
shell.Run """" & baseDir & "\start.bat""", 1, False
