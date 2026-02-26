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
set PIPELINE_EXIT=%ERRORLEVEL%
echo [%date% %time%] Pipeline finished with exit code %PIPELINE_EXIT% >> "%LOG_DIR%\main_%TIMESTAMP%.log"

if %PIPELINE_EXIT% NEQ 0 (
    echo [%date% %time%] Pipeline FAILED - skipping all exports >> "%LOG_DIR%\main_%TIMESTAMP%.log"
    goto :end
)

REM Export asset tasks Excel (runs after pipeline + email are done)
set EXPORT_SCRIPT=%SCRIPT_DIR%..\scripts-reference\export_asset_tasks_excel.py
echo [%date% %time%] Starting asset tasks Excel export >> "%LOG_DIR%\main_%TIMESTAMP%.log"
"%VENV_PYTHON%" -u "%EXPORT_SCRIPT%" >> "%LOG_DIR%\main_%TIMESTAMP%.log" 2>&1
echo [%date% %time%] Excel export finished with exit code %ERRORLEVEL% >> "%LOG_DIR%\main_%TIMESTAMP%.log"

REM Export timer data Excel (runs after asset tasks export)
set TIMER_EXPORT_SCRIPT=%SCRIPT_DIR%..\scripts-reference\export_timer_excel.py
echo [%date% %time%] Starting timer data Excel export >> "%LOG_DIR%\main_%TIMESTAMP%.log"
"%VENV_PYTHON%" -u "%TIMER_EXPORT_SCRIPT%" >> "%LOG_DIR%\main_%TIMESTAMP%.log" 2>&1
echo [%date% %time%] Timer Excel export finished with exit code %ERRORLEVEL% >> "%LOG_DIR%\main_%TIMESTAMP%.log"

REM Export QA form Excel (runs after timer export)
set QA_EXPORT_SCRIPT=%SCRIPT_DIR%..\scripts-reference\export_qa_form_excel.py
echo [%date% %time%] Starting QA form Excel export >> "%LOG_DIR%\main_%TIMESTAMP%.log"
"%VENV_PYTHON%" -u "%QA_EXPORT_SCRIPT%" >> "%LOG_DIR%\main_%TIMESTAMP%.log" 2>&1
echo [%date% %time%] QA form Excel export finished with exit code %ERRORLEVEL% >> "%LOG_DIR%\main_%TIMESTAMP%.log"

:end
