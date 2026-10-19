@echo off
cd /d "%~dp0"
py -3 -c "import PySide6" 2>nul
if errorlevel 1 (
  echo 正在安装界面库 PySide6……
  py -3 -m pip install -r requirements.txt
  if errorlevel 1 python -m pip install -r requirements.txt
)
py -3 dlp.py
if errorlevel 1 python dlp.py
if errorlevel 1 pause
