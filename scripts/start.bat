@echo off
REM Start the Qwen Token Gateway on Windows.
cd /d "%~dp0.."
if not exist .venv\Scripts\python.exe (
  echo Virtual environment missing. Run scripts\setup.bat first.
  exit /b 1
)
.venv\Scripts\python -m app
