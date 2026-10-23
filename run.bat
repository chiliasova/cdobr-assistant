@echo off
chcp 65001 >nul
rem Запуск помощника на Windows. Ollama должна быть запущена (значок в трее).
cd /d %~dp0
if not exist .venv (
  echo Создаю виртуальное окружение...
  py -3 -m venv .venv || python -m venv .venv
)
.venv\Scripts\python -m pip install -q -r requirements.txt
echo.
echo Откройте в браузере: http://127.0.0.1:8000   (логин employee / employee или doc / doc)
echo Первый запуск строит векторный индекс через bge-m3, это 1-3 минуты.
echo.
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
