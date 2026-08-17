@echo off
REM Run the test suite on Windows (no Qwen credentials required).
cd /d "%~dp0.."
.venv\Scripts\python -m pytest %*
