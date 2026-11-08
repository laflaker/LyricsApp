@echo off
if not exist "%~dp0LyricsApp.vbs" (
    echo LyricsApp.vbs not found next to this file.
    pause
    exit /b 1
)
start "" wscript.exe "%~dp0LyricsApp.vbs"
exit /b
