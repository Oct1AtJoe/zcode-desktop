@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 启动 ZCode 用量监控桌面组件。
rem 优先使用无控制台窗口的 pythonw 启动（适合 GUI 程序），
rem 找不到时依次回退到 python、py。均不可用则提示安装 Python。

where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw "%~dp0widget.py"
    exit
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0widget.py"
    exit
)

where python >nul 2>nul
if %errorlevel%==0 (
    start "" python "%~dp0widget.py"
    exit
)

echo [FAIL] 未找到 Python，请先安装 Python 3 并将其加入 PATH。
pause
