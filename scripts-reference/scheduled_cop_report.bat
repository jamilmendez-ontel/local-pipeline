@echo off
REM COP Report - Weekly Monday 1:00 AM
REM Generates Final COP - Pending Task Report, uploads to Drive, emails link

set SCRIPT_DIR=%~dp0
set LOG_DIR=%SCRIPT_DIR%..\swift_api_pipeline\pipeline_logs
set VENV_PYTHON=%SCRIPT_DIR%..\swift_api_pipeline\venv\Scripts\python.exe
REM Use WMIC for consistent date format regardless of locale
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set DT=%%I
set TIMESTAMP=%DT:~0,8%_%DT:~8,4%

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

cd /d "%SCRIPT_DIR%"

echo [%date% %time%] Starting COP report generation >> "%LOG_DIR%\cop_report_%TIMESTAMP%.log"
"%VENV_PYTHON%" -u "%SCRIPT_DIR%export_cop_report.py" >> "%LOG_DIR%\cop_report_%TIMESTAMP%.log" 2>&1
echo [%date% %time%] COP report finished with exit code %ERRORLEVEL% >> "%LOG_DIR%\cop_report_%TIMESTAMP%.log"
