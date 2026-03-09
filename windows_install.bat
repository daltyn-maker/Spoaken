@echo off
:: ══════════════════════════════════════════════════════════════════════
::  Spoaken — Windows Bootstrap Installer
::  Usage: Double-click or run from an Administrator Command Prompt
::  Works on: Windows 10 (1903+) / Windows 11
:: ══════════════════════════════════════════════════════════════════════
setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
:: Strip trailing backslash
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

echo.
echo  +======================================================+
echo  ^|      SPOAKEN -- Windows Bootstrap Installer          ^|
echo  +======================================================+
echo.
echo   Optional flags (passed through to install.py):
echo     --noise        Install noise suppression (noisereduce)
echo     --translation  Install translation support (deep-translator)
echo     --llm          Install LLM + summarization (ollama, sumy, nltk)
echo     --no-vad       Skip webrtcvad  (use energy-gate fallback)
echo     --chat         Enable LAN chat server in config
echo     --offline      Force offline install
echo.
echo   Example:  windows_install.bat --noise --translation
echo.

:: ── 1. Find Python 3.9+ ────────────────────────────────────────────────────
echo [Spoaken] Checking for Python 3.9+...

set "PYTHON="
set "PYNUM="

:: Try py launcher first (most reliable on Windows)
where py >nul 2>&1
if %errorlevel%==0 (
    for /f "tokens=*" %%v in ('py -3 -c "import sys; print(sys.version_info.major*100+sys.version_info.minor)" 2^>nul') do set "PYNUM=%%v"
    if defined PYNUM (
        if !PYNUM! GEQ 309 (
            set "PYTHON=py -3"
            for /f "tokens=*" %%v in ('py -3 --version 2^>nul') do echo   [OK] Found %%v via py launcher
            goto :found_python
        ) else (
            echo   [!] py launcher found Python but version is too old. Need 3.9+.
        )
    )
)

:: Try python3 / python in PATH
for %%c in (python3 python) do (
    if not defined PYTHON (
        where %%c >nul 2>&1
        if !errorlevel!==0 (
            set "PYNUM="
            for /f "tokens=*" %%v in ('%%c -c "import sys; print(sys.version_info.major*100+sys.version_info.minor)" 2^>nul') do set "PYNUM=%%v"
            if defined PYNUM (
                if !PYNUM! GEQ 309 (
                    set "PYTHON=%%c"
                    for /f "tokens=*" %%v in ('%%c --version 2^>nul') do echo   [OK] Found %%v
                    goto :found_python
                ) else (
                    echo   [!] Found %%c but version is too old. Need 3.9+.
                )
            )
        )
    )
)

:: Python not found — try winget
echo   [!] Python 3.9+ not found. Attempting to install via winget...
where winget >nul 2>&1
if %errorlevel% neq 0 (
    echo   [X] winget not available.
    echo       Please install Python 3.11+ from https://www.python.org/downloads/
    echo       Then re-run this script.
    goto :error
)

winget install --id Python.Python.3.11 -s winget --silent --accept-package-agreements --accept-source-agreements
if %errorlevel% neq 0 (
    echo   [X] winget failed to install Python.
    echo       Please install manually from https://www.python.org/downloads/
    goto :error
)

echo   [OK] Python 3.11 installed via winget.
echo   Please restart this script so the new Python is visible in PATH.
pause
exit /b 0

:found_python

:: Confirm version
for /f "tokens=*" %%v in ('!PYTHON! -c "import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\")" 2^>nul') do (
    echo [Spoaken] Using Python %%v
)

:: ── 2. Ensure install.py is present ───────────────────────────────────────────
if not exist "%SCRIPT_DIR%\install.py" (
    echo   [X] install.py not found in %SCRIPT_DIR%
    echo       Please place install.py alongside this script.
    goto :error
)

:: ── 3. Determine config mode ──────────────────────────────────────────────────
set "CONFIG_ARGS="
if exist "%SCRIPT_DIR%\spoaken_config.json" (
    echo [Spoaken] Found spoaken_config.json -- using saved configuration.
    set "CONFIG_ARGS=--config "%SCRIPT_DIR%\spoaken_config.json""
) else (
    echo [Spoaken] No config file found. Launching interactive setup.
    set "CONFIG_ARGS=--interactive"
)

:: ── 4. Run the Python installer ────────────────────────────────────────────────
echo.
echo [Spoaken] Launching Spoaken installer...
echo.

!PYTHON! "%SCRIPT_DIR%\install.py" %CONFIG_ARGS% %*
set "EXIT_CODE=%errorlevel%"

echo.
if %EXIT_CODE%==0 (
    echo  +======================================================+
    echo  ^|  Bootstrap complete. Spoaken is ready to use.        ^|
    echo  +======================================================+
    echo.
    echo   Launch with:  python spoaken\spoaken_main.py
    echo.
    echo   To add optional packages later, re-run with flags, e.g.:
    echo     windows_install.bat --noise --translation --llm
    echo   Or install from the Spoaken Update window.
) else (
    echo   [X] Installation finished with errors (exit code %EXIT_CODE%^).
    echo       Review the output above and retry with:
    echo       python install.py --interactive
)

echo.
pause
exit /b %EXIT_CODE%

:error
echo.
echo   [X] Installation aborted.
echo.
pause
exit /b 1
