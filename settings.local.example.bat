@echo off
rem Copy to settings.local.bat and edit these paths on your own computer.
set "ARKVOICE_SOVITS_DIR=C:\Path\To\GPT-SoVITS"
set "ARKVOICE_TTS_CONFIG=C:\Path\To\GPT-SoVITS\configs\v5turbo.yaml"
set "ARKVOICE_REFERENCE_AUDIO=C:\Path\To\your-authorized-reference.wav"
set "ARKVOICE_REFERENCE_TEXT_FILE=%~dp0voices\reference.txt"
