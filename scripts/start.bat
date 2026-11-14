@echo off
rem Niulai Quant launcher (Windows)
rem
rem   scripts\start.bat          create venv / install deps on first run, check config, start web + scheduler (127.0.0.1 only)
rem   scripts\start.bat run      run the full pipeline once; other args are passed to python -m qfpy
rem   scripts\start.bat test     run unit tests
rem
rem Requires Python 3.11+ (tick "py launcher" and "Add to PATH" when installing).
rem Optional env vars: QF_CONFIG, QF_GUSHI_KEY, QF_DINGTALK_WEBHOOK, QF_DINGTALK_SECRET
rem This file is ASCII only with CRLF line endings on purpose: cmd.exe on Chinese Windows reads .bat files as GBK.
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."

if exist ".venv\Scripts\python.exe" goto :deps
echo [niulai] creating virtual env .venv ...
py -3 -m venv .venv
if exist ".venv\Scripts\python.exe" goto :deps
python -m venv .venv
if exist ".venv\Scripts\python.exe" goto :deps
echo [niulai] ERROR: could not create .venv. Install Python 3.11+ from python.org and tick "Add python.exe to PATH".
pause
exit /b 1

:deps
if exist ".venv\.req-stamp" goto :run
echo [niulai] installing dependencies ...
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements-dev.txt
if errorlevel 1 goto :depfail
echo ok> ".venv\.req-stamp"
goto :run

:depfail
echo [niulai] ERROR: pip install failed. Check the network, or set a mirror:
echo   .venv\Scripts\python.exe -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
pause
exit /b 1

:run
set "QF=.venv\Scripts\python.exe -m qfpy"
if defined QF_CONFIG set "QF=%QF% -c %QF_CONFIG%"

set "CMD=%~1"
if "%CMD%"=="" set "CMD=serve"

if /i "%CMD%"=="test" goto :test
if /i "%CMD%"=="serve" goto :serve
%QF% %*
exit /b %errorlevel%

:test
".venv\Scripts\python.exe" -m pytest -q tests
exit /b %errorlevel%

:serve
%QF% check-config
if errorlevel 1 goto :cfgfail
echo [niulai] web UI: http://127.0.0.1:8081  (close this window to stop)
%QF% serve %2 %3 %4 %5 %6
exit /b %errorlevel%

:cfgfail
echo [niulai] config check failed, see messages above.
pause
exit /b 1
