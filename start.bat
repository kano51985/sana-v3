@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo  === Sana Agent - Web Panel ===
echo.

REM Close stale Sana Streamlit instances so the browser reconnects to the new process.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'streamlit.exe') -and $_.CommandLine -like '*streamlit_app.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Seconds 1"

set "_DEEPSEEK_PROMPTED=0"
if "%DEEPSEEK_API_KEY%"=="" (
    for /f "tokens=2*" %%A in ('reg query "HKCU\Environment" /v DEEPSEEK_API_KEY 2^>nul ^| findstr /i "DEEPSEEK_API_KEY"') do set "DEEPSEEK_API_KEY=%%B"
)
if "%DEEPSEEK_API_KEY%"=="" (
    set /p DEEPSEEK_API_KEY="[Input your DeepSeek API Key]: "
    set "_DEEPSEEK_PROMPTED=1"
)
if "%_DEEPSEEK_PROMPTED%"=="1" if not "%DEEPSEEK_API_KEY%"=="" (
    setx DEEPSEEK_API_KEY "%DEEPSEEK_API_KEY%" >nul
)
if "%DEEPSEEK_API_KEY%"=="" (
    echo WARNING: DEEPSEEK_API_KEY is not set.
) else (
    echo DEEPSEEK_API_KEY loaded.
)
echo.
echo Launching...
venv\Scripts\python -m streamlit run interfaces\streamlit_app.py --server.port 8501
pause
