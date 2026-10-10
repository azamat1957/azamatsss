@echo off
cd /d "%~dp0"
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
python -m pip install -U pip
python -m pip install -U -r requirements.txt
echo.
echo PyTubeFix, yt-dlp, Redis va boshqa backend paketlari yangilandi.
pause
