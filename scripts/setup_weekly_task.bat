@echo off
:: ============================================================
:: setup_weekly_task.bat
:: Registers a Windows Task Scheduler job that runs the NFL
:: data refresh every Tuesday at 6:00 AM.
::
:: Run this ONCE as Administrator, then forget about it.
:: The task will run automatically every week.
:: ============================================================

:: Detect the Python executable — use the one that's on PATH
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: python not found on PATH. Install Python 3.9+ and try again.
    pause
    exit /b 1
)

:: Resolve the absolute path to the project root (one level up from scripts\)
set "SCRIPT_DIR=%~dp0"
set "PROJECT_DIR=%SCRIPT_DIR%.."

:: Normalize the path (removes trailing backslash and resolves ..)
pushd "%PROJECT_DIR%"
set "PROJECT_DIR=%CD%"
popd

set "UPDATE_SCRIPT=%PROJECT_DIR%\scripts\update_data.py"
set "LOG_FILE=%PROJECT_DIR%\scripts\update_data.log"
set "TASK_NAME=NFL_Chatbot_WeeklyDataRefresh"

echo.
echo Project root : %PROJECT_DIR%
echo Script       : %UPDATE_SCRIPT%
echo Log file     : %LOG_FILE%
echo.

:: Delete any existing task with this name so we can re-register cleanly
schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1

:: Create the weekly task
::   /SC WEEKLY /D TUE  — every Tuesday
::   /ST 06:00          — at 6:00 AM
::   /RL HIGHEST        — run with highest available privileges
::   /F                 — force creation without prompt
schtasks /create ^
  /tn "%TASK_NAME%" ^
  /tr "python \"%UPDATE_SCRIPT%\"" ^
  /sc WEEKLY ^
  /d TUE ^
  /st 06:00 ^
  /rl HIGHEST ^
  /f

if %errorlevel% equ 0 (
    echo.
    echo [OK] Task "%TASK_NAME%" registered successfully.
    echo      It will run every Tuesday at 6:00 AM.
    echo.
    echo To run it manually right now:
    echo   schtasks /run /tn "%TASK_NAME%"
    echo.
    echo To remove it later:
    echo   schtasks /delete /tn "%TASK_NAME%" /f
) else (
    echo.
    echo [ERROR] Failed to register the task. Try running this script as Administrator.
)

pause
