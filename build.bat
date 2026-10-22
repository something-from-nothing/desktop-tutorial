@echo off
REM Builds dist\DeskAgent\DeskAgent.exe with the llama.cpp AI engine inside it.
REM Needs Python 3.12 from python.org (tick "Add python.exe to PATH") and an internet connection.
py -3.12 -m venv .venv || goto :error
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt || goto :error
pyinstaller --noconfirm --onedir --windowed --name DeskAgent --icon icon.ico ^
  --collect-data jsonschema_specifications ^
  deskagent.py || goto :error
echo Downloading the llama.cpp engine...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$r = Invoke-RestMethod https://api.github.com/repos/ggml-org/llama.cpp/releases/latest;" ^
  "$a = $r.assets | Where-Object { $_.name -like '*bin-win-cpu-x64.zip' } | Select-Object -First 1;" ^
  "if (-not $a) { $a = $r.assets | Where-Object { $_.name -like '*bin-win-avx2-x64.zip' } | Select-Object -First 1 };" ^
  "Invoke-WebRequest $a.browser_download_url -OutFile llama.zip;" ^
  "Expand-Archive llama.zip -DestinationPath llama_tmp -Force;" ^
  "$exe = Get-ChildItem llama_tmp -Recurse -Filter llama-server.exe | Select-Object -First 1;" ^
  "New-Item -ItemType Directory -Force dist\DeskAgent\llama | Out-Null;" ^
  "Copy-Item (Join-Path $exe.DirectoryName '*') dist\DeskAgent\llama -Recurse -Force;" ^
  "Remove-Item llama.zip, llama_tmp -Recurse -Force" || goto :error
echo.
echo Done. Your app is dist\DeskAgent\DeskAgent.exe (keep the whole DeskAgent folder together).
goto :eof
:error
echo Build failed.
exit /b 1
