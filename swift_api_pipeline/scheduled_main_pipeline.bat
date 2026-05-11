@echo off
setlocal enabledelayedexpansion
REM Swift API Pipeline - Nightly Local Run (12:01 AM)
REM Runs: asset_tasks -> backfill -> analytics -> dispatch GHA (export + timer discrepancies)
REM        -> dispatch date-validator-daily (separate narrow PAT from .env).
REM All other pipelines run on GitHub Actions.
REM Each step retries once after 5 min on failure.
REM Reads working PAT from C:\Users\admin\.secrets\github_token for local-pipeline GHA dispatch.
REM Reads narrow PAT from .env (GITHUB_PAT) for the date-validator dispatch.

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

REM === 1a. Asset Tasks EXTRACT (Swift API -> raw_asset_tasks) ===
REM Split from transform so a transform-only failure doesn't waste the 60-min API pull.
echo [%date% %time%] Starting asset_tasks_extract >> "%LOGFILE%"
"%VENV_PYTHON%" -u main.py --pipeline asset_tasks_extract >> "%LOGFILE%" 2>&1
set EXIT_CODE=!ERRORLEVEL!
echo [%date% %time%] asset_tasks_extract finished with exit code !EXIT_CODE! >> "%LOGFILE%"

if !EXIT_CODE! NEQ 0 (
    echo [%date% %time%] asset_tasks_extract FAILED - retrying after 5 minutes >> "%LOGFILE%"
    timeout /t 300 /nobreak > nul
    "%VENV_PYTHON%" -u main.py --pipeline asset_tasks_extract >> "%LOGFILE%" 2>&1
    set EXIT_CODE=!ERRORLEVEL!
    echo [%date% %time%] asset_tasks_extract retry finished with exit code !EXIT_CODE! >> "%LOGFILE%"
)

if !EXIT_CODE! NEQ 0 (
    echo [%date% %time%] asset_tasks_extract FAILED after retry - skipping transform, backfill, analytics, and GHA dispatch >> "%LOGFILE%"
    goto :done
)

REM === 1b. Asset Tasks TRANSFORM (raw -> stg_assets + stg_asset_tasks) ===
REM Reads the latest successful asset_tasks_extract run_id from pipeline.pipeline_runs
REM and runs the SQL aggregation RPCs only - ~5-10 min vs the ~60 min extract.
echo [%date% %time%] Starting asset_tasks_transform >> "%LOGFILE%"
"%VENV_PYTHON%" -u main.py --pipeline asset_tasks_transform >> "%LOGFILE%" 2>&1
set EXIT_CODE=!ERRORLEVEL!
echo [%date% %time%] asset_tasks_transform finished with exit code !EXIT_CODE! >> "%LOGFILE%"

if !EXIT_CODE! NEQ 0 (
    echo [%date% %time%] asset_tasks_transform FAILED - retrying after 5 minutes >> "%LOGFILE%"
    timeout /t 300 /nobreak > nul
    "%VENV_PYTHON%" -u main.py --pipeline asset_tasks_transform >> "%LOGFILE%" 2>&1
    set EXIT_CODE=!ERRORLEVEL!
    echo [%date% %time%] asset_tasks_transform retry finished with exit code !EXIT_CODE! >> "%LOGFILE%"
)

if !EXIT_CODE! NEQ 0 (
    echo [%date% %time%] asset_tasks_transform FAILED after retry - skipping backfill, analytics, and GHA dispatch >> "%LOGFILE%"
    goto :done
)

REM === 1b. Assets Status (extract + enrich stg_assets.asset_status) ===
REM Must run AFTER asset_tasks (which rebuilds stg_assets) so the UPDATE has rows to enrich.
REM Failure here is non-fatal: downstream consumers handle NULL asset_status gracefully.
echo [%date% %time%] Starting assets pipeline >> "%LOGFILE%"
"%VENV_PYTHON%" -u main.py --pipeline assets --no-email >> "%LOGFILE%" 2>&1
set EXIT_CODE=!ERRORLEVEL!
echo [%date% %time%] assets finished with exit code !EXIT_CODE! >> "%LOGFILE%"

if !EXIT_CODE! NEQ 0 (
    echo [%date% %time%] assets FAILED - retrying after 5 minutes >> "%LOGFILE%"
    timeout /t 300 /nobreak > nul
    "%VENV_PYTHON%" -u main.py --pipeline assets --no-email >> "%LOGFILE%" 2>&1
    set EXIT_CODE=!ERRORLEVEL!
    echo [%date% %time%] assets retry finished with exit code !EXIT_CODE! >> "%LOGFILE%"
)
REM Continue regardless — asset_status NULL is degrading-but-acceptable downstream.

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
set TOKEN_FILE=C:\Users\admin\.secrets\github_token
if not exist "%TOKEN_FILE%" (
    echo [%date% %time%] ERROR: GitHub token file not found at %TOKEN_FILE% - skipping GHA dispatch >> "%LOGFILE%"
    goto :done
)
set /p GITHUB_TOKEN=<"%TOKEN_FILE%"
echo [%date% %time%] Dispatching asset tasks export + timer discrepancies to GitHub Actions >> "%LOGFILE%"
"%VENV_PYTHON%" -u -c "import requests,os,sys; results=[requests.post('https://api.github.com/repos/jamilmendez-ontel/local-pipeline/dispatches', json={'event_type': t}, headers={'Authorization': f'Bearer {os.environ[\"GITHUB_TOKEN\"]}', 'Accept': 'application/vnd.github+json'}) for t in ['pipeline-asset-tasks-export', 'pipeline-timer-discrepancies']]; [print(f'{r.status_code} {r.reason} for {r.request.body}') for r in results]; sys.exit(0 if all(r.status_code in (200,204) for r in results) else 1)" >> "%LOGFILE%" 2>&1
set EXIT_CODE=!ERRORLEVEL!
if !EXIT_CODE! NEQ 0 (
    echo [%date% %time%] ERROR: GHA dispatch failed - check token validity >> "%LOGFILE%"
) else (
    echo [%date% %time%] GHA dispatches sent successfully >> "%LOGFILE%"
)

REM === 5. Dispatch date-validator-daily ===
REM Without this, the validator only ever sees gmail-scraper's 00:41 ET trigger,
REM which fires BEFORE asset_tasks finishes (~00:52 ET), so the preflight gate
REM trips on UpstreamStale and the day's emails never go out. Firing here, at
REM the tail of the local batch, guarantees asset_tasks is "today's success" by
REM the time the validator runs preflight. Uses the narrow GITHUB_PAT from .env
REM (scoped to date-validator only, kept separate from the working PAT).
echo [%date% %time%] Dispatching date-validator-daily to GitHub Actions >> "%LOGFILE%"
"%VENV_PYTHON%" -u -c "from dotenv import load_dotenv; load_dotenv(); from github_trigger import fire_dispatch; import sys; sys.exit(0 if fire_dispatch('jamilmendez-ontel/date-validator', 'date-validator-daily', {'source': 'nightly-batch'}) else 1)" >> "%LOGFILE%" 2>&1
set EXIT_CODE=!ERRORLEVEL!
if !EXIT_CODE! NEQ 0 (
    echo [%date% %time%] WARNING: date-validator dispatch failed - check GITHUB_PAT in .env >> "%LOGFILE%"
) else (
    echo [%date% %time%] date-validator dispatch sent successfully >> "%LOGFILE%"
)

:done
echo [%date% %time%] Nightly local pipeline run complete >> "%LOGFILE%"

endlocal
