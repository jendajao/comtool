@echo off
rem comtool hub launcher for SSH mode (double-click on the local Windows PC).
rem No personal settings here: comhub.py reads comhub.ini in this directory
rem ([ssh] user/host/repo, [hub] tftp_root). Keep this window open while using comtool.
cd /d "%~dp0"
python --version >nul 2>nul
if not errorlevel 1 (
    python comhub.py %*
) else (
    py -3 comhub.py %*
)
echo.
echo comhub exited.
pause
