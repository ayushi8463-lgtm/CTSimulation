@echo off
setlocal enableextensions
rem Usage: run_cpp_server.bat [port]     (default 5000)
rem Optional: set PYTHON_BIN=C:\path\to\python.exe before running.
set "PORT=%~1"
if "%PORT%"=="" set "PORT=5000"
if "%PYTHON_BIN%"=="" set "PYTHON_BIN=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_BIN%" (
  echo Could not find %PYTHON_BIN%
  echo Create the venv first:  python -m venv .venv
  exit /b 1
)
cd /d "%~dp0"

rem --- find the MSVC compiler ---
where cl >nul 2>nul
if not errorlevel 1 goto have_cl

set "VCVARS="
set "PF86=%ProgramFiles(x86)%"
set "VSWHERE=%PF86%\Microsoft Visual Studio\Installer\vswhere.exe"
if exist "%VSWHERE%" (
  for /f "usebackq delims=" %%i in (`"%VSWHERE%" -latest -products * -property installationPath`) do (
    if exist "%%i\VC\Auxiliary\Build\vcvars64.bat" set "VCVARS=%%i\VC\Auxiliary\Build\vcvars64.bat"
  )
)
if not defined VCVARS (
  for %%d in ("%PF86%" "%ProgramFiles%") do (
    for %%e in (BuildTools Community Professional Enterprise) do (
      if exist "%%~d\Microsoft Visual Studio\2022\%%e\VC\Auxiliary\Build\vcvars64.bat" set "VCVARS=%%~d\Microsoft Visual Studio\2022\%%e\VC\Auxiliary\Build\vcvars64.bat"
    )
  )
)
if not defined VCVARS (
  echo Could not find vcvars64.bat. Looked via vswhere in "%PF86%" and in the default VS 2022 folders.
  echo Open the "Visual Studio Installer" app, choose Modify on Build Tools, and tick
  echo "Desktop development with C++". Then rerun this script.
  exit /b 1
)
echo Using %VCVARS%
call "%VCVARS%" >nul
:have_cl

"%PYTHON_BIN%" win_python_info.py || exit /b 1
call win_env.bat

cl /nologo /std:c++17 /O2 /EHsc /utf-8 /DNOMINMAX /I"%PY_INCLUDE%" cpp_server_win.cpp /Fe:ct_simulator_server.exe /link /LIBPATH:"%PY_LIBS%" ws2_32.lib
if errorlevel 1 exit /b 1

rem --- runtime environment for the embedded Python ---
set "PYTHONHOME=%PY_BASE%"
set "PYTHONPATH=%CD%"
set "PYTHONUTF8=1"
set "PATH=%PY_BASE%;%PY_BASE%\DLLs;%PATH%"
set "CT_SIMULATOR_LOCAL=1"
set "CT_PYTHON_SITE=%PY_SITE%"
ct_simulator_server.exe %PORT%
