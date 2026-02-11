@echo off
REM Gmail Pipeline - Hourly Poll (1 AM - 10 AM)
REM Checks if today's data exists, runs aging/sales if not

set SCRIPT_DIR=%~dp0
set LOG_DIR=%SCRIPT_DIR%pipeline_logs
set VENV_PYTHON=%SCRIPT_DIR%venv\Scripts\python.exe
set TIMESTAMP=%date:~-4%%date:~4,2%%date:~7,2%_%time:~0,2%%time:~3,2%
set TIMESTAMP=%TIMESTAMP: =0%

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

cd /d "%SCRIPT_DIR%"

echo [%date% %time%] Starting Gmail pipeline check >> "%LOG_DIR%\gmail_%TIMESTAMP%.log"
"%VENV_PYTHON%" -u run_gmail_pipelines.py >> "%LOG_DIR%\gmail_%TIMESTAMP%.log" 2>&1
echo [%date% %time%] Gmail pipeline finished with exit code %ERRORLEVEL% >> "%LOG_DIR%\gmail_%TIMESTAMP%.log"
