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
║         SPOAKEN — Installer v1.0.0                   ║
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
        "apt-get": [
            "ffmpeg", "portaudio19-dev", "python3-dev",
            "python3-pip", "python3-tk", "wmctrl",
            "xdotool", "libgirepository1.0-dev", "pkg-config"
        ],
        "dnf": [
            "ffmpeg", "portaudio-devel", "python3-devel",
            "python3-pip", "python3-tkinter", "wmctrl",
            "xdotool", "gobject-introspection-devel"
        ],
        "pacman": [
            "ffmpeg", "portaudio", "python",
            "python-pip", "tk", "wmctrl", "xdotool"
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
# ─── Python packages ──────────────────────────────────────────────────────────

# Always installed — Spoaken will not function without these.
COMMON_PACKAGES = [
    "customtkinter",
    "Pillow",
    "faster-whisper",
    "sounddevice",
    "numpy",
    "pyautogui",
    "rapidfuzz",
    "websockets",          # required for LAN + online chat
    "stem",                # required for Tor hidden service / P2P online chat
    "PySocks",             # Tor SOCKS5 proxy for outbound .onion connections
    "cryptography",        # Ed25519 identity keys for P2P DID
]

# Grammar correction — installed unless the user opts out.
GRAMMAR_PACKAGES = [
    "happytransformer<4.0.0",
    "transformers",
    "torch",
]

# Noise suppression — optional, improves transcription in noisy rooms.
NOISE_PACKAGES = [
    "noisereduce",
]

# Translation — optional, enables the 'translate' command.
TRANSLATION_PACKAGES = [
    "deep-translator",
]

# LLM (Ollama client + summarization pipeline) — optional.
LLM_PACKAGES = [
    "ollama",
    "sumy",
    "nltk",
    "scikit-learn",
]

# Better VAD — optional but strongly recommended on systems where it builds cleanly.
VAD_PACKAGES = [
    "webrtcvad",
]

PLATFORM_EXTRA = {
    "Windows": ["pywin32", "pywinauto"],
    "Darwin":  [],
    "Linux":   [],
}

def install_python_packages(gpu: bool = False, grammar: bool = True,
                             noise: bool = False, translation: bool = False,
                             llm: bool = False, vad: bool = True):
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

    # ── Core packages ──────────────────────────────────────────────────────────
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

    # ── Optional: translation ─────────────────────────────────────────────────
    if translation:
        log("Installing translation packages...")
        for pkg in TRANSLATION_PACKAGES:
            try:
                pip_run("install", "--upgrade", pkg)
                ok(pkg)
            except RuntimeError as e:
                warn(f"Could not install {pkg}: {e}")
    else:
        log("Translation skipped (enable later via Update window or --translation flag).")

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
def copy_source_files(script_dir: Path, install_dir: Path):
    """
    Copy the 'spoaken' sibling folder (next to install.py) into install_dir,
    so install_dir/spoaken/ contains the full application source.
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
    shutil.copytree(src, dest)
    ok(f"Copied spoaken/ → {dest}")


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
def write_runtime_config(cfg: dict, install_dir: Path):
    config_path = install_dir / "spoaken_config.json"
    install_dir.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump({
            # ── Transcription engines ──────────────────────────────────────────
            "whisper_model":          cfg.get("whisper_model", "base.en"),
            "whisper_enabled":        True,
            "vosk_model":             cfg.get("vosk_model", None),
            "vosk_enabled":           cfg.get("vosk_enabled", False),
            "enable_giga_model":      False,
            "vosk_model_accurate":    "vosk-model-en-us-0.42-gigaspeech",
            # ── Hardware ──────────────────────────────────────────────────────
            "gpu":                    cfg.get("gpu", False),
            "mic_device":             None,
            "noise_suppression":      cfg.get("noise", False),
            # ── Grammar ───────────────────────────────────────────────────────
            "grammar":                cfg.get("grammar", True),
            # ── Chat / networking ──────────────────────────────────────────────
            "chat_server_enabled":    cfg.get("chat_server_enabled", False),
            "chat_server_port":       cfg.get("chat_server_port", 55300),
            "chat_server_token":      cfg.get("chat_server_token", "spoaken"),
            "android_stream_enabled": cfg.get("android_stream_enabled", False),
            "android_stream_port":    cfg.get("android_stream_port", 55301),
            # ── Memory management ─────────────────────────────────────────────
            "memory_cap_words":       cfg.get("memory_cap_words", 300),
            "memory_cap_minutes":     cfg.get("memory_cap_minutes", 10),
            # ── Text quality ──────────────────────────────────────────────────
            "duplicate_filter":       True,
            # ── Paths ─────────────────────────────────────────────────────────
            "whisper_dir":            str(install_dir / "models" / "whisper"),
            "vosk_dir":               str(install_dir / "models" / "vosk"),
            "platform":               OS,
            "install_dir":            str(install_dir),
        }, f, indent=2)
    ok(f"Runtime config written to {config_path}")

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
            vc = int(input("\nSelect Vosk model [default: 0 = none]: ") or "0")
            if vc == 0:
                vosk_model, vosk_enabled = None, False
            else:
                vosk_model, vosk_enabled = vosk_options[vc], True
            break
        except (ValueError, IndexError):
            print("Invalid choice, try again.")

    gpu_raw        = input("\nEnable GPU / CUDA acceleration? [y/N]: ").strip().lower()
    grammar_raw    = input("Install grammar correction (HappyTransformer/T5)? [Y/n]: ").strip().lower()
    noise_raw      = input("Install noise suppression (noisereduce)? [y/N]: ").strip().lower()
    translation_raw = input("Install translation support (deep-translator)? [y/N]: ").strip().lower()
    llm_raw        = input("Install LLM + summarization packages (ollama, sumy, nltk)? [y/N]: ").strip().lower()
    vad_raw        = input("Install webrtcvad for better Voice Activity Detection? [Y/n]: ").strip().lower()

    print(f"\n{CYAN}── Chat / Networking ──────────────────────────────────────{NC}")
    print("  LAN chat lets other Spoaken users on your network connect to this machine.")
    chat_raw       = input("Enable LAN chat server at startup? [y/N]: ").strip().lower()
    chat_port      = 55300
    chat_token     = "spoaken"
    if chat_raw in ("y", "yes"):
        port_in = input("  Chat server port [55300]: ").strip()
        chat_port  = int(port_in) if port_in.isdigit() else 55300
        token_in   = input("  Shared auth token [spoaken]: ").strip()
        chat_token = token_in or "spoaken"

    android_raw  = input("Enable Android/browser live transcript stream? [y/N]: ").strip().lower()
    android_port = 55301
    if android_raw in ("y", "yes"):
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
        "gpu":                    gpu_raw in ("y", "yes"),
        "grammar":                grammar_raw not in ("n", "no"),
        "noise":                  noise_raw in ("y", "yes"),
        "translation":            translation_raw in ("y", "yes"),
        "llm":                    llm_raw in ("y", "yes"),
        "vad":                    vad_raw not in ("n", "no"),
        "chat_server_enabled":    chat_raw in ("y", "yes"),
        "chat_server_port":       chat_port,
        "chat_server_token":      chat_token,
        "android_stream_enabled": android_raw in ("y", "yes"),
        "android_stream_port":    android_port,
        "install_dir":            idir,
    }

# ─── Main installation orchestrator ───────────────────────────────────────────
def run_install(cfg: dict):
    install_dir     = Path(cfg.get("install_dir", os.path.expanduser("~/spoaken")))
    whisper_model   = cfg.get("whisper_model", "base.en")
    vosk_model      = cfg.get("vosk_model", None)
    vosk_enabled    = cfg.get("vosk_enabled", False)
    gpu             = cfg.get("gpu", False)
    grammar         = cfg.get("grammar", True)
    noise           = cfg.get("noise", False)
    translation     = cfg.get("translation", False)
    llm             = cfg.get("llm", False)
    vad             = cfg.get("vad", True)
    chat_enabled    = cfg.get("chat_server_enabled", False)
    android_enabled = cfg.get("android_stream_enabled", False)
    whisper_dir     = install_dir / "models" / "whisper"
    vosk_dir        = install_dir / "models" / "vosk"

    print(BANNER)
    log(f"Platform    : {OS} ({platform.version()})")
    log(f"Python      : {platform.python_version()} @ {python_exe()}")
    log(f"Install dir : {install_dir}")
    log(f"Whisper     : {whisper_model}")
    log(f"Vosk        : {vosk_model if vosk_enabled else 'disabled'}")
    log(f"GPU/CUDA    : {'yes' if gpu else 'no'}")
    log(f"Grammar     : {'yes' if grammar else 'no'}")
    log(f"Noise NR    : {'yes' if noise else 'no (install later via Update window)'}")
    log(f"Translation : {'yes' if translation else 'no (install later via Update window)'}")
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
    )

    # Step 4 — Copy source files into install_dir
    step(4, "Copying Project Files")
    script_dir = Path(__file__).parent.resolve()
    try:
        copy_source_files(script_dir, install_dir)
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
        write_runtime_config(cfg, install_dir)
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
            ("noisereduce", noise), ("deep-translator", translation),
            ("ollama + sumy + nltk", llm), ("webrtcvad", vad),
        ] if flag
    ]
    optional_skipped = [
        name for name, flag in [
            ("noisereduce", noise), ("deep-translator", translation),
            ("ollama + sumy + nltk", llm), ("webrtcvad", vad),
        ] if not flag
    ]

    print(f"""
{GREEN}╔══════════════════════════════════════════════════════╗
║   Installation complete!                             ║
╚══════════════════════════════════════════════════════╝{NC}

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
  python install.py --interactive              # full guided setup
  python install.py --config my_config.json   # re-run from saved config
  python install.py --noise --translation     # add optional packages to existing install
  python install.py --vosk-only               # re-download vosk model only
        """,
    )
    parser.add_argument("--config",       default="spoaken_config.json",
                        help="Path to JSON configuration file")
    parser.add_argument("--interactive",  action="store_true",
                        help="Run interactive CLI configuration")
    parser.add_argument("--vosk-only",    action="store_true",
                        help="Only install/re-download the Vosk model from config")
    # Optional feature flags — can be used without --interactive
    parser.add_argument("--noise",        action="store_true",
                        help="Install noisereduce for noise suppression")
    parser.add_argument("--translation",  action="store_true",
                        help="Install deep-translator for the translate command")
    parser.add_argument("--llm",          action="store_true",
                        help="Install ollama, sumy, nltk, scikit-learn for LLM + summarization")
    parser.add_argument("--no-vad",       action="store_true",
                        help="Skip webrtcvad installation (use energy-gate fallback instead)")
    parser.add_argument("--chat",         action="store_true",
                        help="Enable LAN chat server in the written config")
    args = parser.parse_args()

    # Determine config
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

    # CLI flags override config file values
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

    # Vosk-only mode
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
