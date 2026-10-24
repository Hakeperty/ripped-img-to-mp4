@echo off
REM Double-click to launch without building the .exe (needs Python 3 from python.org).
py -m pip install --quiet -r "%~dp0requirements.txt"
start "" pyw "%~dp0iso2mp4.py"
