@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo  === Sana Agent - Web Panel ===
echo.

set /p DEEPSEEK_API_KEY="[Input your DeepSeek API Key]: "
echo.
echo Launching...
venv\Scripts\streamlit run interfaces\streamlit_app.py
pause
