@echo off
setlocal EnableDelayedExpansion
title Spoaken Installer
color 0B

echo.
echo  ============================================================
echo   SPOAKEN — Windows Bootstrap Installer
echo  ============================================================
echo.
echo  Optional flags you can pass to this script:
echo    --noise        Install noise suppression (noisereduce)
echo    --translation  Install translation support (deep-translator)
echo    --llm          Install LLM + summarization (ollama, sumy, nltk)
echo    --no-vad       Skip webrtcvad  (use energy-gate fallback)
echo    --chat         Enable LAN chat server in config
echo.
echo  Example:  windows_install.bat --noise --translation
echo.

:: ── Check for Admin rights ────────────────────────────────────────────────────
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo  [!] This installer works best with Administrator privileges.
    echo  [!] Some steps may fail without them.
    echo  [!] Right-click this file and select "Run as administrator" for best results.
    echo.
    pause
)

:: ── Try to find Python 3.9+ ───────────────────────────────────────────────────
set PYTHON_EXE=
for %%p in (python3.exe python.exe py.exe) do (
    where %%p >nul 2>&1
    if !errorlevel! equ 0 (
        for /f "tokens=2 delims= " %%v in ('%%p --version 2^>^&1') do (
            set PY_VER=%%v
        )
        :: Check major.minor >= 3.9 using arithmetic comparison (avoids lexicographic bug)
        for /f "tokens=1,2 delims=." %%a in ("!PY_VER!") do (
            if %%a equ 3 (
                set /A _MINOR=%%b
                if !_MINOR! geq 9 (
                    set PYTHON_EXE=%%p
                    echo  [ok] Found Python !PY_VER! at %%p
                    goto :python_found
                )
            )
            if %%a gtr 3 (
                set PYTHON_EXE=%%p
                echo  [ok] Found Python !PY_VER! at %%p
                goto :python_found
            )
        )
        echo  [!] Found Python !PY_VER! but Spoaken needs 3.9+
    )
)

:: ── Python not found — install via winget ────────────────────────────────────
echo  [*] Python 3.9+ not found. Installing via winget...
where winget >nul 2>&1
if %errorlevel% neq 0 (
    echo  [X] winget is not available on this system.
    echo.
    echo  Please download Python 3.11 from: https://www.python.org/downloads/
    echo  Make sure to check "Add Python to PATH" during installation,
    echo  then re-run this script.
    pause
    exit /b 1
)

winget install --id Python.Python.3.11 ^
    --silent ^
    --scope machine ^
    --accept-package-agreements ^
    --accept-source-agreements

if %errorlevel% neq 0 (
    echo  [X] winget failed to install Python.
    echo  Please download from https://www.python.org/downloads/ and retry.
    pause
    exit /b 1
)

echo  [ok] Python installed. Refreshing PATH...

:: Refresh PATH so we can find the new Python immediately
for /f "tokens=*" %%i in ('powershell -NoProfile -Command "[System.Environment]::GetEnvironmentVariable(\"Path\",\"Machine\")"') do set PATH=%%i;%PATH%

set PYTHON_EXE=python

:python_found
echo.

:: ── Verify install.py is present ─────────────────────────────────────────────
if not exist "%~dp0install.py" (
    echo  [X] install.py not found in %~dp0
    echo  Please ensure install.py is in the same folder as this script.
    pause
    exit /b 1
)

:: ── Check for spoaken_config.json ─────────────────────────────────────────────
set CONFIG_ARG=
if exist "%~dp0spoaken_config.json" (
    echo  [*] Found spoaken_config.json — using saved configuration.
    set CONFIG_ARG=--config "%~dp0spoaken_config.json"
) else (
    echo  [*] No config file found. Will run interactive setup.
    set CONFIG_ARG=--interactive
)

:: ── Collect any extra flags passed to this script (--noise, --llm, etc.) ──────
set EXTRA_FLAGS=%*

:: ── Run the Python installer ──────────────────────────────────────────────────
echo  [*] Launching Spoaken installer...
echo.
%PYTHON_EXE% "%~dp0install.py" %CONFIG_ARG% %EXTRA_FLAGS%

if %errorlevel% equ 0 (
    echo.
    echo  ============================================================
    echo   Installation finished. Press any key to exit.
    echo  ============================================================
    echo.
    echo  To add optional features later, re-run with flags, e.g.:
    echo    windows_install.bat --noise --translation --llm
) else (
    echo.
    echo  [X] Installation encountered errors. See output above.
    echo  You can retry with:  python install.py --interactive
)

pause
endlocal
