#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════
#  Spoaken — Unix Bootstrap Installer (macOS + Linux)
#  Usage: chmod +x bootstrap.sh && ./bootstrap.sh
#  Works on: macOS 12+, Ubuntu/Debian, Fedora/RHEL, Arch Linux
# ══════════════════════════════════════════════════════════════════════

set -euo pipefail

CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'
BOLD='\033[1m'

log()  { echo -e "${CYAN}[Spoaken]${NC} $*"; }
ok()   { echo -e "${GREEN}  [✔]${NC} $*"; }
warn() { echo -e "${YELLOW}  [!]${NC} $*"; }
err()  { echo -e "${RED}  [✘]${NC} $*"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OS="$(uname -s)"

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║      SPOAKEN — Bootstrap Installer                   ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Optional flags (passed through to install.py):"
echo -e "    ${CYAN}--noise${NC}        Install noise suppression (noisereduce)"
echo -e "    ${CYAN}--translation${NC}  Install translation support (deep-translator)"
echo -e "    ${CYAN}--llm${NC}          Install LLM + summarization (ollama, sumy, nltk)"
echo -e "    ${CYAN}--no-vad${NC}       Skip webrtcvad  (use energy-gate fallback)"
echo -e "    ${CYAN}--chat${NC}         Enable LAN chat server in config"
echo ""
echo -e "  Example:  ${CYAN}./install.sh --noise --translation${NC}"
echo ""

# ── 1. Find or install Python 3.9+ ─────────────────────────────────────────────
log "Checking for most recent Python"

PYTHON=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
        major="${ver%%.*}"
        minor="${ver##*.}"
        # bash numeric comparison — strip leading zeros safely
        min_clean=$(echo "$minor" | sed 's/^0*//')
        min_clean=${min_clean:-0}
        if [[ "$major" -gt 3 ]] || { [[ "$major" -eq 3 ]] && [[ "$min_clean" -ge 9 ]]; }; then
            PYTHON="$candidate"
            ok "Found Python $ver at $(command -v $candidate)"
            break
        else
            warn "Found Python $ver but need 3.9+. Skipping $candidate."
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    warn "Python 3.9+ not found. Installing..."

    if [[ "$OS" == "Darwin" ]]; then
        # macOS — use Homebrew
        if ! command -v brew &>/dev/null; then
            log "Installing Homebrew first..."
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
            # Apple Silicon
            [[ -f /opt/homebrew/bin/brew ]] && eval "$(/opt/homebrew/bin/brew shellenv)"
        fi
        brew install python@3.11
        PYTHON="$(brew --prefix python@3.11)/bin/python3.11"
        ok "Python 3.14 installed via Homebrew"

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
            err "Cannot auto-install Python. Please install most recent Python manually and re-run."
        fi
        ok "Python installed via system package manager"
    else
        err "Unsupported OS: $OS. Please install Python 3.9+ manually."
    fi
fi

# Confirm final python version
PY_VER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
ok "Using Python $PY_VER"

# ── 2. Ensure install.py is present ────────────────────────────────────────────
if [[ ! -f "$SCRIPT_DIR/install.py" ]]; then
    err "install.py not found in $SCRIPT_DIR. Please place install.py alongside this script."
fi

# ── 3. Determine config mode ────────────────────────────────────────────────────
CONFIG_ARG=""
if [[ -f "$SCRIPT_DIR/spoaken_config.json" ]]; then
    log "Found spoaken_config.json — using saved configuration."
    CONFIG_ARG="--config $SCRIPT_DIR/spoaken_config.json"
else
    log "No config file found. Launching interactive setup."
    CONFIG_ARG="--interactive"
fi

# ── 4. Collect any extra flags passed to this script ──────────────────────────
#    e.g.  ./install.sh --noise --translation --llm
EXTRA_FLAGS=""
for arg in "$@"; do
    EXTRA_FLAGS="$EXTRA_FLAGS $arg"
done

# ── 5. macOS: request Accessibility permission reminder ────────────────────────
if [[ "$OS" == "Darwin" ]]; then
    echo ""
    warn "╔═══════════════════════════════════════════════════════╗"
    warn "║  BEFORE FIRST LAUNCH: macOS Accessibility             ║"
    warn "║  System Settings → Privacy & Security → Accessibility ║"
    warn "║  Add your Terminal and enable the toggle.             ║"
    warn "╚═══════════════════════════════════════════════════════╝"
    echo ""
fi

# ── 6. Run the Python installer ─────────────────────────────────────────────────
log "Launching Spoaken installer..."
echo ""

# shellcheck disable=SC2086
"$PYTHON" "$SCRIPT_DIR/install.py" $CONFIG_ARG $EXTRA_FLAGS

EXIT_CODE=$?

echo ""
if [[ $EXIT_CODE -eq 0 ]]; then
    echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║  Bootstrap complete. Spoaken is ready to use.        ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  Launch with: ${CYAN}python3 spoaken/spoaken_main.py${NC}"
    echo ""
    echo -e "  To add optional packages later, re-run with flags, e.g.:"
    echo -e "    ${CYAN}./install.sh --noise --translation --llm${NC}"
    echo -e " Or install from spoaken update module"
else
    echo -e "${RED}[✘] Installation finished with errors (exit code $EXIT_CODE).${NC}"
    echo "    Review the output above and retry with:"
    echo "    python3 install.py --interactive"
fi
echo ""
