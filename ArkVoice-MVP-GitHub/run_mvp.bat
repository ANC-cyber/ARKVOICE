@echo off
setlocal
set "ARKVOICE_MVP_ROOT=%~dp0"
if exist "%~dp0settings.local.bat" call "%~dp0settings.local.bat"
if not defined ARKVOICE_SOVITS_DIR (
  echo Configure settings.local.bat first. See settings.local.example.bat.
  pause
  exit /b 1
)
set "ARKVOICE_PYTHON=%ARKVOICE_SOVITS_DIR%\runtime\python.exe"
if not exist "%ARKVOICE_PYTHON%" (
  echo GPT-SoVITS Python not found: "%ARKVOICE_PYTHON%"
  pause
  exit /b 1
)
if not exist "%ARKVOICE_TTS_CONFIG%" (
  echo GPT-SoVITS v5turbo config not found: "%ARKVOICE_TTS_CONFIG%"
  pause
  exit /b 1
)
if not defined ARKVOICE_REFERENCE_TEXT_FILE set "ARKVOICE_REFERENCE_TEXT_FILE=%~dp0voices\reference.txt"
if not exist "%ARKVOICE_REFERENCE_TEXT_FILE%" (
  echo Create voices\reference.txt from voices\reference.txt.example and enter the exact UTF-8 transcript.
  pause
  exit /b 1
)
if not exist "%ARKVOICE_REFERENCE_AUDIO%" (
  echo Reference audio not found: "%ARKVOICE_REFERENCE_AUDIO%"
  pause
  exit /b 1
)
cd /d "%~dp0"
"%ARKVOICE_PYTHON%" -I "%~dp0console_app.py"
pause