@echo off
REM Swift API Pipeline - Nightly Full Run (12:01 AM)
REM Logs output to pipeline_logs directory

set SCRIPT_DIR=%~dp0
set LOG_DIR=%SCRIPT_DIR%pipeline_logs
set VENV_PYTHON=%SCRIPT_DIR%venv\Scripts\python.exe
REM Use WMIC for consistent date format regardless of locale
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set DT=%%I
set TIMESTAMP=%DT:~0,8%_%DT:~8,4%

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

cd /d "%SCRIPT_DIR%"

echo [%date% %time%] Starting nightly pipeline run >> "%LOG_DIR%\main_%TIMESTAMP%.log"
"%VENV_PYTHON%" -u main.py >> "%LOG_DIR%\main_%TIMESTAMP%.log" 2>&1
echo [%date% %time%] Pipeline finished with exit code %ERRORLEVEL% >> "%LOG_DIR%\main_%TIMESTAMP%.log"
