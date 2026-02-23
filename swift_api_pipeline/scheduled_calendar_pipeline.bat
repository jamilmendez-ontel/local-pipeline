@echo off
REM Calendar Leave Pipeline - Daily incremental run (12:30 AM)
REM Fetches new/updated leave events from Google Calendar into Supabase

set SCRIPT_DIR=%~dp0
set LOG_DIR=%SCRIPT_DIR%pipeline_logs
set VENV_PYTHON=%SCRIPT_DIR%venv\Scripts\python.exe
REM Use WMIC for consistent date format regardless of locale
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set DT=%%I
set TIMESTAMP=%DT:~0,8%_%DT:~8,4%

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

cd /d "%SCRIPT_DIR%"

echo [%date% %time%] Starting calendar leave pipeline >> "%LOG_DIR%\calendar_%TIMESTAMP%.log"
"%VENV_PYTHON%" -u extract_calendar_leave.py >> "%LOG_DIR%\calendar_%TIMESTAMP%.log" 2>&1
echo [%date% %time%] Calendar leave pipeline finished with exit code %ERRORLEVEL% >> "%LOG_DIR%\calendar_%TIMESTAMP%.log"
