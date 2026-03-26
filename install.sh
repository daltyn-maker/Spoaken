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
#  copy_source_files SRC_DIR DEST_ROOT
#
#  Mirrors the logic of the standalone copy_dir.sh, inlined here so
#  users only need install.sh.  Called automatically after the Python
#  installer succeeds.  Can also be invoked stand-alone:
#    bash install.sh --copy-only
# ══════════════════════════════════════════════════════════════════════
copy_source_files() {
    local src_dir="$1"
    local dest_root="$2"   # e.g. ~/Spoaken/spoaken

    # ── File → sub-package map ──────────────────────────────────────────
    declare -A PKG_MAP=(
        ["__init__.py"]=""          ["__main__.py"]=""
        ["config.py"]="core"        ["engine.py"]="core"        ["vad.py"]="core"
        ["gui.py"]="ui"             ["splash.py"]="ui"
        ["chat.py"]="network"       ["lan.py"]="network"        ["online.py"]="network"
        ["llm.py"]="processing"     ["summarize.py"]="processing"
        ["summarize_router.py"]="processing"                    ["writer.py"]="processing"
        ["crashlog.py"]="system"    ["environ.py"]="system"     ["mic_config.py"]="system"
        ["paths.py"]="system"       ["session_recovery.py"]="system"
        ["commands.py"]="control"   ["controller.py"]="control" ["update.py"]="control"
    )

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
    local -a PRUNE_ARGS=()
    local skip
    for skip in "${SKIP_DIRS[@]}"; do
        PRUNE_ARGS+=(-name "$skip" -prune -o)
    done

    local copied=0 skipped=0
    local filepath fname subpkg dest_file

    # ── Copy .py files ──────────────────────────────────────────────────
    while IFS= read -r filepath; do
        fname="$(basename "$filepath")"

        # Skip files in NEVER_COPY
        local do_skip=false
        local n
        for n in "${NEVER_COPY[@]}"; do
            [[ "$fname" == "$n" ]] && { do_skip=true; break; }
        done
        $do_skip && continue

        # Determine destination sub-package (missing key → root)
        if [[ -v PKG_MAP[$fname] ]]; then
            subpkg="${PKG_MAP[$fname]}"
        else
            subpkg=""
        fi

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
    while IFS= read -r filepath; do
        cp "$filepath" "$dest_root/assets/$(basename "$filepath")" 2>/dev/null || true
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
    for check in "__main__.py" "core/config.py" "ui/gui.py" \
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
for _arg in "$@"; do
    if [[ "$_arg" == "--copy-only" ]]; then
        COPY_ONLY=true
        break
    fi
done
COPY_ONLY="${COPY_ONLY:-false}"

# ── 1. Find Python 3.9+ ────────────────────────────────────────────────────────
log "Checking for Python 3.9+"

PYTHON=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
    if command -v "$candidate" &>/dev/null; then
        # Ask Python itself for major/minor — avoids ALL string-parsing bugs.
        # Use a temp file instead of process substitution for bash 3.2 compat (macOS).
        _py_ver_out=$("$candidate" -c \
            "import sys; print(sys.version_info.major, sys.version_info.minor)" \
            2>/dev/null || echo "0 0")
        py_major="${_py_ver_out%% *}"
        py_minor="${_py_ver_out##* }"
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
        brew install python@3.11
        # Prefer versioned binary; brew --prefix may not exist on first install
        if command -v python3.11 &>/dev/null; then
            PYTHON="python3.11"
        elif [[ -x "$(brew --prefix python@3.11)/bin/python3.11" ]]; then
            PYTHON="$(brew --prefix python@3.11)/bin/python3.11"
        else
            PYTHON="$(brew --prefix)/bin/python3.11"
        fi
        ok "Python 3.11 installed via Homebrew"
    elif [[ "$OS" == "Linux" ]]; then
        if command -v apt-get &>/dev/null; then
            sudo apt-get update -qq
            sudo apt-get install -y python3 python3-pip python3-venv
            PYTHON="python3"
        elif command -v dnf &>/dev/null; then
            sudo dnf install -y python3 python3-pip
            PYTHON="python3"
        elif command -v pacman &>/dev/null; then
            sudo pacman -Sy --noconfirm python python-pip
            PYTHON="python3"
        else
            err "Cannot auto-install Python. Install Python 3.9+ manually and re-run."
        fi
        ok "Python installed via package manager"
    else
        err "Unsupported OS: $OS. Install Python 3.9+ manually."
    fi
fi

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

# ── 4. macOS Accessibility reminder ────────────────────────────────────────────
if [[ "$OS" == "Darwin" ]]; then
    echo ""
    warn "╔═══════════════════════════════════════════════════════╗"
    warn "║  BEFORE FIRST LAUNCH: macOS Accessibility             ║"
    warn "║  System Settings → Privacy & Security → Accessibility ║"
    warn "║  Add your Terminal and enable the toggle.             ║"
    warn "╚═══════════════════════════════════════════════════════╝"
    echo ""
fi

# ── 5. Run the Python installer ─────────────────────────────────────────────────

# --copy-only: skip Python installer, just re-copy source files into existing install
if $COPY_ONLY; then
    # Determine INSTALL_DIR from saved config
    INSTALL_DIR=""
    for cfg_candidate in \
        "$SCRIPT_DIR/spoaken_config.json" \
        "$HOME/Spoaken/spoaken_config.json"; do
        if [[ -f "$cfg_candidate" ]]; then
            INSTALL_DIR=$(
                "$PYTHON" - "$cfg_candidate" 2>/dev/null <<'PYEOF'
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
            )
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
for _arg in "$@"; do
    [[ "$_arg" != "--copy-only" ]] && PY_ARGS+=("$_arg")
done

set +e
"$PYTHON" "$SCRIPT_DIR/install.py" "${CONFIG_ARGS[@]}" "${PY_ARGS[@]}"
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
    # Use python -c with a file argument (via stdin redirection) to avoid
    # any quoting / injection issues with paths that contain special chars.
    INSTALL_DIR=""
    for cfg_candidate in \
        "$SCRIPT_DIR/spoaken_config.json" \
        "$HOME/Spoaken/spoaken_config.json"; do
        if [[ -f "$cfg_candidate" ]]; then
            INSTALL_DIR=$(
                "$PYTHON" - "$cfg_candidate" 2>/dev/null <<'PYEOF'
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
            )
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
    [[ "$OS" == "Windows_NT" ]] && VENV_PYTHON="$INSTALL_DIR/venv/Scripts/python.exe"

    echo -e "  Installed to:  ${CYAN}$INSTALL_DIR${NC}"
    echo ""
    echo -e "  To re-run with optional packages:"
    echo -e "    ${CYAN}./install.sh --noise --llm${NC}"
    echo ""

    # ── 7. Launch Spoaken immediately ────────────────────────────────────────
    if [[ -x "$RUN_SH" ]]; then
        echo -e "${CYAN}[Spoaken]${NC} Starting Spoaken…"
        echo ""
        exec "$RUN_SH"
    elif [[ -x "$VENV_PYTHON" ]]; then
        echo -e "${CYAN}[Spoaken]${NC} Starting Spoaken via venv Python…"
        echo ""
        exec "$VENV_PYTHON" -m spoaken
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
