@echo off
rem Starts the VSPL Tool and Die Management API (port 8010) and screens (port 5180), then opens the browser.
cd /d "%~dp0backend"
start "VSPL API" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8010"
cd /d "%~dp0frontend"
start "VSPL screens" cmd /k "set VITE_API_URL=http://127.0.0.1:8010&& npx vite --host 127.0.0.1 --port 5180 --strictPort"
timeout /t 8 > nul
start http://127.0.0.1:5180/
