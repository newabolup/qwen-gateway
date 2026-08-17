@echo off
REM One-time setup for Windows: virtualenv, dependencies, .env, admin UI.
setlocal
cd /d "%~dp0.."

echo ==^> Creating virtual environment (.venv)
if not exist .venv (python -m venv .venv || goto :error)

echo ==^> Installing Python dependencies
.venv\Scripts\python -m pip install --quiet --upgrade pip || goto :error
.venv\Scripts\pip install --quiet -r requirements-dev.txt || goto :error

if not exist .env (
  echo ==^> Creating .env from .env.example
  copy /y .env.example .env >nul
  for /f "delims=" %%K in ('.venv\Scripts\python -m app.cli generate-key') do set "GWKEY=%%K"
  for /f "delims=" %%P in ('.venv\Scripts\python -c "import secrets;print(secrets.token_urlsafe(18))"') do set "GWPASS=%%P"
  call :setenvvar GATEWAY_SECRET_KEY "%%GWKEY%%"
  call :setenvvar ADMIN_PASSWORD "%%GWPASS%%"
  echo     Generated GATEWAY_SECRET_KEY and ADMIN_PASSWORD in .env
) else (
  echo ==^> .env already exists (left untouched)
)

where npm >nul 2>&1
if %errorlevel%==0 (
  echo ==^> Building the admin UI
  call npm --prefix frontend install --no-audit --no-fund
  call npm --prefix frontend run build
) else (
  echo ==^> npm not found; skipping UI build (the API still works)
)

if not exist data mkdir data
echo.
echo Setup complete. Start the gateway with: scripts\start.bat
goto :eof

:setenvvar
.venv\Scripts\python -c "import sys,pathlib;k,v=sys.argv[1],sys.argv[2];p=pathlib.Path('.env');t=p.read_text().splitlines();p.write_text('\n'.join((k+'='+v) if l.startswith(k+'=') else l for l in t)+'\n')" %1 %~2
goto :eof

:error
echo Setup failed.
exit /b 1
