@echo off
cd /d "%~dp0"
if not exist ".venv" (
    echo Creating Python virtual environment...
    py -3.9 -m venv .venv
    call .venv\Scripts\activate.bat
    python -m pip install --upgrade pip
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)
python bot.py
