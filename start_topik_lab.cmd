@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 exit /b 1

if not exist ".venv\Scripts\python.exe" (
    echo Project Python environment not found. Run setup_topik_lab.ps1 first.
    goto failed
)

echo Starting the local Bank server...
".venv\Scripts\python.exe" -m topik_question_lab.maintenance.bank_server start
if not "%errorlevel%"=="0" goto failed

echo Starting TOPIK Question Lab...
".venv\Scripts\python.exe" -m streamlit run topik_question_lab\main.py
if not "%errorlevel%"=="0" goto failed

popd
exit /b 0

:failed
echo Startup failed. Check the message above.
popd
pause
exit /b 1
