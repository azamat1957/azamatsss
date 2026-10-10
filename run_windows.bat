@echo off
setlocal
cd /d "%~dp0"
title UZ NAVO BOT Backend V3

echo ==========================================
echo   UZ NAVO BOT - Backend V3 setup + run
echo ==========================================

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python topilmadi.
  echo Python 3.11+ o'rnating va "Add Python to PATH" ni belgilang.
  pause
  exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo [INFO] FFmpeg topilmadi. Winget orqali o'rnatishga urinaman...
  where winget >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] winget ham topilmadi. FFmpeg ni qo'lda o'rnating.
    pause
    exit /b 1
  )
  winget install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements
  echo.
  echo FFmpeg o'rnatildi. Terminalni yopib, run_windows.bat ni qayta oching.
  pause
  exit /b 0
)

if not exist .venv (
  echo [1/3] Virtual environment yaratilmoqda...
  python -m venv .venv
)

call .venv\Scripts\activate

echo [2/3] Python paketlari o'rnatilmoqda/yangilanmoqda...
python -m pip install -U pip
pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] Paketlarni o'rnatib bo'lmadi. Internetni tekshiring.
  pause
  exit /b 1
)

if not exist .env (
  copy .env.example .env >nul
  echo [INFO] .env yaratildi. BOT_TOKEN va kanal sozlamalarini kiriting.
  notepad .env
  pause
)

echo [3/3] Backend ishga tushmoqda...
python run_backend.py
pause
