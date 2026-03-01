#!/usr/bin/env python3
"""
Spoaken — Cross-Platform Installer Backend
Supports: Windows 10/11 · macOS 12+ · Ubuntu/Debian · Fedora/RHEL · Arch Linux
Usage:
    python install.py                          # reads spoaken_config.json
    python install.py --config my_config.json  # custom config path
    python install.py --interactive            # CLI prompt mode
    #
"""

import sys
import os
import platform
import subprocess
import json
import urllib.request
import urllib.error
import shutil
import zipfile
import tarfile
import tempfile
import argparse
import time
import hashlib
import re
from pathlib import Path
from typing import Optional

# ─── Terminal colours (graceful fallback on Windows without ANSI) ─────────────
try:
    import ctypes
    kernel = ctypes.windll.kernel32
    kernel.SetConsoleMode(kernel.GetStdHandle(-11), 7)
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
{CYAN}╔══════════════════════════════════════════════════════╗
║         SPOAKEN — Installer v2.1.0                   ║
║         Voice-to-Text Engine · Whisper + Vosk        ║
╚══════════════════════════════════════════════════════╝{NC}
"""

def log(msg):    print(f"{CYAN}[Spoaken]{NC} {msg}")
def ok(msg):     print(f"{GREEN}  [✔]{NC} {msg}")
def warn(msg):   print(f"{YELLOW}  [!]{NC} {msg}")
def err(msg):    print(f"{RED}  [✘]{NC} {msg}")
def step(n, msg):print(f"\n{BOLD}{CYAN}── Step {n}: {msg}{NC}")
def bar(pct, width=40):
    filled = int(width * pct / 100)
    b = "█" * filled + "░" * (width - filled)
    return f"{CYAN}[{b}]{NC} {pct:3d}%"

# ─── Platform detection ───────────────────────────────────────────────────────
OS = platform.system()   # Windows | Darwin | Linux

# ─── Internet connectivity probe ─────────────────────────────────────────────
def check_internet(timeout: float = 4.0) -> bool:
    """
    Return True if the machine has an active internet connection.
    Tries two well-known hosts so a single firewall rule can't fool it.
    """
    import socket
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
        try:
            socket.setdefaulttimeout(timeout)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect((host, port))
            return True
        except OSError:
            continue
    return False

def detect_linux_distro():
    for pm in ("apt", "dnf", "pacman"):
        if shutil.which(pm):
            return pm
    return None

def detect_wayland():
    return (
        os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        or bool(os.environ.get("WAYLAND_DISPLAY"))
    )

def python_exe():
    """Return the current Python executable path."""
    return sys.executable

def pip_run(*args, check=True):
    """Run pip as a subprocess via the current Python interpreter."""
    cmd = [python_exe(), "-m", "pip"] + list(args)
    # On Debian-based systems newer pip needs --break-system-packages
    if OS == "Linux":
        distro = detect_linux_distro()
        if distro == "apt" and "--break-system-packages" not in args:
            if args and args[0] in ("install", "upgrade"):
                cmd.append("--break-system-packages")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result

def run(cmd, check=True, shell=False, capture=False):
    """Thin wrapper around subprocess.run with unified error handling."""
    kw = {"shell": shell, "text": True}
    if capture:
        kw.update({"capture_output": True})
    result = subprocess.run(cmd, **kw)
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    return result

# ─── Download helpers ─────────────────────────────────────────────────────────
def download_with_progress(url: str, dest: Path, label: str, retries: int = 3):
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                chunk_size = 65536
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = int(100 * downloaded / total)
                            mb_done = downloaded / 1_048_576
                            mb_total = total / 1_048_576
                            print(f"\r  {bar(pct)} {mb_done:.1f}/{mb_total:.1f} MB  {label}  ", end="", flush=True)
            print() 
            return dest
        except (urllib.error.URLError, OSError) as e:
            warn(f"Attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                time.sleep(2 * attempt)
            else:
                raise RuntimeError(f"Failed to download {label} after {retries} attempts.")

def extract_archive(archive: Path, dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
    elif archive.suffix in (".gz", ".bz2", ".xz") or ".tar" in archive.name:
        with tarfile.open(archive) as t:
            t.extractall(dest)
    else:
        raise ValueError(f"Unknown archive type: {archive}")

# ─── System dependency installers ────────────────────────────────────────────
def install_system_deps_windows():
    log("Checking for winget...")
    if not shutil.which("winget"):
        raise RuntimeError(
            "winget not found. Please update Windows to 1.21H2+ "
            "or install App Installer from the Microsoft Store."
        )
    for pkg_id, label in [("Gyan.FFmpeg", "FFmpeg")]:
        log(f"Installing {label} via winget...")
        result = run(["winget", "install", "--id", pkg_id, "-s", "winget",
                      "--silent", "--accept-package-agreements",
                      "--accept-source-agreements"], check=False, capture=True)
        if result.returncode == 0 or "already installed" in (result.stdout + result.stderr).lower():
            ok(f"{label} installed / already present")
        else:
            warn(f"winget could not install {label}. "
                 "Download manually from https://www.gyan.dev/ffmpeg/builds/")

def install_system_deps_macos():
    if not shutil.which("brew"):
        log("Homebrew not found. Installing Homebrew (requires internet)...")
        script_url = "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
        run(["/bin/bash", "-c",
             f'curl -fsSL {script_url} | /bin/bash'], shell=False, check=True)
 
        brew_path = "/opt/homebrew/bin/brew"
        if os.path.exists(brew_path):
            os.environ["PATH"] = f"/opt/homebrew/bin:{os.environ['PATH']}"
        ok("Homebrew installed")
    else:
        ok("Homebrew already installed")

    for pkg in ("ffmpeg", "portaudio"):
        log(f"brew install {pkg}...")
        result = run(["brew", "install", pkg], check=False, capture=True)
        if result.returncode == 0 or "already installed" in (result.stdout + result.stderr).lower():
            ok(f"{pkg} ready")
        else:
            warn(f"Could not brew install {pkg}: {result.stderr.strip()}")

def install_system_deps_linux():
    pm = detect_linux_distro()
    if pm is None:
        raise RuntimeError(
            "Unsupported Linux distribution. "
            "Please manually install: ffmpeg portaudio python3-tk wmctrl xdotool"
        )

    pkg_map = {
        "apt": [
            "ffmpeg", "portaudio19-dev", "python3-dev",
            "python3-pip", "python3-tk", "wmctrl",
            "xdotool", "libgirepository1.0-dev", "pkg-config",
            "tor", "build-essential",
        ],
        "dnf": [
            "ffmpeg", "portaudio-devel", "python3-devel",
            "python3-pip", "python3-tkinter", "wmctrl",
            "xdotool", "gobject-introspection-devel",
            "tor", "gcc", "gcc-c++",
        ],
        "pacman": [
            "ffmpeg", "portaudio", "python",
            "python-pip", "tk", "wmctrl", "xdotool",
            "tor", "base-devel",
        ],
    }

    if pm == "apt":
        run(["sudo", "apt", "update", "-qq"], check=False)
        run(["sudo", "apt", "install", "-y"] + pkg_map["apt"])
    elif pm == "dnf":
        # Enable RPM Fusion for ffmpeg
        fedora_ver_cmd = run(["rpm", "-E", "%fedora"], capture=True, check=False)
        fver = fedora_ver_cmd.stdout.strip() if fedora_ver_cmd.returncode == 0 else "39"
        for rpm_url in [
            f"https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-{fver}.noarch.rpm",
            f"https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-{fver}.noarch.rpm",
        ]:
            run(["sudo", "dnf", "install", "-y", rpm_url], check=False)
        run(["sudo", "dnf", "install", "-y"] + pkg_map["dnf"])
    elif pm == "pacman":
        run(["sudo", "pacman", "-Syu", "--noconfirm"] + pkg_map["pacman"])

    ok("System packages installed")

    if detect_wayland():
        warn("Wayland session detected. wmctrl/xdotool need X11.")
        warn("Spoaken will automatically fall back to pyautogui for window writing.")
        warn("For native targeting, log out and choose the Xorg session at login.")

# ─── Python / pip bootstrap ───────────────────────────────────────────────────
def ensure_pip():
    log("Upgrading pip, setuptools, wheel...")
    pip_run("install", "--upgrade", "pip", "setuptools", "wheel")
    ok("pip up to date")

# ─── Python packages ──────────────────────────────────────────────────────────

# ── Online-only source files ───────────────────────────────────────────────────
# These files are excluded from the install when running in offline mode.
# Spoaken starts, transcribes, and writes to windows without any of them.
ONLINE_ONLY_SOURCE_FILES = {
    "spoaken_chat_online.py",   # Internet relay server / client
}

# Always installed — Spoaken will not function without these.
# All packages here work fully offline after installation.
COMMON_PACKAGES = [
    "customtkinter>=5.2.2",
    "Pillow>=10.0.0",
    "faster-whisper>=1.0.3",
    "sounddevice>=0.4.7",
    "numpy>=1.26.0",
    "pyautogui>=0.9.54",
    "rapidfuzz>=3.6.1",
    "websockets>=12.0",        # LAN chat (works fully offline on the local network)
    "cryptography>=42.0.0",    # Ed25519 identity keys, TLS, AES-GCM
]

# Packages that are only meaningful with an active internet connection.
# Installed in online mode; skipped (with a note) in offline mode.
ONLINE_ONLY_PACKAGES = [
    "stem>=1.8.2",             # Tor hidden service control (requires Tor daemon + internet)
    "PySocks>=1.7.1",          # Tor SOCKS5 proxy for outbound .onion connections
    "torpy>=1.1.8",            # Pure-Python Tor client (requires internet)
    "aiohttp>=3.9.0",          # Async HTTP — used by online relay and update checker
    "aiofiles>=23.2.1",        # Async file I/O for online chat file transfers
    "deep-translator>=1.11.4", # Google Translate API (cloud, requires internet)
]

# Grammar correction — installed unless the user opts out.
GRAMMAR_PACKAGES = [
    "happytransformer<4.0.0",
    "transformers>=4.40.0",
    "torch",                   # version handled separately (CPU vs CUDA)
    "sentencepiece>=0.2.0",    # required by many T5 tokenizers
    "protobuf>=4.25.0",        # required by sentencepiece / transformers
]

# Noise suppression — optional, improves transcription in noisy rooms.
NOISE_PACKAGES = [
    "noisereduce>=3.0.0",
    "scipy>=1.12.0",           # noisereduce dependency; also useful for audio processing
]

# LLM (Ollama client + summarization pipeline) — optional.
# Ollama itself runs locally; models are pulled once (requires internet at model-download time).
LLM_PACKAGES = [
    "ollama>=0.2.0",
    "sumy>=0.11.0",
    "nltk>=3.8.1",
    "scikit-learn>=1.4.0",
    "networkx>=3.3",           # used by some sumy summarizers
]

# Better VAD — optional but strongly recommended on systems where it builds cleanly.
VAD_PACKAGES = [
    "webrtcvad",               # no version pin — prebuilt wheels vary by platform
]

PLATFORM_EXTRA = {
    "Windows": ["pywin32>=306", "pywinauto>=0.6.8"],
    "Darwin":  [],
    "Linux":   [],
}

def install_python_packages(gpu: bool = False, grammar: bool = True,
                             noise: bool = False, translation: bool = False,
                             llm: bool = False, vad: bool = True,
                             online: bool = True):
    # ── PyTorch (handled separately for CUDA support) ──────────────────────────
    if grammar:
        if gpu and OS == "Windows":
            log("Installing PyTorch with CUDA 12.1 support...")
            pip_run("install", "torch", "--index-url",
                    "https://download.pytorch.org/whl/cu121")
        else:
            log("Installing PyTorch (CPU)...")
            pip_run("install", "torch")
        ok("PyTorch installed")

    # ── Core packages (offline-safe) ───────────────────────────────────────────
    platform_extras = PLATFORM_EXTRA.get(OS, [])
    core_pkgs = COMMON_PACKAGES + platform_extras
    total_core = len(core_pkgs)
    log(f"Installing {total_core} core packages...")
    for i, pkg in enumerate(core_pkgs, 1):
        log(f"  [{i}/{total_core}] {pkg}")
        try:
            pip_run("install", "--upgrade", pkg)
            ok(pkg)
        except RuntimeError as e:
            warn(f"Could not install {pkg}: {e}")

    # ── Online-only packages ───────────────────────────────────────────────────
    if online:
        log(f"Installing {len(ONLINE_ONLY_PACKAGES)} online-feature packages "
            f"(Tor, aiohttp, deep-translator)...")
        for pkg in ONLINE_ONLY_PACKAGES:
            try:
                pip_run("install", "--upgrade", pkg)
                ok(pkg)
            except RuntimeError as e:
                warn(f"Could not install {pkg}: {e}")
    else:
        short_names = ", ".join(p.split(">=")[0].split("<")[0] for p in ONLINE_ONLY_PACKAGES)
        warn(f"OFFLINE MODE — skipping online-only packages ({short_names}).")
        warn("  To add them later:  python install.py --online-only")

    # ── Grammar packages ───────────────────────────────────────────────────────
    if grammar:
        log("Installing grammar correction packages...")
        for pkg in GRAMMAR_PACKAGES:
            if pkg == "torch":
                continue    # already handled above
            try:
                pip_run("install", "--upgrade", pkg)
                ok(pkg)
            except RuntimeError as e:
                warn(f"Could not install {pkg}: {e}")
    else:
        warn("Grammar correction skipped (disabled in config).")

    # ── Optional: noise suppression ────────────────────────────────────────────
    if noise:
        log("Installing noise suppression packages...")
        for pkg in NOISE_PACKAGES:
            try:
                pip_run("install", "--upgrade", pkg)
                ok(pkg)
            except RuntimeError as e:
                warn(f"Could not install {pkg}: {e}")
    else:
        log("Noise suppression skipped (enable later via Update window or --noise flag).")

    # ── Optional: translation (online only) ───────────────────────────────────
    if translation:
        if online:
            log("deep-translator already included in online-only packages above.")
        else:
            warn("Translation (deep-translator) requires internet — skipped in offline mode.")
            warn("  It will be installed automatically if you re-run with --online-only.")

    # ── Optional: LLM + summarization ─────────────────────────────────────────
    if llm:
        log("Installing LLM + summarization packages...")
        for pkg in LLM_PACKAGES:
            try:
                pip_run("install", "--upgrade", pkg)
                ok(pkg)
            except RuntimeError as e:
                warn(f"Could not install {pkg}: {e}")
        # Download NLTK punkt data needed by sumy
        try:
            import nltk
            log("Downloading NLTK tokenizer data...")
            nltk.download("punkt",     quiet=True)
            nltk.download("punkt_tab", quiet=True)
            nltk.download("stopwords", quiet=True)
            ok("NLTK data downloaded")
        except Exception as e:
            warn(f"NLTK data download failed: {e}")
    else:
        log("LLM/summarization skipped (enable later via Update window or --llm flag).")

    # ── Optional: better VAD ───────────────────────────────────────────────────
    if vad:
        log("Installing webrtcvad (better Voice Activity Detection)...")
        try:
            pip_run("install", "--upgrade", "webrtcvad")
            ok("webrtcvad installed")
        except RuntimeError as e:
            warn(
                f"webrtcvad build failed ({e}). "
                "Spoaken will use the built-in energy-gate fallback instead.\n"
                "  Linux fix:  sudo apt install python3-dev build-essential\n"
                "  Windows fix: install Visual C++ Build Tools from "
                "https://visualstudio.microsoft.com/visual-cpp-build-tools/"
            )

    ok("Python package installation complete")

# ─── Whisper model pre-download ───────────────────────────────────────────────
def preload_whisper_model(model_name: str, models_dir: Path):
    """
    faster-whisper downloads from HuggingFace on first use.
    We trigger that download now so the user sees it happen during install.
    """
    models_dir.mkdir(parents=True, exist_ok=True)
    log(f"Pre-downloading Whisper model: {model_name}")
    log("(This may take several minutes depending on model size and connection speed)")

    script = f"""
import sys
sys.stdout.reconfigure(line_buffering=True)
from faster_whisper import WhisperModel
import os
cache = r"{models_dir}"
os.makedirs(cache, exist_ok=True)
print("Connecting to HuggingFace Hub...", flush=True)
m = WhisperModel("{model_name}", device="cpu", compute_type="int8",
                  download_root=cache)
print("Model ready.", flush=True)
"""
    tmp = Path(tempfile.mktemp(suffix=".py"))
    tmp.write_text(script)
    try:
        result = subprocess.run([python_exe(), str(tmp)],
                                text=True, capture_output=False)
        if result.returncode != 0:
            warn("Whisper model pre-download encountered an issue. "
                 "It will be downloaded automatically on first launch.")
        else:
            ok(f"Whisper model '{model_name}' cached at {models_dir}")
    finally:
        tmp.unlink(missing_ok=True)

# ─── Vosk model download ─────────────────────────────────────────────────────
VOSK_MODELS = {
    "vosk-model-small-en-us-0.15":      "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip",
    "vosk-model-en-us-0.22":            "https://alphacephei.com/vosk/models/vosk-model-en-us-0.22.zip",
    "vosk-model-en-us-0.42-gigaspeech": "https://alphacephei.com/vosk/models/vosk-model-en-us-0.42-gigaspeech.zip",
}

def install_vosk_model(model_name: str, models_dir: Path):
    if model_name not in VOSK_MODELS:
        raise ValueError(f"Unknown Vosk model: {model_name}")

    # Install vosk Python package
    log("Installing vosk Python package...")
    pip_run("install", "vosk")
    ok("vosk package installed")

    url = VOSK_MODELS[model_name]
    archive_path = models_dir / f"{model_name}.zip"
    extract_path = models_dir / model_name

    if extract_path.exists():
        ok(f"Vosk model '{model_name}' already present at {extract_path}")
        return

    models_dir.mkdir(parents=True, exist_ok=True)
    log(f"Downloading Vosk model: {model_name}")
    download_with_progress(url, archive_path, model_name)
    ok("Download complete. Extracting...")
    extract_archive(archive_path, models_dir)
    archive_path.unlink(missing_ok=True)  # clean up zip
    ok(f"Vosk model ready at {extract_path}")

# ─── Copy source files into install directory ────────────────────────────────
def copy_source_files(script_dir: Path, install_dir: Path, online: bool = True):
    """
    Copy the 'spoaken' sibling folder (next to install.py) into install_dir,
    so install_dir/spoaken/ contains the full application source.

    In offline mode, files listed in ONLINE_ONLY_SOURCE_FILES are excluded
    so there is no dead code referencing cloud APIs that can never be reached.
    """
    src = script_dir / "spoaken"

    if not src.exists() or not src.is_dir():
        warn(f"'spoaken' folder not found at {src}. "
             "Make sure install.py sits alongside the 'spoaken/' source folder.")
        return

    dest = install_dir / "spoaken"

    if dest.exists():
        log(f"Removing old copy at {dest}...")
        shutil.rmtree(dest)

    install_dir.mkdir(parents=True, exist_ok=True)

    if online:
        # Full copy — include everything
        shutil.copytree(src, dest)
        ok(f"Copied spoaken/ → {dest}  (online mode — all files included)")
    else:
        # Selective copy — exclude online-only source files
        skipped = []

        def _ignore(directory, contents):
            ignored = set()
            for name in contents:
                if name in ONLINE_ONLY_SOURCE_FILES:
                    ignored.add(name)
                    skipped.append(name)
            return ignored

        shutil.copytree(src, dest, ignore=_ignore)
        ok(f"Copied spoaken/ → {dest}  (offline mode)")
        if skipped:
            warn(f"Excluded online-only source files: {', '.join(skipped)}")
            warn("  To restore them later: python install.py --online-only")


# ─── Desktop shortcut creator ────────────────────────────────────────────────
def create_shortcut(install_dir: Path, script_dir: Path):
    """
    Create an OS-appropriate launcher/shortcut for spoaken_main.py.
    Looks for Art/logo.ico (Windows) or Art/logo.png (macOS/Linux) in
    script_dir; falls back gracefully if the Art folder is absent.
    """
    main_py   = install_dir / "spoaken" / "spoaken_main.py"
    art_dir   = script_dir / "Art"
    ico_path  = art_dir / "logo.ico"
    png_path  = art_dir / "logo.png"

    # --- Windows: .lnk via PowerShell ----------------------------------------
    if OS == "Windows":
        desktop = Path(os.path.expanduser("~")) / "Desktop"
        lnk     = desktop / "Spoaken.lnk"

        icon_arg = f'$s.IconLocation = "{ico_path}"' if ico_path.exists() else ""

        ps = (
            f'$ws = New-Object -ComObject WScript.Shell; '
            f'$s = $ws.CreateShortcut("{lnk}"); '
            f'$s.TargetPath = "{python_exe()}"; '
            f'$s.Arguments = \\"{main_py}\\"; '
            f'$s.WorkingDirectory = "{install_dir}"; '
            f'{icon_arg} '
            f'$s.Save()'
        )
        result = run(
            ["powershell", "-NoProfile", "-Command", ps],
            check=False, capture=True
        )
        if result.returncode == 0:
            ok(f"Desktop shortcut created → {lnk}")
        else:
            warn(f"Could not create Windows shortcut: {result.stderr.strip()}")

    # --- macOS: shell-script .command in /Applications ------------------------
    elif OS == "Darwin":
        launcher = Path("/Applications") / "Spoaken.command"
        content  = (
            "#!/usr/bin/env bash\n"
            f'cd "{install_dir}"\n'
            f'exec "{python_exe()}" "{main_py}"\n'
        )
        try:
            launcher.write_text(content)
            launcher.chmod(0o755)
            ok(f"Launcher created → {launcher}")
            if png_path.exists():
                # Use AppleScript to attach the icon
                run(
                    ["osascript", "-e",
                     f'tell application "Finder" to set file "{launcher}" '
                     f'to use icon of file "{png_path}"'],
                    check=False, capture=True
                )
                ok("Custom icon applied to macOS launcher")
        except PermissionError:
            warn("Could not write to /Applications. "
                 "Try re-running with sudo, or create the launcher manually.")

    # --- Linux: .desktop file -------------------------------------------------
    elif OS == "Linux":
        apps_dir = Path(os.path.expanduser("~/.local/share/applications"))
        apps_dir.mkdir(parents=True, exist_ok=True)
        desktop_file = apps_dir / "spoaken.desktop"

        icon_line = f"Icon={png_path}" if png_path.exists() else "Icon=audio-input-microphone"

        content = (
            "[Desktop Entry]\n"
            "Version=1.0\n"
            "Type=Application\n"
            "Name=Spoaken\n"
            "Comment=Voice-to-Text Engine\n"
            f"Exec={python_exe()} {main_py}\n"
            f"Path={install_dir}\n"
            f"{icon_line}\n"
            "Terminal=false\n"
            "Categories=Utility;Accessibility;\n"
        )
        desktop_file.write_text(content)
        desktop_file.chmod(0o755)
        ok(f".desktop entry created → {desktop_file}")

        # Also drop a copy on the user's Desktop if it exists
        user_desktop = Path(os.path.expanduser("~/Desktop"))
        if user_desktop.is_dir():
            desk_copy = user_desktop / "Spoaken.desktop"
            shutil.copy2(desktop_file, desk_copy)
            desk_copy.chmod(0o755)
            ok(f"Desktop shortcut also placed at → {desk_copy}")

        # Update the desktop database so the app appears in menus
        if shutil.which("update-desktop-database"):
            run(["update-desktop-database", str(apps_dir)], check=False, capture=True)

    else:
        warn(f"Shortcut creation not supported on OS: {OS}")


# ─── Write runtime config ─────────────────────────────────────────────────────
def write_runtime_config(cfg: dict, install_dir: Path, online: bool = True):
    config_path = install_dir / "spoaken_config.json"
    install_dir.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump({
            # ── Network / offline mode ─────────────────────────────────────────
            # offline_mode = True  → spoaken_config.is_online() always returns False
            #                        without probing the network.
            # happy_online_only    → T5 grammar correction only loads from local
            #                        cache; never fetches from HuggingFace at runtime.
            "offline_mode":           not online,
            "happy_online_only":      True,
            # ── Transcription engines ──────────────────────────────────────────
            "whisper_model":          cfg.get("whisper_model", "base.en"),
            "whisper_enabled":        True,
            "whisper_compute":        "auto",
            "vosk_model":             cfg.get("vosk_model", None),
            "vosk_enabled":           cfg.get("vosk_enabled", True),
            "enable_giga_model":      False,
            "vosk_model_accurate":    "vosk-model-en-us-0.42-gigaspeech",
            # ── Hardware ──────────────────────────────────────────────────────
            "gpu_enabled":            cfg.get("gpu", True),
            "mic_device":             None,
            "noise_suppression":      cfg.get("noise", True),
            # ── Grammar ───────────────────────────────────────────────────────
            "grammar_enabled":        cfg.get("grammar", True),
            "t5_model":               "vennify/t5-base-grammar-correction",
            # ── Chat / networking ──────────────────────────────────────────────
            "chat_server_enabled":    cfg.get("chat_server_enabled", True),
            "chat_server_port":       cfg.get("chat_server_port", 55300),
            "chat_server_token":      cfg.get("chat_server_token", "spoaken"),
            "android_stream_enabled": cfg.get("android_stream_enabled", True),
            "android_stream_port":    cfg.get("android_stream_port", 55301),
            "bind_address":           "",
            # ── Security / PKI ────────────────────────────────────────────────
            "use_tls":                True,
            "mtls_enabled":           True,
            "beacon_sign":            True,
            "msg_envelope":           False,
            "log_tls_events":         True,
            "token_ttl":              300.0,
            "token_clock_skew":       60.0,
            # ── Memory management ─────────────────────────────────────────────
            "memory_cap_words":       cfg.get("memory_cap_words", 2000),
            "memory_cap_minutes":     cfg.get("memory_cap_minutes", 60),
            # ── Text quality ──────────────────────────────────────────────────
            "duplicate_filter":       True,
            # ── Paths ─────────────────────────────────────────────────────────
            "whisper_dir":            str(install_dir / "models" / "whisper"),
            "vosk_dir":               str(install_dir / "models" / "vosk"),
            "platform":               OS,
            "install_dir":            str(install_dir),
        }, f, indent=2)
    mode_label = "OFFLINE" if not online else "ONLINE"
    ok(f"Runtime config written to {config_path}  [{mode_label} mode]")

# ─── Interactive CLI fallback ─────────────────────────────────────────────────
def interactive_config() -> dict:
    print(f"\n{CYAN}── Configuration ──────────────────────────────────────────{NC}")

    whisper_options = [
        "tiny.en", "tiny", "base.en", "base", "small.en",
        "small", "medium.en", "medium", "turbo", "large-v3"
    ]
    print("\nWhisper models:")
    for i, m in enumerate(whisper_options, 1):
        print(f"  {i:2d}. {m}")
    while True:
        try:
            choice = int(input("\nSelect Whisper model [default: 3 = base.en]: ") or "3")
            whisper_model = whisper_options[choice - 1]
            break
        except (ValueError, IndexError):
            print("Invalid choice, try again.")

    vosk_options = ["none", "vosk-model-small-en-us-0.15",
                    "vosk-model-en-us-0.22",
                    "vosk-model-en-us-0.42-gigaspeech"]
    print("\nVosk models (optional, for real-time display):")
    for i, m in enumerate(vosk_options, 0):
        suffix = "  ← recommended" if i == 1 else ""
        print(f"  {i}. {m}{suffix}")
    while True:
        try:
            vc = int(input("\nSelect Vosk model [default: 1 = vosk-model-small-en-us-0.15]: ") or "1")
            if vc == 0:
                vosk_model, vosk_enabled = None, False
            else:
                vosk_model, vosk_enabled = vosk_options[vc], True
            break
        except (ValueError, IndexError):
            print("Invalid choice, try again.")

    gpu_raw        = input("\nEnable GPU / CUDA acceleration? [Y/n]: ").strip().lower()
    grammar_raw    = input("Install grammar correction (HappyTransformer/T5)? [Y/n]: ").strip().lower()
    noise_raw      = input("Install noise suppression (noisereduce)? [Y/n]: ").strip().lower()
    llm_raw        = input("Install LLM + summarization packages (ollama, sumy, nltk)? [Y/n]: ").strip().lower()
    vad_raw        = input("Install webrtcvad for better Voice Activity Detection? [Y/n]: ").strip().lower()

    print(f"\n{CYAN}── Online / Offline Mode ───────────────────────────────────{NC}")
    print("  Online mode  : installs Tor/relay/translation packages, copies online source files.")
    print("  Offline mode : skips all cloud-dependent packages and source files.")
    print("                 Translation (deep-translator) and online relay will not be available.")
    print("                 Vosk, Whisper, VAD, LAN chat, and local Ollama work fully offline.")
    online_raw = input("Install in online mode? [Y/n]: ").strip().lower()
    online = online_raw not in ("n", "no")

    print(f"\n{CYAN}── Chat / Networking ──────────────────────────────────────{NC}")
    print("  LAN chat lets other Spoaken users on your network connect to this machine.")
    chat_raw       = input("Enable LAN chat server at startup? [Y/n]: ").strip().lower()
    chat_port      = 55300
    chat_token     = "spoaken"
    if chat_raw not in ("n", "no"):
        port_in = input("  Chat server port [55300]: ").strip()
        chat_port  = int(port_in) if port_in.isdigit() else 55300
        token_in   = input("  Shared auth token [spoaken]: ").strip()
        chat_token = token_in or "spoaken"

    android_raw  = input("Enable Android/browser live transcript stream? [Y/n]: ").strip().lower()
    android_port = 55301
    if android_raw not in ("n", "no"):
        port_in = input("  Stream port [55301]: ").strip()
        android_port = int(port_in) if port_in.isdigit() else 55301

    default_dir = {
        "Windows": "C:\\Program Files\\Spoaken",
        "Darwin":  "/Applications/Spoaken",
        "Linux":   os.path.expanduser("~/spoaken"),
    }.get(OS, os.path.expanduser("~/spoaken"))
    idir = input(f"\nInstall directory [{default_dir}]: ").strip() or default_dir

    return {
        "whisper_model":          whisper_model,
        "vosk_model":             vosk_model,
        "vosk_enabled":           vosk_enabled,
        "gpu":                    gpu_raw not in ("n", "no"),
        "grammar":                grammar_raw not in ("n", "no"),
        "noise":                  noise_raw not in ("n", "no"),
        "translation":            online,   # translation is included automatically in online mode
        "llm":                    llm_raw not in ("n", "no"),
        "vad":                    vad_raw not in ("n", "no"),
        "online":                 online,
        "chat_server_enabled":    chat_raw not in ("n", "no"),
        "chat_server_port":       chat_port,
        "chat_server_token":      chat_token,
        "android_stream_enabled": android_raw not in ("n", "no"),
        "android_stream_port":    android_port,
        "install_dir":            idir,
    }

# ─── Main installation orchestrator ───────────────────────────────────────────
def run_install(cfg: dict):
    install_dir     = Path(cfg.get("install_dir", os.path.expanduser("~/spoaken")))
    whisper_model   = cfg.get("whisper_model", "base.en")
    vosk_model      = cfg.get("vosk_model", None)
    vosk_enabled    = cfg.get("vosk_enabled", True)
    gpu             = cfg.get("gpu", True)
    grammar         = cfg.get("grammar", True)
    noise           = cfg.get("noise", True)
    translation     = cfg.get("translation", True)
    llm             = cfg.get("llm", True)
    vad             = cfg.get("vad", True)
    chat_enabled    = cfg.get("chat_server_enabled", True)
    android_enabled = cfg.get("android_stream_enabled", True)
    whisper_dir     = install_dir / "models" / "whisper"
    vosk_dir        = install_dir / "models" / "vosk"

    # ── Online / offline detection ─────────────────────────────────────────────
    # Priority: explicit CLI/config flag → auto-detect
    if "online" in cfg:
        online = bool(cfg["online"])
        detect_label = "forced via --offline/--online flag"
    else:
        log("Checking internet connectivity...")
        online = check_internet()
        detect_label = "auto-detected"
    translation = translation or online   # deep-translator comes free with online mode

    print(BANNER)
    log(f"Platform    : {OS} ({platform.version()})")
    log(f"Python      : {platform.python_version()} @ {python_exe()}")
    log(f"Install dir : {install_dir}")

    # ── Mode banner ───────────────────────────────────────────────────────────
    if online:
        print(f"\n{GREEN}  ● ONLINE MODE{NC}  ({detect_label})")
        print(f"  {DIM}All packages and source files will be installed, including{NC}")
        print(f"  {DIM}Tor relay, aiohttp, deep-translator, and online chat files.{NC}")
    else:
        print(f"\n{YELLOW}  ○ OFFLINE MODE{NC}  ({detect_label})")
        print(f"  {DIM}Online-only packages and source files will be skipped.{NC}")
        print(f"  {DIM}Vosk, Whisper, VAD, LAN chat, and local Ollama work fully.{NC}")
        print(f"  {DIM}To add online features later: python install.py --online-only{NC}")
    print()

    log(f"Whisper     : {whisper_model}")
    log(f"Vosk        : {vosk_model if vosk_enabled else 'disabled'}")
    log(f"GPU/CUDA    : {'yes' if gpu else 'no'}")
    log(f"Grammar     : {'yes' if grammar else 'no'}")
    log(f"Noise NR    : {'yes' if noise else 'no (install later via Update window)'}")
    log(f"Translation : {'yes (online mode)' if online else 'NO (offline mode)'}")
    log(f"LLM/Summary : {'yes' if llm else 'no (install later via Update window)'}")
    log(f"WebRTC VAD  : {'yes' if vad else 'no'}")
    log(f"LAN chat    : {'enabled (port ' + str(cfg.get('chat_server_port', 55300)) + ')' if chat_enabled else 'disabled'}")
    log(f"Android SSE : {'enabled (port ' + str(cfg.get('android_stream_port', 55301)) + ')' if android_enabled else 'disabled'}")

    # Step 1 — System packages
    step(1, "System Dependencies")
    try:
        if OS == "Windows":
            install_system_deps_windows()
        elif OS == "Darwin":
            install_system_deps_macos()
        elif OS == "Linux":
            install_system_deps_linux()
        else:
            warn(f"Unknown OS '{OS}'. Skipping system packages.")
    except RuntimeError as e:
        err(str(e))
        err("Continuing — some features may not work without system packages.")

    # Step 2 — pip
    step(2, "Python Package Manager")
    ensure_pip()

    # Step 3 — Python packages
    step(3, "Python Packages")
    install_python_packages(
        gpu=gpu,
        grammar=grammar,
        noise=noise,
        translation=translation,
        llm=llm,
        vad=vad,
        online=online,
    )

    # Step 4 — Copy source files into install_dir
    step(4, "Copying Project Files")
    script_dir = Path(__file__).parent.resolve()
    try:
        copy_source_files(script_dir, install_dir, online=online)
    except Exception as e:
        warn(f"File copy encountered an issue: {e}")
        warn("You may need to manually copy the 'spoaken/' folder to the install directory.")

    # Step 5 — Whisper model
    if whisper_model:
        step(5, f"Whisper Model [{whisper_model}]")
        try:
            preload_whisper_model(whisper_model, whisper_dir)
        except Exception as e:
            warn(f"Whisper pre-download failed: {e}")
            warn("The model will download automatically on first launch.")

    # Step 6 — Vosk model (optional)
    if vosk_enabled and vosk_model:
        step(6, f"Vosk Model [{vosk_model}]")
        try:
            install_vosk_model(vosk_model, vosk_dir)
        except Exception as e:
            warn(f"Vosk model install failed: {e}")
            warn("You can retry by running: python install.py --vosk-only")

    # Step 7 — Write config
    step(7, "Writing Configuration")
    try:
        write_runtime_config(cfg, install_dir, online=online)
    except Exception as e:
        warn(f"Unable to write config: {e}")

    # Step 8 — Desktop shortcut
    step(8, "Creating Desktop Shortcut")
    try:
        create_shortcut(install_dir, script_dir)
    except Exception as e:
        warn(f"Shortcut creation failed: {e}")

    # ── Done ────────────────────────────────────────────────────────────────────
    optional_installed = [
        name for name, flag in [
            ("noisereduce", noise), ("deep-translator + Tor (online mode)", online),
            ("ollama + sumy + nltk", llm), ("webrtcvad", vad),
        ] if flag
    ]
    optional_skipped = [
        name for name, flag in [
            ("noisereduce", noise),
            ("ollama + sumy + nltk", llm), ("webrtcvad", vad),
        ] if not flag
    ]

    mode_str = f"{GREEN}ONLINE{NC}" if online else f"{YELLOW}OFFLINE{NC}"
    print(f"""
{GREEN}╔══════════════════════════════════════════════════════╗
║   Installation complete!                             ║
╚══════════════════════════════════════════════════════╝{NC}

  Mode:            {mode_str}
  Launch Spoaken:  {CYAN}python3 spoaken/spoaken_main.py{NC}

  Config file:     {CYAN}{install_dir / "spoaken_config.json"}{NC}
  Whisper models:  {CYAN}{whisper_dir}{NC}
  Vosk models:     {CYAN}{vosk_dir if vosk_enabled else "N/A"}{NC}
""")
    if optional_installed:
        ok(f"Optional packages installed: {', '.join(optional_installed)}")
    if optional_skipped:
        warn(
            f"Skipped optional packages: {', '.join(optional_skipped)}\n"
            "  Install them later from the app's Update & Repair window,\n"
            "  or re-run:  python install.py --interactive"
        )

# ─── Entry point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Spoaken Installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python install.py --interactive              # full guided setup (auto-detects online/offline)
  python install.py --offline                  # force offline install (no internet needed)
  python install.py --online                   # force online install (include Tor, relay, translate)
  python install.py --online-only              # add online packages/files to an existing install
  python install.py --config my_config.json   # re-run from saved config
  python install.py --noise --llm             # add optional packages to existing install
  python install.py --vosk-only               # re-download vosk model only
        """,
    )
    parser.add_argument("--config",       default="spoaken_config.json",
                        help="Path to JSON configuration file")
    parser.add_argument("--interactive",  action="store_true",
                        help="Run interactive CLI configuration")
    parser.add_argument("--vosk-only",    action="store_true",
                        help="Only install/re-download the Vosk model from config")
    # ── Online / offline mode flags ────────────────────────────────────────────
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--offline",
        action="store_true",
        help=(
            "Force offline install: skip online-only packages (Tor, aiohttp, "
            "deep-translator) and online source files. Vosk, Whisper, VAD, "
            "LAN chat, and local Ollama all work fully offline."
        ),
    )
    mode_group.add_argument(
        "--online",
        action="store_true",
        help=(
            "Force online install even if auto-detection fails: "
            "include Tor, aiohttp, deep-translator, and online source files."
        ),
    )
    mode_group.add_argument(
        "--online-only",
        action="store_true",
        help=(
            "Add online-only packages and source files to an existing offline "
            "install WITHOUT re-running the full installation. Also updates "
            "spoaken_config.json to set offline_mode = false."
        ),
    )
    # Optional feature flags
    parser.add_argument("--noise",        action="store_true",
                        help="Install noisereduce for noise suppression")
    parser.add_argument("--translation",  action="store_true",
                        help="Install deep-translator (implies --online)")
    parser.add_argument("--llm",          action="store_true",
                        help="Install ollama, sumy, nltk, scikit-learn for LLM + summarization")
    parser.add_argument("--no-vad",       action="store_true",
                        help="Skip webrtcvad installation (use energy-gate fallback instead)")
    parser.add_argument("--chat",         action="store_true",
                        help="Enable LAN chat server in the written config")
    args = parser.parse_args()

    # ── --online-only: patch an existing offline install ──────────────────────
    if args.online_only:
        log("Online-only mode: adding online packages and files to existing install...")

        log(f"Installing {len(ONLINE_ONLY_PACKAGES)} online-only packages...")
        for pkg in ONLINE_ONLY_PACKAGES:
            try:
                pip_run("install", "--upgrade", pkg)
                ok(pkg)
            except RuntimeError as e:
                warn(f"Could not install {pkg}: {e}")

        # Copy online source files if the install dir can be found from config
        cfg_path = Path(args.config)
        if cfg_path.exists():
            with open(cfg_path) as f:
                _c = json.load(f)
            install_dir = Path(_c.get("install_dir", os.path.expanduser("~/spoaken")))
            script_dir  = Path(__file__).parent.resolve()
            src_spoaken = script_dir / "spoaken"
            dest_spoaken = install_dir / "spoaken"
            if src_spoaken.exists() and dest_spoaken.exists():
                copied = []
                for fname in ONLINE_ONLY_SOURCE_FILES:
                    src_f  = src_spoaken / fname
                    dest_f = dest_spoaken / fname
                    if src_f.exists():
                        shutil.copy2(src_f, dest_f)
                        ok(f"Copied {fname}")
                        copied.append(fname)
                    else:
                        warn(f"{fname} not found in source — skipping")
            # Patch config: set offline_mode = false
            try:
                _c["offline_mode"] = False
                cfg_path_dest = install_dir / "spoaken_config.json"
                target = cfg_path_dest if cfg_path_dest.exists() else cfg_path
                with open(target, "w") as f:
                    json.dump(_c, f, indent=2)
                ok(f"Config updated: offline_mode = false  ({target})")
            except Exception as e:
                warn(f"Could not update config: {e}")
        else:
            warn(f"Config file '{args.config}' not found — skipping source file copy.")
            warn("  Online packages were installed. Manually copy online source files if needed.")

        ok("Online-only patch complete.")
        sys.exit(0)

    # ── Determine config ───────────────────────────────────────────────────────
    if args.interactive:
        cfg = interactive_config()
    elif os.path.exists(args.config):
        with open(args.config) as f:
            cfg = json.load(f)
        log(f"Loaded config from {args.config}")
    else:
        warn(f"No config file found at '{args.config}'.")
        ans = input("Run interactive configuration instead? [Y/n]: ").strip().lower()
        if ans in ("n", "no"):
            sys.exit(0)
        cfg = interactive_config()

    # ── CLI flags override config file values ──────────────────────────────────
    if args.offline:
        cfg["online"] = False
    elif args.online or args.translation:
        cfg["online"] = True
    # (if neither is set, run_install will auto-detect)

    if args.noise:
        cfg["noise"] = True
    if args.translation:
        cfg["translation"] = True
    if args.llm:
        cfg["llm"] = True
    if args.no_vad:
        cfg["vad"] = False
    if args.chat:
        cfg["chat_server_enabled"] = True

    # ── Vosk-only mode ─────────────────────────────────────────────────────────
    if args.vosk_only:
        vm = cfg.get("vosk_model")
        if not vm:
            err("No vosk_model specified in config.")
            sys.exit(1)
        install_dir = Path(cfg.get("install_dir", os.path.expanduser("~/spoaken")))
        install_vosk_model(vm, install_dir / "models" / "vosk")
        sys.exit(0)

    try:
        run_install(cfg)
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Installation cancelled by user.{NC}")
        sys.exit(1)
    except Exception as exc:
        err(f"Fatal error: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
