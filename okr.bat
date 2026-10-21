@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (
    echo 没有找到 Python，请先安装：https://www.python.org/downloads/
    echo 安装时记得勾选 "Add python.exe to PATH"
    pause
    exit /b 1
)
if "%~1"=="" (
    python scripts\okr_web.py
    if errorlevel 1 pause
) else (
    python scripts\okr_tool.py %*
)
