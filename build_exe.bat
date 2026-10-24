@echo off
REM Builds dist\ISO-to-MP4.exe (single file, no console window).
REM Put ffmpeg.exe and ffprobe.exe next to this file first to bundle them inside the exe;
REM otherwise the app offers to download ffmpeg on first run.
py -m pip install -r requirements.txt pyinstaller || exit /b 1
set BIN=
if exist ffmpeg.exe if exist ffprobe.exe set BIN=--add-binary "ffmpeg.exe;." --add-binary "ffprobe.exe;."
py -m PyInstaller --onefile --windowed --name ISO-to-MP4 %BIN% iso2mp4.py
echo.
echo Done: dist\ISO-to-MP4.exe
