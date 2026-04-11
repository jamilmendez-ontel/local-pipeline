@echo off
setlocal enabledelayedexpansion
REM Swift API Pipeline - Nightly Local Run (12:01 AM)
REM Runs: asset_tasks -> backfill -> analytics -> dispatch GHA (export + timer discrepancies)
REM All other pipelines run on GitHub Actions.
REM Each step retries once after 5 min on failure.
REM Requires GITHUB_TOKEN environment variable (fine-grained PAT) for GHA dispatch.

set SCRIPT_DIR=%~dp0
set LOG_DIR=%SCRIPT_DIR%pipeline_logs
set VENV_PYTHON=%SCRIPT_DIR%venv\Scripts\python.exe
REM Use WMIC for consistent date format regardless of locale
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set DT=%%I
set TIMESTAMP=%DT:~0,8%_%DT:~8,4%

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

cd /d "%SCRIPT_DIR%"

set LOGFILE=%LOG_DIR%\main_%TIMESTAMP%.log

echo [%date% %time%] Starting nightly local pipeline run >> "%LOGFILE%"

REM === 1. Asset Tasks (extract + transform) ===
echo [%date% %time%] Starting asset_tasks pipeline >> "%LOGFILE%"
"%VENV_PYTHON%" -u main.py --pipeline asset_tasks >> "%LOGFILE%" 2>&1
set EXIT_CODE=!ERRORLEVEL!
echo [%date% %time%] asset_tasks finished with exit code !EXIT_CODE! >> "%LOGFILE%"

if !EXIT_CODE! NEQ 0 (
    echo [%date% %time%] asset_tasks FAILED - retrying after 5 minutes >> "%LOGFILE%"
    timeout /t 300 /nobreak > nul
    "%VENV_PYTHON%" -u main.py --pipeline asset_tasks >> "%LOGFILE%" 2>&1
    set EXIT_CODE=!ERRORLEVEL!
    echo [%date% %time%] asset_tasks retry finished with exit code !EXIT_CODE! >> "%LOGFILE%"
)

if !EXIT_CODE! NEQ 0 (
    echo [%date% %time%] asset_tasks FAILED after retry - skipping backfill and analytics >> "%LOGFILE%"
    goto :dispatch
)

REM === 2. Asset DID Backfill ===
echo [%date% %time%] Starting backfill >> "%LOGFILE%"
"%VENV_PYTHON%" -u main.py --pipeline backfill --no-email >> "%LOGFILE%" 2>&1
set EXIT_CODE=!ERRORLEVEL!
echo [%date% %time%] backfill finished with exit code !EXIT_CODE! >> "%LOGFILE%"

if !EXIT_CODE! NEQ 0 (
    echo [%date% %time%] backfill FAILED - retrying after 5 minutes >> "%LOGFILE%"
    timeout /t 300 /nobreak > nul
    "%VENV_PYTHON%" -u main.py --pipeline backfill --no-email >> "%LOGFILE%" 2>&1
    set EXIT_CODE=!ERRORLEVEL!
    echo [%date% %time%] backfill retry finished with exit code !EXIT_CODE! >> "%LOGFILE%"
)

REM === 3. Analytics MV Refresh ===
echo [%date% %time%] Starting analytics refresh >> "%LOGFILE%"
"%VENV_PYTHON%" -u main.py --pipeline analytics --no-email >> "%LOGFILE%" 2>&1
set EXIT_CODE=!ERRORLEVEL!
echo [%date% %time%] analytics finished with exit code !EXIT_CODE! >> "%LOGFILE%"

if !EXIT_CODE! NEQ 0 (
    echo [%date% %time%] analytics FAILED - retrying after 5 minutes >> "%LOGFILE%"
    timeout /t 300 /nobreak > nul
    "%VENV_PYTHON%" -u main.py --pipeline analytics --no-email >> "%LOGFILE%" 2>&1
    set EXIT_CODE=!ERRORLEVEL!
    echo [%date% %time%] analytics retry finished with exit code !EXIT_CODE! >> "%LOGFILE%"
)

REM === 4. Dispatch asset tasks export + timer discrepancies to GHA ===
:dispatch
echo [%date% %time%] Dispatching asset tasks export to GitHub Actions >> "%LOGFILE%"
"%VENV_PYTHON%" -u -c "import requests,os; [requests.post('https://api.github.com/repos/jamilmendez-ontel/local-pipeline/dispatches', json={'event_type': t}, headers={'Authorization': f'Bearer {os.environ[\"GITHUB_TOKEN\"]}', 'Accept': 'application/vnd.github+json'}) for t in ['pipeline-asset-tasks-export', 'pipeline-timer-discrepancies']]" >> "%LOGFILE%" 2>&1
echo [%date% %time%] GHA dispatches sent >> "%LOGFILE%"

echo [%date% %time%] Nightly local pipeline run complete >> "%LOGFILE%"

endlocal
