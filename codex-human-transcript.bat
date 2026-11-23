@echo off
py -3 -V >nul 2>&1
if errorlevel 1 goto python
py -3 -X utf8 "%~dp0codex-human-transcript" %*
exit /b %errorlevel%

:python
python -X utf8 "%~dp0codex-human-transcript" %*
exit /b %errorlevel%
