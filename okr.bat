@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%~1"=="" (
    python scripts\okr_tool.py dashboard
    pause
) else (
    python scripts\okr_tool.py %*
)
