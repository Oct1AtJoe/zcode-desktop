@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 启动 ZCode 用量监控桌面组件。
rem 优先使用无控制台窗口的 pythonw 启动（适合 GUI 程序），
rem 找不到时依次回退到 python、py。均不可用则提示安装 Python。
rem 注意：用绝对路径优先，绕开 WindowsApps 的 Store 占位别名（pyw/pythonw 别名）。

rem 1) 优先：本机已知的真实 pythonw 路径
set "PYW=C:\Users\Administrator\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"
if exist "%PYW%" (
    start "" "%PYW%" "%~dp0widget.py"
    exit /b
)

rem 2) 回退：用 where 排除 WindowsApps 别名后的 pythonw
for /f "delims=" %%I in ('where pythonw 2^>nul ^| findstr /v /i "WindowsApps"') do (
    start "" "%%I" "%~dp0widget.py"
    exit /b
)

rem 3) 回退：排除别名的 python
for /f "delims=" %%I in ('where python 2^>nul ^| findstr /v /i "WindowsApps"') do (
    start "" "%%I" "%~dp0widget.py"
    exit /b
)

rem 4) 回退：排除别名的 py
for /f "delims=" %%I in ('where py 2^>nul ^| findstr /v /i "WindowsApps"') do (
    start "" "%%I" "%~dp0widget.py"
    exit /b
)

echo [FAIL] 未找到 Python，请先安装 Python 3 并将其加入 PATH。
pause
