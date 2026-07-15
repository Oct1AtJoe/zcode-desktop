@echo off
cd /d "%~dp0"

rem Launch the ZCode usage desktop widget.
rem Prefer the windowless pythonw (good for GUI apps). Fall back to python / py.
rem Use absolute path first to bypass the WindowsApps Store alias placeholders.

rem 1) Prefer: known real pythonw path on this machine
set "PYW=C:\Users\Administrator\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"
if exist "%PYW%" (
    start "" "%PYW%" "%~dp0widget.py"
    exit /b
)

rem 2) Fallback: pythonw from PATH, excluding WindowsApps alias
for /f "delims=" %%I in ('where pythonw 2^>nul ^| findstr /v /i "WindowsApps"') do (
    start "" "%%I" "%~dp0widget.py"
    exit /b
)

rem 3) Fallback: python from PATH, excluding WindowsApps alias
for /f "delims=" %%I in ('where python 2^>nul ^| findstr /v /i "WindowsApps"') do (
    start "" "%%I" "%~dp0widget.py"
    exit /b
)

rem 4) Fallback: py from PATH, excluding WindowsApps alias
for /f "delims=" %%I in ('where py 2^>nul ^| findstr /v /i "WindowsApps"') do (
    start "" "%%I" "%~dp0widget.py"
    exit /b
)

echo [FAIL] Python not found. Please install Python 3 and add it to PATH.
pause
