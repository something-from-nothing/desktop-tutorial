@echo off
REM Builds dist\DeskAgent.exe. Needs Python 3.12 from python.org (tick "Add python.exe to PATH").
py -3.12 -m venv .venv || goto :error
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt || goto :error
pyinstaller --noconfirm --onefile --windowed --name DeskAgent ^
  --collect-data jsonschema_specifications ^
  deskagent.py || goto :error
echo.
echo Done. Your app is dist\DeskAgent.exe
goto :eof
:error
echo Build failed.
exit /b 1
