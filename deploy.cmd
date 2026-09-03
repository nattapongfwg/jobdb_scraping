@echo off
REM Double-click to deploy the latest code: install deps, restart the board, health-check.
REM Add -Pull to also "git pull" first:   deploy.cmd -Pull
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0service.ps1" deploy %*
echo.
pause
