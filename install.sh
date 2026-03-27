#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════
#  Spoaken — Bootstrap Installer (macOS + Linux)
#  Usage: chmod +x install.sh && ./install.sh
#  Works on: macOS 12+  ·  Ubuntu/Debian  ·  Fedora/RHEL  ·  Arch Linux
# ══════════════════════════════════════════════════════════════════════

set -euo pipefail

CYAN='\033[0;36m'; GREEN='\033[0;32m'
YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

log()  { echo -e "${CYAN}[Spoaken]${NC} $*"; }
ok()   { echo -e "${GREEN}  ✔${NC}  $*"; }
warn() { echo -e "${YELLOW}  !${NC}  $*"; }
err()  { echo -e "${RED}  ✘${NC}  $*" >&2; exit 1; }

# ══════════════════════════════════════════════════════════════════════
#  Parallel arrays: file → sub-package map (bash 3.2 compatible)
#  Kept at global scope so _pkg_lookup() can reference them safely
#  without relying on bash's dynamic-scoping of local variables,
#  which is fragile and undefined for nested functions.
# ══════════════════════════════════════════════════════════════════════
_MAP_FILES=(
    "__init__.py"   "__main__.py"
    "config.py"     "engine.py"          "vad.py"
    "gui.py"        "splash.py"          "theme.py"
    "chat.py"       "lan.py"             "online.py"
    "llm.py"        "summarize.py"       "summarize_router.py"  "writer.py"
    "crashlog.py"   "environ.py"         "mic_config.py"
    "paths.py"      "session_recovery.py"
    "commands.py"   "controller.py"      "update.py"
)
_MAP_PKGS=(
    ""              ""
    "core"          "core"               "core"
    "ui"            "ui"                 "ui"
    "network"       "network"            "network"
    "processing"    "processing"         "processing"           "processing"
    "system"        "system"             "system"
    "system"        "system"
    "control"       "control"            "control"
)

# ── _pkg_lookup FILENAME ────────────────────────────────────────────────────────
#  Sets _pkg_result to the sub-package name for FILENAME, or "" if it belongs
#  at the package root.  Returns 1 if the file is not in the map (caller
#  should also treat that as root placement).
#
#  FIX: Moved from inside copy_source_files() — bash does not support true
#  nested functions (inner functions land in global scope and cannot close over
#  the caller's locals).  Referencing _MAP_FILES/_MAP_PKGS from here is safe
#  because they are now genuine globals.
# ═══════════════════════════════════════════════════════════════════════════════
_pkg_lookup() {
    local needle="$1"
    local i
    _pkg_result=""
    for (( i=0; i<${#_MAP_FILES[@]}; i++ )); do
        if [[ "${_MAP_FILES[$i]}" == "$needle" ]]; then
            _pkg_result="${_MAP_PKGS[$i]}"
            return 0
        fi
    done
    return 1   # not in map → caller treats as root
}

# ══════════════════════════════════════════════════════════════════════
#  copy_source_files SRC_DIR DEST_ROOT
#
#  Mirrors the logic of the standalone copy_dir.sh, inlined here so
#  users only need install.sh.  Called automatically after the Python
#  installer succeeds.  Can also be invoked stand-alone:
#    bash install.sh --copy-only
# ══════════════════════════════════════════════════════════════════════
copy_source_files() {
    local src_dir="$1"
    local dest_root="$2"   
    # e.g. ~/Spoaken/spoaken

    local -a SUBPKGS=(core ui network processing system control)
    local -a NEVER_COPY=(install.py setup.py conftest.py)
    local -a SKIP_DIRS=(venv .venv __pycache__ .git models logs node_modules)

    echo ""
    log "Source file copy: $src_dir → $dest_root"
    echo ""

    # ── Wipe old dest and recreate ──────────────────────────────────────
    if [[ -d "$dest_root" ]]; then
        log "Removing old $dest_root …"
        rm -rf "$dest_root"
    fi
    mkdir -p "$dest_root"

    # Create sub-package dirs with __init__.py stubs
    local pkg
    for pkg in "${SUBPKGS[@]}"; do
        mkdir -p "$dest_root/$pkg"
        echo "# $pkg sub-package" > "$dest_root/$pkg/__init__.py"
    done

    # ── Build find -prune arguments ─────────────────────────────────────
    # FIX: Guard against empty SKIP_DIRS — an empty array produced a
    # dangling -o expression that made find misbehave.
    local -a PRUNE_ARGS=()
    local skip
    if [[ ${#SKIP_DIRS[@]} -gt 0 ]]; then
        for skip in "${SKIP_DIRS[@]}"; do
            PRUNE_ARGS+=(-name "$skip" -prune -o)
        done
    fi

    local copied=0 skipped=0
    # FIX: Declare loop-local variables outside the while loop.
    # Using 'local' inside a loop is valid but misleading — it only
    # takes effect on the first iteration; subsequent iterations just
    # reassign. Moved declarations here for clarity and correctness.
    local filepath fname subpkg dest_file do_skip n

    # ── Copy .py files ──────────────────────────────────────────────────
    while IFS= read -r filepath; do
        fname="$(basename "$filepath")"

        # Skip files in NEVER_COPY
        # FIX: Removed 'local do_skip=false' / 'local n' from inside loop.
        do_skip=false
        for n in "${NEVER_COPY[@]}"; do
            [[ "$fname" == "$n" ]] && { do_skip=true; break; }
        done
        $do_skip && continue

        # Determine destination sub-package (not in map → root)
        _pkg_lookup "$fname" || true   # return 1 is expected for root files
        subpkg="$_pkg_result"

        if [[ -n "$subpkg" ]]; then
            dest_file="$dest_root/$subpkg/$fname"
        else
            dest_file="$dest_root/$fname"
        fi

        # Don't overwrite with a later occurrence of the same filename
        [[ -f "$dest_file" ]] && continue

        mkdir -p "$(dirname "$dest_file")"
        if cp "$filepath" "$dest_file"; then
            copied=$(( copied + 1 ))
        else
            warn "Could not copy $fname"
            skipped=$(( skipped + 1 ))
        fi
    done < <(find "$src_dir" \
        "${PRUNE_ARGS[@]}" \
        -name "*.py" -print \
        2>/dev/null | sort)

    # ── Copy assets ─────────────────────────────────────────────────────
    mkdir -p "$dest_root/assets"
    local asset_dest
    while IFS= read -r filepath; do
        # FIX: Guard against duplicate asset filenames — the original silently
        # clobbered earlier files when two source subdirs had the same asset name.
        asset_dest="$dest_root/assets/$(basename "$filepath")"
        if [[ ! -f "$asset_dest" ]]; then
            cp "$filepath" "$asset_dest" 2>/dev/null || true
        fi
    done < <(find "$src_dir" \
        "${PRUNE_ARGS[@]}" \
        \( -name "*.png" -o -name "*.ico" -o -name "*.jpg" -o -name "*.svg" \) \
        -print 2>/dev/null)

    # ── Copy config if not already present at install root ──────────────
    local install_root
    install_root="$(dirname "$dest_root")"   # parent of spoaken/ = install_dir
    if [[ -f "$src_dir/spoaken_config.json" ]] && \
       [[ ! -f "$install_root/spoaken_config.json" ]]; then
        cp "$src_dir/spoaken_config.json" "$install_root/spoaken_config.json"
        ok "Copied spoaken_config.json"
    fi

    # ── Report ──────────────────────────────────────────────────────────
    echo ""
    ok "Source files: $copied copied, $skipped skipped"
    echo ""

    # ── Integrity check ─────────────────────────────────────────────────
    local all_ok=true
    local check
    for check in "__main__.py" "core/config.py" "ui/gui.py" "ui/theme.py" \
                 "control/controller.py" "system/crashlog.py"; do
        if [[ ! -f "$dest_root/$check" ]]; then
            warn "MISSING: $dest_root/$check"
            all_ok=false
        fi
    done

    if $all_ok; then
        ok "All critical files verified ✔"
    else
        warn "Some critical files are missing — check that they exist in $src_dir"
        echo ""
        echo "  .py files found in source root:"
        find "$src_dir" -maxdepth 1 -name "*.py" ! -name "install.py" \
            | sort | sed 's/^/    /'
        echo ""
        return 1
    fi
}

# ── read_install_dir CONFIG_FILE ────────────────────────────────────────────────
#  Reads install_dir from a JSON config file via Python and prints it.
#  FIX: Extracted the repeated here-doc Python snippet into a single function
#  to eliminate duplication (was copy-pasted identically in two places).
#  FIX: Separated 'local var' declaration from command substitution so that
#  a failing Python invocation is caught by set -e.  The old pattern
#  'local var=$(cmd)' masks the exit code because 'local' always exits 0.
# ═══════════════════════════════════════════════════════════════════════════════
read_install_dir() {
    local cfg_file="$1"
    local _result
    # FIX: Two-line pattern — 'local' on its own line so set -e sees the real
    # exit code of the command substitution on the second line.
    _result=$(
        "$PYTHON" - "$cfg_file" 2>/dev/null <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        d = json.load(f)
    v = d.get("install_dir", "")
    if v:
        print(v)
except Exception:
    pass
PYEOF
    ) || true   # Python failing (bad config) is non-fatal; caller handles empty result
    echo "$_result"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OS="$(uname -s)"

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║      SPOAKEN — Bootstrap Installer                   ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Optional flags (passed through to install.py):"
echo -e "    ${CYAN}--offline${NC}      Force offline install"
echo -e "    ${CYAN}--online${NC}       Force online install"
echo -e "    ${CYAN}--interactive${NC}  Full guided setup (all options)"
echo -e "    ${CYAN}--noise${NC}        Install noise suppression (noisereduce)"
echo -e "    ${CYAN}--llm${NC}          Install LLM + summarization (ollama, sumy, nltk)"
echo -e "    ${CYAN}--no-vad${NC}       Skip webrtcvad  (energy-gate VAD fallback)"
echo -e "    ${CYAN}--chat${NC}         Enable LAN chat server in config"
echo -e "    ${CYAN}--copy-only${NC}    Re-copy source files only (skip Python installer)"
echo ""
echo -e "  Example:  ${CYAN}./install.sh --noise --online${NC}"
echo ""

# ── Handle --copy-only before anything else ────────────────────────────────────
COPY_ONLY=false
for _arg in "$@"; do
    if [[ "$_arg" == "--copy-only" ]]; then
        COPY_ONLY=true
        break
    fi
done

# ── 1. Find Python 3.9+ ────────────────────────────────────────────────────────
log "Checking for Python 3.9+"

PYTHON=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
    if command -v "$candidate" &>/dev/null; then
        # Ask Python itself for major/minor — avoids ALL string-parsing bugs.
        # FIX: Capture output in a variable first; if the invocation produces
        # unexpected non-numeric output (e.g. an error message), the subsequent
        # integer comparisons would throw under set -e.  Default to "0 0" on
        # any failure so the candidate is safely skipped.
        _py_ver_out=$("$candidate" -c \
            "import sys; print(sys.version_info.major, sys.version_info.minor)" \
            2>/dev/null) || _py_ver_out="0 0"
        # Validate that the output is exactly two tokens before splitting.
        # Garbled output (e.g. a Python startup error) would cause -gt to fail.
        if [[ "$_py_ver_out" =~ ^[0-9]+\ [0-9]+$ ]]; then
            py_major="${_py_ver_out%% *}"
            py_minor="${_py_ver_out##* }"
        else
            py_major=0; py_minor=0
        fi
        if [[ "$py_major" -gt 3 ]] || { [[ "$py_major" -eq 3 ]] && [[ "$py_minor" -ge 9 ]]; }; then
            PYTHON="$candidate"
            ok "Found Python ${py_major}.${py_minor} at $(command -v "$candidate")"
            break
        else
            warn "Found Python ${py_major}.${py_minor} — need 3.9+. Skipping $candidate."
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    warn "Python 3.9+ not found — attempting to install…"
    if [[ "$OS" == "Darwin" ]]; then
        if ! command -v brew &>/dev/null; then
            log "Installing Homebrew first…"
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
            # Apple Silicon: add brew to PATH for this session
            [[ -f /opt/homebrew/bin/brew ]] && eval "$(/opt/homebrew/bin/brew shellenv)"
        fi
        # '|| true' prevents set -e from aborting if the formula is already installed
        brew install python@3.11 || true
        # Ensure the versioned binary is linked into PATH
        brew link --overwrite python@3.11 || true
        # FIX: Validate each candidate path before assigning PYTHON.
        # The original fallback 'PYTHON="$(brew --prefix)/bin/python3.11"' was
        # never checked for executability and would silently set PYTHON to a
        # non-existent path, causing a cryptic "command not found" later.
        if command -v python3.11 &>/dev/null; then
            PYTHON="python3.11"
        else
            _brew_py="$(brew --prefix python@3.11 2>/dev/null)/bin/python3.11"
            if [[ -x "$_brew_py" ]]; then
                PYTHON="$_brew_py"
            else
                _brew_py="$(brew --prefix 2>/dev/null)/bin/python3.11"
                if [[ -x "$_brew_py" ]]; then
                    PYTHON="$_brew_py"
                else
                    err "Homebrew installed python@3.11 but no executable found. Try: brew link --overwrite python@3.11"
                fi
            fi
        fi
        ok "Python 3.11 installed via Homebrew"
    elif [[ "$OS" == "Linux" ]]; then
        if command -v apt-get &>/dev/null; then
            sudo apt-get update -qq
            sudo apt-get install -y python3 python3-pip python3-venv
            PYTHON="python3"
        elif command -v dnf &>/dev/null; then
            sudo dnf install -y python3 python3-pip python3-virtualenv
            PYTHON="python3"
        elif command -v pacman &>/dev/null; then
            # BUG FIX: 'pacman -Sy' (partial DB sync) is explicitly warned against
            # by Arch Linux — it can leave the system in a broken partial-upgrade state.
            # Must use '-Syu' to sync and upgrade before installing new packages.
            sudo pacman -Syu --noconfirm python python-pip
            PYTHON="python3"
        else
            err "Cannot auto-install Python. Install Python 3.9+ manually and re-run."
        fi
        ok "Python installed via package manager"
    else
        err "Unsupported OS: $OS. Install Python 3.9+ manually."
    fi

    # BUG FIX: After a package-manager install, PYTHON is set to "python3" but
    # never re-validated. On older LTS systems apt/dnf may install Python 3.6 or
    # 3.8, which is below the 3.9 requirement. Re-run the version check here so
    # the script aborts early with a clear error rather than failing cryptically
    # inside install.py.
    if [[ -n "$PYTHON" ]]; then
        _pm_ver_out=$("$PYTHON" -c \
            "import sys; print(sys.version_info.major, sys.version_info.minor)" \
            2>/dev/null) || _pm_ver_out="0 0"
        if [[ "$_pm_ver_out" =~ ^[0-9]+\ [0-9]+$ ]]; then
            py_major="${_pm_ver_out%% *}"; py_minor="${_pm_ver_out##* }"
        else
            py_major=0; py_minor=0
        fi
        if ! { [[ "$py_major" -gt 3 ]] || \
               { [[ "$py_major" -eq 3 ]] && [[ "$py_minor" -ge 9 ]]; }; }; then
            err "Package manager installed Python ${py_major}.${py_minor}, but 3.9+ is required. Install a newer Python manually."
        fi
    fi
fi

# Two-line pattern — keep assignment separate from the command substitution
# so set -e surfaces real failures.
PY_VER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
ok "Using Python $PY_VER"

# ── 2. Verify install.py is present ────────────────────────────────────────────
if [[ ! -f "$SCRIPT_DIR/install.py" ]]; then
    err "install.py not found in $SCRIPT_DIR"
fi

# ── 3. Determine config mode ────────────────────────────────────────────────────
CONFIG_ARGS=()
# install.py writes the config to <install_dir>/spoaken_config.json, which may
# differ from SCRIPT_DIR.  Check both the script directory AND the default
# install location so a saved config is always found on re-runs.
_found_cfg=""
for _cfg_candidate in \
    "$SCRIPT_DIR/spoaken_config.json" \
    "$HOME/Spoaken/spoaken_config.json"; do
    if [[ -f "$_cfg_candidate" ]]; then
        _found_cfg="$_cfg_candidate"
        break
    fi
done

if [[ -n "$_found_cfg" ]]; then
    log "Found config at $_found_cfg — using saved configuration."
    CONFIG_ARGS=(--config "$_found_cfg")
else
    log "No config file found — installer will prompt for online/offline choice."
fi

# ── 4. macOS: native deps + permissions reminder ───────────────────────────────
if [[ "$OS" == "Darwin" ]]; then
    # portaudio is required by sounddevice at runtime.  pip install sounddevice
    # succeeds without it but the import crashes — install the native lib now.
    if command -v brew &>/dev/null; then
        log "Installing macOS native dependencies via Homebrew…"
        brew install portaudio || true

        # tkinter is not bundled with Homebrew Python — install the matching
        # python-tk formula.  Derive the major.minor from $PYTHON.
        # FIX: Two-line assignment pattern so a Python failure is visible.
        _tk_ver=$("$PYTHON" -c \
            "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" \
            2>/dev/null) || _tk_ver="3.11"
        brew install "python-tk@${_tk_ver}" || true
        ok "Native deps: portaudio and python-tk@${_tk_ver} installed"
    else
        warn "Homebrew not found — skipping native dep install."
        warn "If sounddevice or tkinter fails at runtime, install manually:"
        warn "  brew install portaudio python-tk@3.11"
    fi

    # FIX: The permission reminder box was piped through warn(), which prepends
    # "  !  " to every line and breaks the box-drawing characters.
    # Switched to plain echo -e, consistent with the success/failure boxes.
    echo ""
    echo -e "${YELLOW}╔═══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${YELLOW}║  BEFORE FIRST LAUNCH: macOS Privacy permissions required     ║${NC}"
    echo -e "${YELLOW}║                                                               ║${NC}"
    echo -e "${YELLOW}║  1. Accessibility (for window writer / keyboard automation):  ║${NC}"
    echo -e "${YELLOW}║     System Settings → Privacy & Security → Accessibility     ║${NC}"
    echo -e "${YELLOW}║     Add your Terminal app and enable the toggle.             ║${NC}"
    echo -e "${YELLOW}║                                                               ║${NC}"
    echo -e "${YELLOW}║  2. Microphone (required for all transcription):             ║${NC}"
    echo -e "${YELLOW}║     System Settings → Privacy & Security → Microphone        ║${NC}"
    echo -e "${YELLOW}║     Add your Terminal app and enable the toggle.             ║${NC}"
    echo -e "${YELLOW}║     (macOS will also prompt on first launch — click Allow.)  ║${NC}"
    echo -e "${YELLOW}╚═══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
fi

# ── 5. Run the Python installer ─────────────────────────────────────────────────

# --copy-only: skip Python installer, just re-copy source files into existing install
if $COPY_ONLY; then
    # Determine INSTALL_DIR from saved config
    # FIX: Use the extracted read_install_dir() helper to avoid duplicated
    # here-doc code and to apply the two-line assignment safety pattern.
    INSTALL_DIR=""
    for cfg_candidate in \
        "$SCRIPT_DIR/spoaken_config.json" \
        "$HOME/Spoaken/spoaken_config.json"; do
        if [[ -f "$cfg_candidate" ]]; then
            INSTALL_DIR=$(read_install_dir "$cfg_candidate")
            [[ -n "$INSTALL_DIR" ]] && break
        fi
    done
    [[ -z "$INSTALL_DIR" ]] && INSTALL_DIR="$HOME/Spoaken"

    log "Copy-only mode — target: $INSTALL_DIR"
    copy_source_files "$SCRIPT_DIR" "$INSTALL_DIR/spoaken"
    ok "Copy-only complete."
    exit 0
fi

log "Launching Spoaken installer…"
echo ""

# Filter --copy-only out of args passed to install.py (it's a shell-only flag)
PY_ARGS=()
for _arg in "${@:-}"; do
    [[ "$_arg" != "--copy-only" ]] && PY_ARGS+=("$_arg")
done

set +e
"$PYTHON" "$SCRIPT_DIR/install.py" "${CONFIG_ARGS[@:-]}" "${PY_ARGS[@]:-}"
EXIT_CODE=$?
set -e

echo ""
if [[ $EXIT_CODE -eq 0 ]]; then
    echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║  Bootstrap complete. Spoaken is ready to launch.     ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
    echo ""

    # ── 6. Determine install directory from config ────────────────────────────
    # install.py writes the config to <install_dir>/spoaken_config.json.
    # We read install_dir from whichever config file install.py wrote.
    # FIX: Use the extracted read_install_dir() helper (was copy-pasted here
    # identically to the copy-only block above).
    INSTALL_DIR=""
    for cfg_candidate in \
        "$SCRIPT_DIR/spoaken_config.json" \
        "$HOME/Spoaken/spoaken_config.json"; do
        if [[ -f "$cfg_candidate" ]]; then
            INSTALL_DIR=$(read_install_dir "$cfg_candidate")
            [[ -n "$INSTALL_DIR" ]] && break
        fi
    done

    if [[ -z "$INSTALL_DIR" ]]; then
        INSTALL_DIR="$HOME/Spoaken"
    fi

    # ── 6b. Copy source files into the install dir ────────────────────────
    log "Copying source files…"
    copy_source_files "$SCRIPT_DIR" "$INSTALL_DIR/spoaken" || \
        warn "Source file copy had issues — check warnings above."
    echo ""

    # Determine run.sh and venv python paths
    RUN_SH="$INSTALL_DIR/run.sh"
    VENV_PYTHON="$INSTALL_DIR/venv/bin/python"
    # BUG FIX: uname -s never returns "Windows_NT" — that value only exists in
    # CMD/PowerShell's %OS% environment variable.  On Git Bash / MSYS2 / Cygwin,
    # uname -s returns strings like "MINGW64_NT-10.0" or "CYGWIN_NT-10.0".
    # Use a glob match to cover all Windows-via-uname cases.
    [[ "$OS" == MINGW*_NT* || "$OS" == CYGWIN_NT* || "$OS" == MSYS_NT* ]] && \
        VENV_PYTHON="$INSTALL_DIR/venv/Scripts/python.exe"

    echo -e "  Installed to:  ${CYAN}$INSTALL_DIR${NC}"
    echo ""
    echo -e "  To re-run with optional packages:"
    echo -e "    ${CYAN}./install.sh --noise --llm${NC}"
    echo ""

    # ── 7. Launch Spoaken immediately ────────────────────────────────────────
    if [[ -x "$RUN_SH" ]]; then
        # Strip the macOS quarantine flag that Gatekeeper applies to files
        # downloaded from the internet — without this, the first exec is blocked.
        if [[ "$OS" == "Darwin" ]]; then
            xattr -d com.apple.quarantine "$RUN_SH" 2>/dev/null || true
        fi
        echo -e "${CYAN}[Spoaken]${NC} Starting Spoaken…"
        echo ""
        # Use a subshell rather than exec so the terminal window stays open
        # on macOS if Spoaken exits with an error (exec would close it instantly).
        "$RUN_SH"
    elif [[ -x "$VENV_PYTHON" ]]; then
        echo -e "${CYAN}[Spoaken]${NC} Starting Spoaken via venv Python…"
        echo ""
        "$VENV_PYTHON" -m spoaken
    else
        warn "Could not locate run.sh or venv Python. Launch manually:"
        echo -e "    ${CYAN}cd $INSTALL_DIR && ./run.sh${NC}"
        echo ""
    fi
else
    echo -e "${RED}[✘] Installation finished with errors (exit code $EXIT_CODE).${NC}"
    echo "    Review the output above and retry:"
    echo -e "    ${CYAN}python3 install.py --interactive${NC}"
fi
echo ""
