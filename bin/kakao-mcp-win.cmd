@echo off
rem kakao-mcp for Windows from the project venv:
rem   kakao-mcp-win --http     daemon (start it from the logon task, see scripts\install-daemon-win.ps1)
rem   kakao-mcp-win --bridge   stdio relay to the daemon
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%"
"%ROOT%\.venv\Scripts\python.exe" -m kkt.win.server %*
