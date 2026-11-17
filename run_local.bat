@echo off
REM Optional: run the helper directly on Windows (no Docker) against Z:\ for testing.
REM Requires Python 3.11+.  First time:  pip install -r requirements.txt
cd /d "%~dp0app"
if "%HELPER_API_KEY%"=="" set /p HELPER_API_KEY=Enter API key: 
set SHARE_ROOT=Z:\
set DISPLAY_ROOT=Z:\
python -m uvicorn main:app --host 127.0.0.1 --port 8765
