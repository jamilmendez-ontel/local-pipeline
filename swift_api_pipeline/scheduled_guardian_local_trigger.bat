@echo off
REM Guardian Local Trigger — runs every 5 min via Task Scheduler
REM Checks agent.monitor_state for approved local pipeline triggers

cd /d "%~dp0"
venv\Scripts\python.exe guardian_local_trigger.py
