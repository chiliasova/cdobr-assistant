@echo off
rem CDOBR assistant: start on Windows. Ollama must be running (tray icon).
rem Only ASCII in this file: cmd.exe misreads Cyrillic in .bat files.
cd /d %~dp0
if not exist .venv (
  echo Creating virtual environment...
  py -3.14 -m venv .venv || py -3.13 -m venv .venv || py -3.12 -m venv .venv || python -m venv .venv
)
echo Installing libraries, 1-3 minutes...
.venv\Scripts\python -m pip install -q --no-cache-dir --disable-pip-version-check -r requirements.txt
echo.
echo Open in browser: http://127.0.0.1:8000   (login: employee / employee or doc / doc)
echo First start builds the search index with bge-m3, about 1-3 minutes.
echo.
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
echo.
echo Server stopped. If there is an error above, send a screenshot.
pause
