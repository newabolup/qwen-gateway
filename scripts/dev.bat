@echo off
REM Development mode on Windows: reloading API plus the Vite UI.
cd /d "%~dp0.."
start "Qwen Gateway API" .venv\Scripts\uvicorn app.main:app --host 0.0.0.0 --port 8787 --reload
if exist frontend\node_modules (
  start "Qwen Gateway UI" cmd /c npm --prefix frontend run dev
  echo Admin UI (dev): http://localhost:5173
) else (
  echo frontend\node_modules missing; run: npm --prefix frontend install
)
echo API: http://localhost:8787   Docs: http://localhost:8787/docs
