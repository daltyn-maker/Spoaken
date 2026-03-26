#!/usr/bin/env python3
"""
Spoaken — Cross-Platform Installer
====================================
Supports: Windows 10/11 · macOS 12+ · Ubuntu/Debian · Fedora/RHEL · Arch Linux

The installer NEVER auto-detects connectivity.  Each feature group that
contacts an external server is explained and individually confirmed.

Usage:
    python install.py               # default: prompts for each external group
    python install.py --interactive # same as default (alias for clarity)
    python install.py --config PATH # re-run from a saved config
    python install.py --vosk-only   # re-download Vosk model only
    python install.py --online-only # add Tor/translate to an existing install
    python install.py --noise       # add noise suppression to existing install
    python install.py --llm         # add LLM packages to existing install
    python install.py --no-vad      # skip webrtcvad (energy-gate VAD fallback)
    python install.py --chat        # enable LAN chat server in config
"""

import argparse, json, os, platform, shutil, subprocess
import sys, tarfile, tempfile, time, urllib.error, urllib.request, zipfile
from pathlib import Path

# ── Python version gate ───────────────────────────────────────────────────────
if sys.version_info < (3, 9):
    print(
        f"[Spoaken]: Python 3.9+ required "
        f"(current: {sys.version_info.major}.{sys.version_info.minor})",
        file=sys.stderr,
    )
    sys.exit(1)

# ── Enable ANSI on Windows ────────────────────────────────────────────────────
if platform.system() == "Windows":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleMode(
            ctypes.windll.kernel32.GetStdHandle(-11), 7
        )
    except Exception:
        pass

CYAN   = "\033[0;36m"
GREEN  = "\033[0;32m"
YELLOW = "\033[1;33m"
RED    = "\033[0;31m"
DIM    = "\033[2m"
BOLD   = "\033[1m"
NC     = "\033[0m"

BANNER = f"""
{CYAN}╔══════════════════════════════════════════════════════════╗
║           SPOAKEN  Installer  v2.1                       ║
║           Voice-to-Text  ·  Whisper + Vosk               ║
╚══════════════════════════════════════════════════════════╝{NC}
"""

# ── Logging ───────────────────────────────────────────────────────────────────
def _hdr(msg):
    pad = max(52 - len(msg), 0)
    print(f"\n{BOLD}{CYAN}┌─ {msg} {'─' * pad}┐{NC}")

def log(msg):  print(f"{CYAN}[Spoaken]{NC} {msg}")
def ok(msg):   print(f"{GREEN}  ✔{NC}  {msg}")
def warn(msg): print(f"{YELLOW}  !{NC}  {msg}", file=sys.stderr)
def err(msg):  print(f"{RED}  ✘{NC}  {msg}", file=sys.stderr)
def dim(msg):  print(f"  {DIM}{msg}{NC}")

def _bar(pct, width=38):
    filled = int(width * pct / 100)
    return f"{CYAN}[{'█' * filled}{'░' * (width - filled)}]{NC} {pct:3d}%"

def ask(prompt, default_yes=True):
    """Y/n or y/N prompt. Returns bool."""
    hint = "[Y/n]" if default_yes else "[y/N]"
    raw  = input(f"  {prompt} {hint}: ").strip().lower()
    if default_yes:
        return raw not in ("n", "no")
    else:
        return raw in ("y", "yes")

def ask_str(prompt, default=""):
    hint = f"[{default}]" if default else ""
    raw  = input(f"  {prompt} {hint}: ").strip()
    return raw or default

# ── Platform ──────────────────────────────────────────────────────────────────
OS         = platform.system()   # "Windows" | "Darwin" | "Linux"
SCRIPT_DIR = Path(__file__).resolve().parent

# ── Global: venv Python (set after venv creation) ─────────────────────────────
_VENV_PYTHON: Path | None = None

# ══════════════════════════════════════════════════════════════════════════════
# System helpers
# ══════════════════════════════════════════════════════════════════════════════
def detect_linux_pm():
    for pm in ("apt", "dnf", "pacman"):
        if shutil.which(pm):
            return pm
    return None

def detect_wayland():
    return (
        os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        or bool(os.environ.get("WAYLAND_DISPLAY"))
    )

# ══════════════════════════════════════════════════════════════════════════════
# Venv
# ══════════════════════════════════════════════════════════════════════════════
def venv_py_path(venv_dir):
    return venv_dir / ("Scripts/python.exe" if OS == "Windows" else "bin/python")

def create_venv(venv_dir):
    py = venv_py_path(venv_dir)
    if py.exists():
        ok(f"Existing venv: {venv_dir}")
        return py
    log(f"Creating virtual environment at {venv_dir} …")
    r = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"venv failed:\n{(r.stderr or r.stdout).strip()}")
    if not py.exists():
        alt = venv_dir / "bin" / "python3"
        if alt.exists():
            return alt
        raise RuntimeError(f"venv created but python not found: {py}")
    ok(f"Venv created: {venv_dir}")
    return py

def python_exe():
    if _VENV_PYTHON and _VENV_PYTHON.exists():
        return str(_VENV_PYTHON)
    return sys.executable

# ══════════════════════════════════════════════════════════════════════════════
# pip wrapper
# ══════════════════════════════════════════════════════════════════════════════
def pip_run(*args, check=True):
    cmd = [python_exe(), "-m", "pip"] + list(args)
    if OS == "Linux":
        pm = detect_linux_pm()
        if pm in ("apt", "pacman") and "--break-system-packages" not in args:
            if args and args[0] in ("install", "upgrade"):
                cmd.append("--break-system-packages")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return r

def _run(cmd, check=True, capture=False):
    kw = {"text": True}
    if capture:
        kw["capture_output"] = True
    r = subprocess.run(cmd, **kw)
    if check and r.returncode != 0:
        detail = ((r.stderr or r.stdout) or "").strip() if capture else ""
        raise RuntimeError(
            f"Command failed: {' '.join(str(c) for c in cmd)}"
            + (f": {detail}" if detail else "")
        )
    return r

def _install_pkg_list(packages):
    """Install a list of pip packages — one ✔ or ! per line, apt-style."""
    for pkg in packages:
        name = pkg.split(">=")[0].split("<=")[0].split("<")[0].split("==")[0].strip()
        print(f"  {CYAN}Installing{NC} {name:<44}", end="", flush=True)
        try:
            pip_run("install", "--upgrade", "--quiet", pkg)
            print(f"{GREEN}✔{NC}")
        except RuntimeError as e:
            print(f"{YELLOW}! (warning){NC}")
            warn(f"  Could not install {pkg}: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# Download helpers
# ══════════════════════════════════════════════════════════════════════════════
def download_file(url, dest, label, retries=3):
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                total      = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                with open(dest, "wb") as fh:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = int(100 * downloaded / total)
                            print(
                                f"\r  {_bar(pct)} "
                                f"{downloaded/1048576:.1f}/{total/1048576:.1f} MB"
                                f"  {label}  ",
                                end="", flush=True,
                            )
            print()
            return dest
        except (urllib.error.URLError, OSError) as e:
            warn(f"Attempt {attempt}/{retries}: {e}")
            if attempt < retries:
                time.sleep(2 * attempt)
            else:
                raise RuntimeError(
                    f"Download failed after {retries} attempts: {label}"
                ) from e

def extract_archive(archive, dest):
    dest.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    elif ".tar" in archive.name or archive.suffix in (".gz", ".bz2", ".xz"):
        with tarfile.open(archive) as tf:
            try:
                tf.extractall(dest, filter="data")
            except TypeError:
                tf.extractall(dest)
    else:
        raise ValueError(f"Unknown archive type: {archive}")

# ══════════════════════════════════════════════════════════════════════════════
# System dependency installers
# ══════════════════════════════════════════════════════════════════════════════
def _sys_windows():
    if not shutil.which("winget"):
        raise RuntimeError(
            "winget not found. Update Windows to 21H2+ or install "
            "App Installer from the Microsoft Store."
        )
    r = _run(
        ["winget", "install", "--id", "Gyan.FFmpeg", "-s", "winget",
         "--silent", "--accept-package-agreements", "--accept-source-agreements"],
        check=False, capture=True,
    )
    body = (r.stdout + r.stderr).lower()
    if r.returncode == 0 or "already installed" in body:
        ok("FFmpeg ready")
    else:
        warn("winget could not install FFmpeg. "
             "Download manually: https://www.gyan.dev/ffmpeg/builds/")

def _sys_macos():
    if not shutil.which("brew"):
        log("Installing Homebrew (requires internet)…")
        _run(["/bin/bash", "-c",
              "curl -fsSL "
              "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
              " | /bin/bash"])
        if os.path.exists("/opt/homebrew/bin/brew"):
            os.environ["PATH"] = f"/opt/homebrew/bin:{os.environ['PATH']}"
        ok("Homebrew installed")
    for pkg in ("ffmpeg", "portaudio"):
        r = _run(["brew", "install", pkg], check=False, capture=True)
        if r.returncode == 0 or "already installed" in (r.stdout + r.stderr).lower():
            ok(f"{pkg} ready")
        else:
            warn(f"brew install {pkg} failed: {r.stderr.strip()}")

def _sys_linux():
    pm = detect_linux_pm()
    if not pm:
        raise RuntimeError(
            "Unsupported Linux distro — install manually: "
            "ffmpeg portaudio python3-tk wmctrl xdotool"
        )
    pkg_map = {
        "apt":    ["ffmpeg", "portaudio19-dev", "python3-dev", "python3-pip",
                   "python3-venv", "python3-tk", "wmctrl", "xdotool",
                   "libgirepository1.0-dev", "pkg-config", "build-essential"],
        "dnf":    ["ffmpeg", "portaudio-devel", "python3-devel", "python3-pip",
                   "python3-tkinter", "wmctrl", "xdotool",
                   "gobject-introspection-devel", "gcc", "gcc-c++"],
        "pacman": ["ffmpeg", "portaudio", "python", "python-pip",
                   "tk", "wmctrl", "xdotool", "base-devel"],
    }
    # tor is only added if Tor P2P was selected — handled by install_tor_syspkg()
    if pm == "apt":
        _run(["sudo", "apt", "update", "-qq"], check=False)
        _run(["sudo", "apt", "install", "-y"] + pkg_map["apt"])
    elif pm == "dnf":
        r = _run(["rpm", "-E", "%fedora"], capture=True, check=False)
        fver = r.stdout.strip() if r.returncode == 0 else "39"
        for url in [
            f"https://mirrors.rpmfusion.org/free/fedora/"
            f"rpmfusion-free-release-{fver}.noarch.rpm",
            f"https://mirrors.rpmfusion.org/nonfree/fedora/"
            f"rpmfusion-nonfree-release-{fver}.noarch.rpm",
        ]:
            _run(["sudo", "dnf", "install", "-y", url], check=False)
        _run(["sudo", "dnf", "install", "-y"] + pkg_map["dnf"])
    elif pm == "pacman":
        _run(["sudo", "pacman", "-Syu", "--noconfirm"] + pkg_map["pacman"])
    ok("System packages installed")
    if detect_wayland():
        warn("Wayland detected — wmctrl/xdotool need an X11 session for window targeting.")

def install_tor_syspkg():
    """Install the tor system daemon — only called when Tor P2P is selected."""
    pm = detect_linux_pm()
    if pm == "apt":
        _run(["sudo", "apt", "install", "-y", "tor"], check=False)
    elif pm == "dnf":
        _run(["sudo", "dnf", "install", "-y", "tor"], check=False)
    elif pm == "pacman":
        _run(["sudo", "pacman", "-S", "--noconfirm", "tor"], check=False)

# ══════════════════════════════════════════════════════════════════════════════
# Package manifests
# ══════════════════════════════════════════════════════════════════════════════

# Always installed — no external server calls beyond pip itself.
COMMON_PACKAGES = [
    "customtkinter>=5.2.2",
    "Pillow>=10.0.0",
    "sounddevice>=0.4.7",
    "numpy>=1.26.0",
    "scipy>=1.12.0",
    "pyautogui>=0.9.54",
    "rapidfuzz>=3.6.1",
    "websockets>=12.0",         # LAN chat — local network only
    "cryptography>=42.0.0",     # Ed25519 identity keys — local only
    "psutil>=5.9.8",
]

# faster-whisper pip package — installed always.
# The actual model download is a SEPARATE external prompt (HuggingFace Hub).
WHISPER_PKG = ["faster-whisper>=1.0.3"]

# Grammar packages installed via pip — no external calls during install.
# The T5 model download from HuggingFace Hub happens at RUNTIME on first
# Polish operation — also prompted separately below.
GRAMMAR_PACKAGES = [
    "happytransformer<4.0.0",   # pip install only — runtime HF Hub call is separate
    "transformers>=4.40.0",
    "sentencepiece>=0.2.0",
    "protobuf>=4.25.0",
]

# Tor P2P relay — contacts Tor network at runtime.
TOR_PACKAGES = [
    "stem>=1.8.2",
    "PySocks>=1.7.1",
    "aiohttp>=3.9.0",
    "aiofiles>=23.2.1",
]

# Google Translate — sends transcript text to translate.googleapis.com.
TRANSLATE_PACKAGES = ["deep-translator>=1.11.4"]

# Noise suppression — pure local signal processing, no external calls.
NOISE_PACKAGES = ["noisereduce>=3.0.0"]

# LLM — connects to LOCAL Ollama daemon on localhost:11434, not the internet.
LLM_PACKAGES = [
    "ollama>=0.2.0",
    "sumy>=0.11.0",
    "nltk>=3.8.1",
    "scikit-learn>=1.4.0",
    "networkx>=3.3",
]

# WebRTC VAD — pip only, no external calls.
VAD_PACKAGES = ["webrtcvad"]

PLATFORM_EXTRA = {
    "Windows": ["pywin32>=306", "pywinauto>=0.6.8"],
    "Darwin":  [],
    "Linux":   [],
}

# Vosk model catalogue — downloads from alphacephei.com.
VOSK_MODELS = {
    "vosk-model-small-en-us-0.15":
        "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip",
    "vosk-model-en-us-0.22":
        "https://alphacephei.com/vosk/models/vosk-model-en-us-0.22.zip",
    "vosk-model-en-us-0.42-gigaspeech":
        "https://alphacephei.com/vosk/models/vosk-model-en-us-0.42-gigaspeech.zip",
}

# ══════════════════════════════════════════════════════════════════════════════
# pip bootstrap
# ══════════════════════════════════════════════════════════════════════════════
def ensure_pip():
    log("Upgrading pip, setuptools, wheel…")
    pip_run("install", "--upgrade", "pip", "setuptools", "wheel")
    ok("pip up to date")

# ══════════════════════════════════════════════════════════════════════════════
# Whisper model pre-download (HuggingFace Hub)
# ══════════════════════════════════════════════════════════════════════════════
def preload_whisper(model_name, models_dir):
    models_dir.mkdir(parents=True, exist_ok=True)
    log(f"Pre-downloading Whisper model: {model_name}")
    log("  Source : HuggingFace Hub  (huggingface.co)")
    log("  Size   : 140 MB – 3 GB depending on model")
    script = (
        "import sys, os\n"
        "sys.stdout.reconfigure(line_buffering=True)\n"
        "from faster_whisper import WhisperModel\n"
        f"cache = r\"{models_dir}\"\n"
        "os.makedirs(cache, exist_ok=True)\n"
        "print('Connecting to HuggingFace Hub…', flush=True)\n"
        f"WhisperModel('{model_name}', device='cpu', "
        f"compute_type='int8', download_root=cache)\n"
        "print('Model ready.', flush=True)\n"
    )
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(script)
            tmp = Path(fh.name)
        r = subprocess.run([python_exe(), str(tmp)], text=True)
        if r.returncode != 0:
            warn("Whisper model pre-download failed — will retry automatically on first launch.")
        else:
            ok(f"Whisper '{model_name}' cached at {models_dir}")
    finally:
        if tmp and tmp.exists():
            tmp.unlink(missing_ok=True)

# ══════════════════════════════════════════════════════════════════════════════
# Vosk model download (alphacephei.com)
# ══════════════════════════════════════════════════════════════════════════════
def install_vosk(model_name, models_dir):
    if model_name not in VOSK_MODELS:
        raise ValueError(f"Unknown Vosk model: {model_name}")
    pip_run("install", "--quiet", "vosk")
    ok("vosk package installed")
    url     = VOSK_MODELS[model_name]
    archive = models_dir / f"{model_name}.zip"
    dest    = models_dir / model_name
    if dest.exists():
        ok(f"Vosk model '{model_name}' already present")
        return
    models_dir.mkdir(parents=True, exist_ok=True)
    log(f"  Source : alphacephei.com")
    download_file(url, archive, model_name)
    ok("Extracting…")
    extract_archive(archive, models_dir)
    archive.unlink(missing_ok=True)
    ok(f"Vosk model ready: {dest}")

# ══════════════════════════════════════════════════════════════════════════════
# Source file copy
# ══════════════════════════════════════════════════════════════════════════════
# Maps dest path inside install_dir/spoaken/ → source filename in SCRIPT_DIR.
# None = generate a stub __init__.py (no source file needed).
_FILE_MAP = {
    "__init__.py":                    "__init__.py",
    "__main__.py":                    "__main__.py",
    "core/__init__.py":               None,
    "core/config.py":                 "config.py",
    "core/engine.py":                 "engine.py",
    "core/vad.py":                    "vad.py",
    "ui/__init__.py":                 None,
    "ui/gui.py":                      "gui.py",
    "ui/splash.py":                   "splash.py",
    "network/__init__.py":            None,
    "network/chat.py":                "chat.py",
    "network/lan.py":                 "lan.py",
    # network/online.py  — copied only if Tor P2P was selected
    "processing/__init__.py":         None,
    "processing/llm.py":              "llm.py",
    "processing/summarize.py":        "summarize.py",
    "processing/summarize_router.py": "summarize_router.py",
    "processing/writer.py":           "writer.py",
    "system/__init__.py":             None,
    "system/crashlog.py":             "crashlog.py",
    "system/environ.py":              "environ.py",
    "system/mic_config.py":           "mic_config.py",
    "system/paths.py":                "paths.py",
    "system/session_recovery.py":     "session_recovery.py",
    "control/__init__.py":            None,
    "control/commands.py":            "commands.py",
    "control/controller.py":          "controller.py",
    "control/update.py":              "update.py",
}

_INIT_BODIES = {
    "core/__init__.py":       '"""core — Transcription engine and configuration."""\n',
    "ui/__init__.py":         '"""ui — User interface components."""\n',
    "network/__init__.py":    '"""network — Chat and networking features."""\n',
    "processing/__init__.py": '"""processing — Text processing and AI."""\n',
    "system/__init__.py":     '"""system — System utilities and monitoring."""\n',
    "control/__init__.py":    '"""control — Application control layer."""\n',
}

def copy_source_files(install_dir, include_tor_p2p: bool):
    dest_root = install_dir / "spoaken"
    copied = skipped = created = 0

    for rel_dest, src_name in _FILE_MAP.items():
        dest_file = dest_root / rel_dest
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        if src_name is None:
            if not dest_file.exists():
                body = _INIT_BODIES.get(rel_dest, '"""auto-generated"""\n')
                dest_file.write_text(body, encoding="utf-8")
                created += 1
            continue
        src = SCRIPT_DIR / src_name
        if not src.exists():
            warn(f"Source not found, skipping: {src_name}")
            skipped += 1
            continue
        shutil.copy2(src, dest_file)
        copied += 1

    # online.py — only copy if Tor P2P was selected.
    online_dest = dest_root / "network" / "online.py"
    online_src  = SCRIPT_DIR / "online.py"
    if include_tor_p2p:
        if online_src.exists():
            online_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(online_src, online_dest)
            copied += 1
            ok("Copied online.py (Tor P2P relay)")
        else:
            warn("online.py not found in source directory — Tor P2P unavailable.")
    else:
        # Ensure it does NOT exist so the guarded import in chat.py stays clean.
        if online_dest.exists():
            online_dest.unlink()
        ok("online.py not installed (Tor P2P not selected — safe to ignore)")

    # Assets (logos, GIFs, icons)
    assets_dest = dest_root / "assets"
    assets_dest.mkdir(parents=True, exist_ok=True)
    assets_src = SCRIPT_DIR / "assets"
    if assets_src.is_dir():
        for f in assets_src.iterdir():
            shutil.copy2(f, assets_dest / f.name)
            copied += 1
    else:
        for ext in ("*.png", "*.ico", "*.gif"):
            for img in SCRIPT_DIR.glob(ext):
                shutil.copy2(img, assets_dest / img.name)
                copied += 1

    # Copy run.sh / install.sh / install.py / README to install_dir root.
    for fname in ("run.sh", "install.sh", "install.py", "README.md", "spoaken_config.json"):
        src = SCRIPT_DIR / fname
        if src.exists():
            shutil.copy2(src, install_dir / fname)
            if fname in ("run.sh", "install.sh") and OS != "Windows":
                (install_dir / fname).chmod(0o755)

    ok(
        f"Source files: {copied} copied, {created} __init__.py generated"
        + (f", {skipped} skipped" if skipped else "")
    )

# ══════════════════════════════════════════════════════════════════════════════
# Runtime config writer
# ══════════════════════════════════════════════════════════════════════════════
def write_config(cfg, install_dir, venv_dir, include_tor_p2p: bool,
                 include_translate: bool, include_grammar: bool):
    config_path = install_dir / "spoaken_config.json"
    install_dir.mkdir(parents=True, exist_ok=True)
    runtime = {
        "venv_dir":               str(venv_dir),
        # offline_mode: True means no outbound internet from within Spoaken itself.
        # Tor P2P and Google Translate are both disabled when True.
        "offline_mode":           not (include_tor_p2p or include_translate),
        "tor_p2p_enabled":        include_tor_p2p,
        "translation_enabled":    include_translate,
        # Transcription engines
        "whisper_model":          cfg.get("whisper_model",  "base.en"),
        "whisper_enabled":        True,
        "whisper_compute":        "auto",
        "vosk_model":             cfg.get("vosk_model",     None),
        "vosk_enabled":           cfg.get("vosk_enabled",   False),
        "enable_giga_model":      False,
        "vosk_model_accurate":    "vosk-model-en-us-0.42-gigaspeech",
        "engine_mode":            "auto",
        # Hardware
        "gpu":                    cfg.get("gpu",            False),
        "mic_device":             None,
        "noise_suppression":      cfg.get("noise",          False),
        # Audio pipeline
        "audio_preset":           "budget_usb",
        "eq_profile":             "speech",
        "hp_cutoff":              80,
        "nr_strength":            0.75,
        "comp_threshold_db":      -18.0,
        "comp_ratio":             4.0,
        "agc_target_rms":         0.15,
        "agc_max_gain_db":        12.0,
        # VAD
        "vad_aggressiveness":     2,
        "vad_min_speech_ms":      200,
        "vad_silence_gap_ms":     500,
        "vad_energy_threshold":   0.015,
        "vad_config_persist":     True,
        # Grammar
        "grammar":                include_grammar,
        "grammar_lazy_load":      True,
        "t5_model":               "vennify/t5-base-grammar-correction",
        # Chat / networking
        "chat_server_enabled":    cfg.get("chat_server_enabled",    False),
        "chat_server_port":       cfg.get("chat_server_port",       55300),
        "chat_server_token":      cfg.get("chat_server_token",      "spoaken"),
        "android_stream_enabled": cfg.get("android_stream_enabled", False),
        "android_stream_port":    cfg.get("android_stream_port",    55301),
        # Memory / text
        "memory_cap_words":       cfg.get("memory_cap_words",   2000),
        "memory_cap_minutes":     cfg.get("memory_cap_minutes", 60),
        "duplicate_filter":       True,
        "enable_partials":        False,
        "log_unlimited":          True,
        # Performance
        "llm_lazy_load":          True,
        "background_mode":        False,
        "audio_lookahead_buffer": 4,
        # Paths — used by system/paths.py at runtime
        "whisper_dir":            str(install_dir / "models" / "whisper"),
        "vosk_dir":               str(install_dir / "models" / "vosk"),
        "install_dir":            str(install_dir),
        "platform":               OS,
        "first_run_shown":        False,
    }
    tmp = config_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(runtime, fh, indent=2)
    tmp.replace(config_path)
    ok(f"Config written → {config_path}")

# ══════════════════════════════════════════════════════════════════════════════
# Desktop shortcut
# ══════════════════════════════════════════════════════════════════════════════
def create_shortcut(install_dir):
    install_dir = install_dir.resolve()
    assets      = install_dir / "spoaken" / "assets"
    ico         = assets / "logo.ico"
    png         = assets / "logo.png"
    if OS == "Windows":
        lnk     = Path.home() / "Desktop" / "Spoaken.lnk"
        icon_ps = f'$s.IconLocation="{ico}";' if ico.exists() else ""
        ps = (
            f'$ws=New-Object -ComObject WScript.Shell;'
            f'$s=$ws.CreateShortcut("{lnk}");'
            f'$s.TargetPath="{python_exe()}";'
            f'$s.Arguments="-m spoaken";'
            f'$s.WorkingDirectory="{install_dir}";'
            f'{icon_ps}$s.Save()'
        )
        r = _run(["powershell", "-NoProfile", "-Command", ps], check=False, capture=True)
        if r.returncode == 0:
            ok(f"Desktop shortcut → {lnk}")
        else:
            warn(f"Windows shortcut failed: {r.stderr.strip()}")
    elif OS == "Darwin":
        launcher = Path("/Applications") / "Spoaken.command"
        try:
            launcher.write_text(
                f'#!/usr/bin/env bash\ncd "{install_dir}"\n'
                f'exec "{python_exe()}" -m spoaken\n'
            )
            launcher.chmod(0o755)
            ok(f"Launcher → {launcher}")
        except PermissionError:
            warn("Could not write to /Applications — try running with sudo.")
    elif OS == "Linux":
        apps = Path.home() / ".local" / "share" / "applications"
        apps.mkdir(parents=True, exist_ok=True)
        df   = apps / "spoaken.desktop"
        icon = f"Icon={png}" if png.exists() else "Icon=audio-input-microphone"
        df.write_text(
            "[Desktop Entry]\nVersion=1.0\nType=Application\nName=Spoaken\n"
            f"Comment=Voice-to-Text Engine\nExec={python_exe()} -m spoaken\n"
            f"Path={install_dir}\n{icon}\nTerminal=false\n"
            "Categories=Utility;Accessibility;\n"
        )
        df.chmod(0o755)
        ok(f".desktop entry → {df}")
        ud = Path.home() / "Desktop"
        if ud.is_dir():
            dc = ud / "Spoaken.desktop"
            shutil.copy2(df, dc)
            dc.chmod(0o755)
            ok(f"Desktop shortcut → {dc}")
        if shutil.which("update-desktop-database"):
            _run(["update-desktop-database", str(apps)], check=False, capture=True)

# ══════════════════════════════════════════════════════════════════════════════
# PROMPT FLOW — each external-server group is individually explained and asked
# ══════════════════════════════════════════════════════════════════════════════
def prompt_install_choices() -> dict:
    """
    Walks the user through every feature group.  Groups that contact external
    servers are flagged with the server address.  No auto-detection is done.
    Returns a config dict.
    """
    print(BANNER)
    print(f"{BOLD}{CYAN}  Spoaken Installation Setup{NC}")
    print(f"  {DIM}Each section that contacts an external server will tell you")
    print(f"  exactly which server and why.  Press Enter for the default.{NC}\n")

    # ── Install directory ─────────────────────────────────────────────────────
    print(f"{CYAN}── Install Location ────────────────────────────────────────{NC}")
    default_dir = str(Path.home() / "Spoaken")
    idir = ask_str(f"Install directory", default=default_dir)
    idir = str(Path(idir).expanduser().resolve())
    print()

    # ── Whisper model (HuggingFace Hub) ──────────────────────────────────────
    print(f"{CYAN}── Whisper Model  {YELLOW}[EXTERNAL]{NC}  huggingface.co")
    print("  Whisper is the primary speech-to-text engine.")
    print("  The model files are downloaded from HuggingFace Hub during install.")
    print("  After download the model runs 100% locally — no further internet use.")
    print()
    print("  Available models:")
    whisper_opts = [
        ("tiny.en",    "~75 MB    fastest, English only"),
        ("base.en",    "~145 MB   good balance of speed and accuracy  ← default"),
        ("small.en",   "~460 MB   better accuracy, slower"),
        ("medium.en",  "~1.4 GB   high accuracy, requires 4+ GB RAM"),
        ("large-v3",   "~2.9 GB   best accuracy, requires 8+ GB RAM"),
        ("turbo",      "~1.6 GB   large-v3 speed optimised"),
    ]
    for i, (m, desc) in enumerate(whisper_opts, 1):
        print(f"  {i}. {m:<12}  {desc}")
    while True:
        raw = input(f"\n  Select Whisper model [2]: ").strip() or "2"
        try:
            whisper_model = whisper_opts[int(raw) - 1][0]
            break
        except (ValueError, IndexError):
            print("  Invalid choice, try again.")
    print()
    predownload_whisper = ask(
        f"Pre-download Whisper '{whisper_model}' now? (recommended — avoids delay on first launch)",
        default_yes=True,
    )
    print()

    # ── Vosk model (alphacephei.com) — optional ───────────────────────────────
    print(f"{CYAN}── Vosk Model  {YELLOW}[EXTERNAL]{NC}  alphacephei.com  (optional)")
    print("  Vosk enables real-time partial transcription while you speak.")
    print("  The model is downloaded from alphacephei.com and stored locally.")
    print("  Whisper alone is fully functional — Vosk is not required.")
    print()
    install_vosk_model = ask("Install a Vosk model for real-time partials?", default_yes=False)
    vosk_model = None
    vosk_enabled = False
    if install_vosk_model:
        vosk_opts = [
            ("vosk-model-small-en-us-0.15",      "~50 MB    recommended"),
            ("vosk-model-en-us-0.22",             "~1.8 GB   higher accuracy"),
            ("vosk-model-en-us-0.42-gigaspeech",  "~2.3 GB   highest accuracy"),
        ]
        print()
        for i, (k, desc) in enumerate(vosk_opts, 1):
            print(f"  {i}. {k:<40}  {desc}")
        while True:
            raw = input("\n  Select Vosk model [1]: ").strip() or "1"
            try:
                vosk_model   = vosk_opts[int(raw) - 1][0]
                vosk_enabled = True
                break
            except (ValueError, IndexError):
                print("  Invalid choice, try again.")
    print()

    # ── Grammar correction (HuggingFace Hub — RUNTIME call, not install) ──────
    print(f"{CYAN}── Grammar Correction  {YELLOW}[EXTERNAL]{NC}  huggingface.co  (runtime)")
    print("  Installs HappyTransformer, transformers, sentencepiece, protobuf, and PyTorch.")
    print("  The T5 grammar model (~480 MB) is downloaded from HuggingFace Hub the")
    print("  FIRST TIME you press Polish inside Spoaken — not during install.")
    print("  After that initial download the model runs fully offline.")
    print(f"  {YELLOW}Note: HappyTransformer and transformers require internet to install via pip.{NC}")
    print()
    install_grammar = ask("Install grammar correction packages?", default_yes=True)
    print()

    # ── Tor P2P relay (Tor network) — optional ────────────────────────────────
    print(f"{CYAN}── Tor P2P Chat  {YELLOW}[EXTERNAL]{NC}  Tor network  (optional)")
    print("  Enables encrypted anonymous P2P chat rooms routed through the Tor network.")
    print("  Installs: stem, PySocks, aiohttp, aiofiles + online.py source file.")
    print("  The tor system daemon is also installed (tor package).")
    print(f"  {DIM}LAN chat over your local network works without this feature.{NC}")
    print(f"  {DIM}If you choose No, online.py will not be installed.{NC}")
    print(f"  {DIM}Removing online.py later has no effect on any other Spoaken feature.{NC}")
    print()
    install_tor = ask("Install Tor P2P chat support?", default_yes=False)
    print()

    # ── Google Translate (translate.googleapis.com) — optional ────────────────
    print(f"{CYAN}── Translation  {YELLOW}[EXTERNAL]{NC}  translate.googleapis.com  (optional)")
    print("  Sends transcribed text to Google Translate for real-time translation.")
    print("  Installs: deep-translator.")
    print(f"  {YELLOW}Each translation request sends your transcript text to Google's servers.{NC}")
    print(f"  {DIM}Translation can be enabled or disabled at any time in the app.{NC}")
    print()
    install_translate = ask("Install Google Translate support?", default_yes=False)
    print()

    # ── LLM / summarization (localhost Ollama) ────────────────────────────────
    print(f"{CYAN}── LLM Summarization  {DIM}[LOCAL ONLY]{NC}  localhost:11434")
    print("  Enables automatic transcript summarization using a local Ollama model.")
    print("  Installs: ollama client, sumy, nltk, scikit-learn, networkx.")
    print(f"  {DIM}Ollama connects to your local Ollama daemon — no external servers.{NC}")
    print(f"  {DIM}You still need to run: ollama pull <model>  to download a model separately.{NC}")
    print()
    install_llm = ask("Install local LLM summarization support?", default_yes=False)
    print()

    # ── Noise suppression (local only) ───────────────────────────────────────
    print(f"{CYAN}── Noise Suppression  {DIM}[NO EXTERNAL SERVERS]{NC}")
    print("  Real-time microphone noise reduction using noisereduce.")
    print("  Installs: noisereduce.  All processing is local — no internet required.")
    print()
    install_noise = ask("Install noise suppression?", default_yes=False)
    print()

    # ── WebRTC VAD (local only) ───────────────────────────────────────────────
    print(f"{CYAN}── WebRTC Voice Activity Detection  {DIM}[NO EXTERNAL SERVERS]{NC}")
    print("  Better silence/speech detection.  Installs: webrtcvad (C extension).")
    print(f"  {DIM}If build fails, Spoaken falls back to energy-gate VAD automatically.{NC}")
    print()
    install_vad = ask("Install WebRTC VAD?", default_yes=True)
    print()

    # ── LAN chat server ───────────────────────────────────────────────────────
    print(f"{CYAN}── LAN Chat Server  {DIM}[LOCAL NETWORK ONLY]{NC}")
    print("  Allows other devices on your local network to receive the live transcript.")
    print(f"  {DIM}No internet traffic — LAN only.{NC}")
    print()
    enable_chat = ask("Enable LAN chat server at startup?", default_yes=False)
    chat_port   = 55300
    chat_token  = "spoaken"
    if enable_chat:
        chat_port  = int(ask_str("  Chat server port", default="55300") or "55300")
        chat_token = ask_str("  Auth token", default="spoaken") or "spoaken"
    print()

    # ── Android / browser stream ──────────────────────────────────────────────
    print(f"{CYAN}── Android / Browser Live Stream  {DIM}[LOCAL NETWORK ONLY]{NC}")
    print("  SSE endpoint lets browsers on your local network see the live transcript.")
    print()
    enable_android  = ask("Enable Android/browser live transcript stream?", default_yes=False)
    android_port    = 55301
    if enable_android:
        android_port = int(ask_str("  Stream port", default="55301") or "55301")
    print()

    # ── GPU ───────────────────────────────────────────────────────────────────
    print(f"{CYAN}── GPU Acceleration  {DIM}[NO EXTERNAL SERVERS]{NC}")
    print("  Enables CUDA for faster Whisper inference (requires NVIDIA GPU + CUDA).")
    print("  PyTorch with CUDA support is installed via PyPI.")
    print()
    enable_gpu = ask("Enable GPU/CUDA acceleration?", default_yes=False)
    print()

    return {
        "install_dir":            idir,
        "whisper_model":          whisper_model,
        "predownload_whisper":    predownload_whisper,
        "vosk_model":             vosk_model,
        "vosk_enabled":           vosk_enabled,
        "grammar":                install_grammar,
        "tor_p2p":                install_tor,
        "translate":              install_translate,
        "llm":                    install_llm,
        "noise":                  install_noise,
        "vad":                    install_vad,
        "gpu":                    enable_gpu,
        "chat_server_enabled":    enable_chat,
        "chat_server_port":       chat_port,
        "chat_server_token":      chat_token,
        "android_stream_enabled": enable_android,
        "android_stream_port":    android_port,
    }

# ══════════════════════════════════════════════════════════════════════════════
# Main installation orchestrator
# ══════════════════════════════════════════════════════════════════════════════
def run_install(cfg: dict):
    global _VENV_PYTHON

    install_dir         = Path(cfg["install_dir"]).expanduser().resolve()
    whisper_model       = cfg.get("whisper_model",       "base.en")
    predownload_whisper = cfg.get("predownload_whisper",  True)
    vosk_model          = cfg.get("vosk_model",           None)
    vosk_enabled        = cfg.get("vosk_enabled",         False)
    grammar             = cfg.get("grammar",              True)
    tor_p2p             = cfg.get("tor_p2p",              False)
    translate           = cfg.get("translate",            False)
    llm                 = cfg.get("llm",                  False)
    noise               = cfg.get("noise",                False)
    vad                 = cfg.get("vad",                  True)
    gpu                 = cfg.get("gpu",                  False)
    whisper_dir         = install_dir / "models" / "whisper"
    vosk_dir            = install_dir / "models" / "vosk"

    print(BANNER)
    print(f"  {BOLD}Platform   :{NC} {OS} {platform.version()}")
    print(f"  {BOLD}Python     :{NC} {platform.python_version()} @ {sys.executable}")
    print(f"  {BOLD}Install dir:{NC} {install_dir}")
    print(f"  {BOLD}Venv       :{NC} {install_dir / 'venv'}")
    print()
    print(f"  Feature summary:")
    print(f"    Whisper   : {whisper_model}  (pre-download: {'yes' if predownload_whisper else 'no'})")
    print(f"    Vosk      : {vosk_model or 'disabled'}")
    print(f"    Grammar   : {'yes — T5 model downloads on first Polish use' if grammar else 'no'}")
    print(f"    Tor P2P   : {'yes — contacts Tor network' if tor_p2p else 'no (online.py not installed)'}")
    print(f"    Translate : {'yes — sends text to Google Translate' if translate else 'no'}")
    print(f"    LLM       : {'yes — local Ollama (localhost only)' if llm else 'no'}")
    print(f"    Noise NR  : {'yes' if noise else 'no'}  |  VAD: {'yes' if vad else 'no (energy-gate fallback)'}  |  GPU: {'yes' if gpu else 'no'}")
    print()

    # Step 1 — venv
    _hdr("Step 1 / 8  Virtual Environment")
    venv_dir    = install_dir / "venv"
    _VENV_PYTHON = create_venv(venv_dir)

    # Step 2 — system packages
    _hdr("Step 2 / 8  System Dependencies")
    try:
        if OS == "Windows":
            _sys_windows()
        elif OS == "Darwin":
            _sys_macos()
        elif OS == "Linux":
            _sys_linux()
            if tor_p2p:
                install_tor_syspkg()
                ok("tor system daemon installed")
        else:
            warn(f"Unknown OS '{OS}' — skipping system packages.")
    except RuntimeError as e:
        err(str(e))
        warn("Continuing — some features may not work without system packages.")

    # Step 3 — pip bootstrap
    _hdr("Step 3 / 8  Package Manager Bootstrap")
    ensure_pip()

    # Step 4 — Python packages
    _hdr("Step 4 / 8  Python Packages")

    # faster-whisper (pip only — model download happens in Step 6)
    _install_pkg_list(WHISPER_PKG)

    # Core packages (no external server calls beyond pip)
    core = COMMON_PACKAGES + PLATFORM_EXTRA.get(OS, [])
    _hdr(f"  Core packages ({len(core)})")
    _install_pkg_list(core)

    # Grammar (PyPI packages — T5 model download happens at runtime)
    if grammar:
        _hdr(f"  Grammar packages ({len(GRAMMAR_PACKAGES) + 1})  [PyTorch + HappyTransformer]")
        if gpu and OS == "Windows":
            log("  Installing PyTorch with CUDA 12.1…")
            pip_run("install", "--quiet", "torch",
                    "--index-url", "https://download.pytorch.org/whl/cu121")
        else:
            log("  Installing PyTorch (CPU)…")
            pip_run("install", "--quiet", "torch")
        ok("  PyTorch installed")
        _install_pkg_list(GRAMMAR_PACKAGES)
    else:
        warn("Grammar packages skipped — enable later via Update & Repair.")

    # Tor P2P packages
    if tor_p2p:
        _hdr(f"  Tor P2P packages ({len(TOR_PACKAGES)})  → Tor network")
        _install_pkg_list(TOR_PACKAGES)
    else:
        dim("Tor P2P packages skipped — online.py will not be installed.")

    # Google Translate
    if translate:
        _hdr("  Translation packages  → translate.googleapis.com")
        _install_pkg_list(TRANSLATE_PACKAGES)
    else:
        dim("Translation packages skipped.")

    # LLM (localhost only)
    if llm:
        _hdr(f"  LLM packages ({len(LLM_PACKAGES)})  [local Ollama daemon]")
        _install_pkg_list(LLM_PACKAGES)
        try:
            import importlib
            nltk_mod = importlib.import_module("nltk")
            log("  Downloading NLTK tokenizer data…")
            nltk_mod.download("punkt",     quiet=True)
            nltk_mod.download("punkt_tab", quiet=True)
            nltk_mod.download("stopwords", quiet=True)
            ok("  NLTK data downloaded")
        except Exception as e:
            warn(f"  NLTK data download failed: {e}")
    else:
        dim("LLM packages skipped.")

    # Noise suppression
    if noise:
        _hdr(f"  Noise suppression ({len(NOISE_PACKAGES)})  [local only]")
        _install_pkg_list(NOISE_PACKAGES)
    else:
        dim("Noise suppression skipped.")

    # WebRTC VAD
    if vad:
        _hdr("  WebRTC VAD  [local only]")
        _install_pkg_list(VAD_PACKAGES)
    else:
        dim("webrtcvad skipped — energy-gate VAD fallback will be used.")

    ok("Python packages complete")

    # Step 5 — copy source files
    _hdr("Step 5 / 8  Installing Spoaken Source Files")
    copy_source_files(install_dir, include_tor_p2p=tor_p2p)

    # Step 6 — Whisper model (HuggingFace Hub)
    _hdr(f"Step 6 / 8  Whisper Model [{whisper_model}]")
    if predownload_whisper:
        try:
            preload_whisper(whisper_model, whisper_dir)
        except Exception as e:
            warn(f"Whisper pre-download failed: {e}")
            warn("The model will download automatically on first launch.")
    else:
        dim("Whisper pre-download skipped — model downloads on first launch.")

    # Step 7 — Vosk model (alphacephei.com)
    _hdr(f"Step 7 / 8  Vosk Model [{vosk_model or 'disabled'}]")
    if vosk_enabled and vosk_model:
        try:
            install_vosk(vosk_model, vosk_dir)
        except Exception as e:
            warn(f"Vosk model install failed: {e}")
            warn("Retry: python install.py --vosk-only")
    else:
        dim("Vosk disabled — skipping model download.")

    # Step 8 — config + shortcut
    _hdr("Step 8 / 8  Configuration + Desktop Shortcut")
    try:
        write_config(
            cfg, install_dir, venv_dir,
            include_tor_p2p=tor_p2p,
            include_translate=translate,
            include_grammar=grammar,
        )
    except Exception as e:
        warn(f"Config write failed: {e}")
    try:
        create_shortcut(install_dir)
    except Exception as e:
        warn(f"Shortcut failed: {e}")

    # ── Done ──────────────────────────────────────────────────────────────────
    venv_py_disp = venv_dir / ("Scripts/python.exe" if OS == "Windows" else "bin/python")
    print(f"""
{GREEN}╔════════════════════════════════════════════════════════╗
║   Spoaken installed successfully!                      ║
╚════════════════════════════════════════════════════════╝{NC}

  Location  : {CYAN}{install_dir}{NC}
  Config    : {CYAN}{install_dir / "spoaken_config.json"}{NC}
  Venv      : {CYAN}{venv_dir}{NC}

  {BOLD}External connections installed:{NC}
    Whisper model  : {'HuggingFace Hub (pre-downloaded)' if predownload_whisper else 'HuggingFace Hub (downloads on first launch)'}
    Vosk model     : {'alphacephei.com (downloaded)' if vosk_enabled else 'not installed'}
    Grammar T5     : {'HuggingFace Hub (downloads on first Polish)' if grammar else 'not installed'}
    Tor P2P        : {'Tor network (stem + PySocks installed)' if tor_p2p else 'not installed'}
    Translation    : {'Google Translate API (deep-translator installed)' if translate else 'not installed'}

  {BOLD}Launch:{NC}
    {CYAN}cd {install_dir} && ./run.sh{NC}
    {CYAN}{venv_py_disp} -m spoaken{NC}
""")
    if not vad:
        warn("webrtcvad not installed — energy-gate VAD fallback is active.")
    if not grammar:
        warn("Grammar correction not installed — enable later via Update & Repair.")
    if not tor_p2p:
        dim("Tor P2P not installed — LAN chat still works.  Add later: python install.py --online-only")

# ══════════════════════════════════════════════════════════════════════════════
# --online-only patch mode
# ══════════════════════════════════════════════════════════════════════════════
def run_online_only(cfg_path_str: str):
    """Add Tor P2P and translation packages to an existing install."""
    global _VENV_PYTHON
    log("Online-only patch: adding Tor P2P and translation packages…")

    # Restore venv Python from the saved config's install_dir.
    cfg_p = Path(cfg_path_str)
    idir  = Path.home() / "Spoaken"
    if cfg_p.exists():
        with open(cfg_p, encoding="utf-8") as fh:
            saved = json.load(fh)
        idir = Path(saved.get("install_dir", str(idir)))

    for cand in [idir/"venv"/"bin"/"python", idir/"venv"/"Scripts"/"python.exe"]:
        if cand.exists():
            _VENV_PYTHON = cand
            ok(f"Using venv: {_VENV_PYTHON}")
            break

    if _VENV_PYTHON is None:
        warn("Could not find venv — installing into system Python instead.")

    print()
    print(f"{CYAN}── Tor P2P packages  {YELLOW}[EXTERNAL]{NC}  Tor network")
    install_tor = ask("Install Tor P2P packages?", default_yes=True)
    if install_tor:
        _hdr(f"Tor P2P packages ({len(TOR_PACKAGES)})")
        _install_pkg_list(TOR_PACKAGES)
        if OS == "Linux":
            install_tor_syspkg()

    print()
    print(f"{CYAN}── Translation  {YELLOW}[EXTERNAL]{NC}  translate.googleapis.com")
    install_translate = ask("Install Google Translate packages?", default_yes=True)
    if install_translate:
        _hdr("Translation packages")
        _install_pkg_list(TRANSLATE_PACKAGES)

    # Copy online.py if tor was selected.
    if install_tor and cfg_p.exists():
        online_src  = SCRIPT_DIR / "online.py"
        online_dest = idir / "spoaken" / "network" / "online.py"
        if online_src.exists():
            online_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(online_src, online_dest)
            ok(f"Copied online.py → {online_dest}")
        else:
            warn("online.py not found in source directory — skipping.")

    # Update config flags.
    if cfg_p.exists():
        try:
            with open(cfg_p, encoding="utf-8") as fh:
                saved = json.load(fh)
            if install_tor:
                saved["tor_p2p_enabled"]  = True
            if install_translate:
                saved["translation_enabled"] = True
            if install_tor or install_translate:
                saved["offline_mode"] = False
            tmp = cfg_p.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(saved, fh, indent=2)
            tmp.replace(cfg_p)
            ok(f"Config updated: {cfg_p}")
        except Exception as e:
            warn(f"Could not update config: {e}")

    ok("Online-only patch complete.")

# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Spoaken Installer — prompts for each external server group.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python install.py                    # default: prompts for each feature group
  python install.py --interactive      # same as default
  python install.py --config my.json   # re-run from saved config
  python install.py --online-only      # add Tor/translate to existing install
  python install.py --vosk-only        # re-download Vosk model only
  python install.py --noise            # add noise suppression to existing install
  python install.py --llm              # add LLM packages to existing install
        """,
    )
    p.add_argument("--config",      default="spoaken_config.json",
                   help="Path to saved config JSON.")
    p.add_argument("--interactive", action="store_true",
                   help="Run full interactive setup (same as default).")
    p.add_argument("--vosk-only",   action="store_true",
                   help="Re-download Vosk model only.")
    p.add_argument("--online-only", action="store_true",
                   help="Add Tor P2P / translate packages to existing install.")
    p.add_argument("--noise",       action="store_true",
                   help="Add noise suppression to existing install.")
    p.add_argument("--llm",         action="store_true",
                   help="Add LLM packages to existing install.")
    p.add_argument("--no-vad",      action="store_true",
                   help="Skip webrtcvad (energy-gate VAD fallback).")
    p.add_argument("--chat",        action="store_true",
                   help="Enable LAN chat server in config.")
    args = p.parse_args()

    # ── --online-only ─────────────────────────────────────────────────────────
    if args.online_only:
        try:
            run_online_only(args.config)
        except Exception as e:
            err(f"Fatal: {e}")
            import traceback; traceback.print_exc()
            sys.exit(1)
        sys.exit(0)

    # ── Load or build config ──────────────────────────────────────────────────
    if os.path.exists(args.config) and not args.interactive:
        with open(args.config, encoding="utf-8") as fh:
            cfg = json.load(fh)
        log(f"Loaded config from {args.config}")
        # Apply any CLI add-on flags on top of saved config.
        if args.noise:   cfg["noise"]  = True
        if args.llm:     cfg["llm"]    = True
        if args.no_vad:  cfg["vad"]    = False
        if args.chat:    cfg["chat_server_enabled"] = True
    else:
        # Always go through the prompt flow — no auto-detection.
        cfg = prompt_install_choices()
        if args.noise:   cfg["noise"]  = True
        if args.llm:     cfg["llm"]    = True
        if args.no_vad:  cfg["vad"]    = False
        if args.chat:    cfg["chat_server_enabled"] = True

    # ── --vosk-only ───────────────────────────────────────────────────────────
    if args.vosk_only:
        vm   = cfg.get("vosk_model")
        if not vm:
            err("No vosk_model in config.")
            sys.exit(1)
        idir = Path(cfg.get("install_dir", str(Path.home() / "Spoaken")))
        for cand in [idir/"venv"/"bin"/"python", idir/"venv"/"Scripts"/"python.exe"]:
            if cand.exists():
                _VENV_PYTHON = cand
                break
        install_vosk(vm, idir / "models" / "vosk")
        sys.exit(0)

    # ── Full install ──────────────────────────────────────────────────────────
    try:
        run_install(cfg)
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Installation cancelled.{NC}")
        sys.exit(1)
    except Exception as e:
        err(f"Fatal: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
