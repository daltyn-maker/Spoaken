#!/usr/bin/env python3
"""
spoaken_uninstall.py
────────────────────
Complete uninstaller for Spoaken.

What it removes
───────────────
  1. All pip packages installed by Spoaken
  2. Downloaded model caches
       • Vosk models          (<install>/models/vosk/)
       • Whisper models       (<install>/models/whisper/)
       • T5 / HappyTransformer cache  (<install>/happy/)
  3. Spoaken security / PKI data       (~/.spoaken/)
  4. Log files                          (<install>/Logs/)
  5. The Spoaken source directory itself

Usage
─────
  python spoaken_uninstall.py           — interactive, confirms before each step
  python spoaken_uninstall.py --yes     — skip all confirmations (CI / scripted)
  python spoaken_uninstall.py --dry-run — show what WOULD be removed, touch nothing
  python spoaken_uninstall.py --keep-models  — preserve downloaded model files
  python spoaken_uninstall.py --keep-config  — preserve spoaken_config.json
─────
This does not uninstall system dependencies:
Ollama and installed models, and FFmpeg
These must be uninstalled separately from this file
	
"""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import platform
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

# ── Colour helpers ─────────────────────────────────────────────────────────────
_USE_COLOR = sys.stdout.isatty() and platform.system() != "Windows"

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text

def red(t):    return _c("31", t)
def green(t):  return _c("32", t)
def yellow(t): return _c("33", t)
def cyan(t):   return _c("36", t)
def bold(t):   return _c("1",  t)
def dim(t):    return _c("2",  t)


# ── Complete pip package list (mirrors spoaken_update._PACKAGES) ───────────────
_PIP_PACKAGES = [
    # Core UI
    "customtkinter",
    "Pillow",
    # Audio
    "sounddevice",
    "numpy",
    # Transcription
    "faster-whisper",
    "vosk",
    # Grammar
    "happytransformer",
    "transformers",
    "torch",
    "sentencepiece",
    "accelerate",
    # Text automation
    "pyautogui",
    "rapidfuzz",
    # Optional quality
    "noisereduce",
    "deep-translator",
    # LLM / Ollama client
    "ollama",
    # Summarization
    "sumy",
    "nltk",
    "scikit-learn",
    # Windows extras
    "pywin32",
    "pywinauto",
    # Signal processing (used by EQ filter)
    "scipy",
    # Crypto (used by secure LAN chat)
    "cryptography",
    # Tor (optional anonymity)
    "stem",
    "tor",
]

# ── Source file names (relative to the spoaken package directory) ──────────────
_SOURCE_FILES = [
    "spoaken_main.py",
    "spoaken_config.py",
    "spoaken_connect.py",
    "spoaken_control.py",
    "spoaken_gui.py",
    "spoaken_chat.py",
    "spoaken_chat_lan.py",
    "spoaken_chat_lan_secure.py",
    "spoaken_chat_online.py",
    "spoaken_commands.py",
    "spoaken_crypto.py",
    "spoaken_llm.py",
    "spoaken_mic_config.py",
    "spoaken_splash.py",
    "spoaken_summarize.py",
    "spoaken_sysenviron.py",
    "spoaken_update.py",
    "spoaken_uninstall.py",
    "spoaken_vad.py",
    "spoaken_writer.py",
    "paths.py",
]


# ═══════════════════════════════════════════════════════════════════════════════
# Path resolution
# ═══════════════════════════════════════════════════════════════════════════════

def _resolve_paths() -> dict:
    """
    Attempt to import paths.py from the same directory as this script.
    Falls back to sensible guesses when paths.py is unavailable.
    """
    script_dir = Path(__file__).resolve().parent

    # Try importing paths to get installer-configured directories
    try:
        sys.path.insert(0, str(script_dir))
        import paths as _paths
        root_dir    = Path(_paths.ROOT_DIR)
        vosk_dir    = Path(_paths.VOSK_DIR)
        whisper_dir = Path(_paths.WHISPER_DIR)
        happy_dir   = Path(_paths.HAPPY_DIR)
        log_dir     = Path(_paths.LOG_DIR)
        art_dir     = Path(_paths.ART_DIR)
    except Exception:
        # Fallback layout: assume this file is inside <install_dir>/spoaken/
        root_dir    = script_dir.parent
        vosk_dir    = root_dir / "models" / "vosk"
        whisper_dir = root_dir / "models" / "whisper"
        happy_dir   = root_dir / "happy"
        log_dir     = root_dir / "Logs"
        art_dir     = script_dir / "Art"

    pki_dir    = Path.home() / ".spoaken" / "pki"
    spoaken_user_dir = Path.home() / ".spoaken"

    return {
        "root_dir"        : root_dir,
        "spoaken_dir"     : script_dir,
        "vosk_dir"        : vosk_dir,
        "whisper_dir"     : whisper_dir,
        "happy_dir"       : happy_dir,
        "log_dir"         : log_dir,
        "art_dir"         : art_dir,
        "pki_dir"         : pki_dir,
        "spoaken_user_dir": spoaken_user_dir,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# pip helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _is_installed(pkg_name: str) -> bool:
    """Check if a pip package is currently installed."""
    bare = pkg_name.split("<")[0].split(">")[0].split("=")[0].strip()
    try:
        importlib.metadata.version(bare)
        return True
    except importlib.metadata.PackageNotFoundError:
        return False


def _pip_uninstall(pkg: str, dry_run: bool = False) -> bool:
    """
    Uninstall a single pip package.
    Returns True if uninstalled (or would be in dry-run), False if not installed / failed.
    """
    if not _is_installed(pkg):
        return False

    if dry_run:
        print(f"  {dim('[dry-run]')}  would uninstall  {cyan(pkg)}")
        return True

    cmd = [sys.executable, "-m", "pip", "uninstall", "-y", pkg]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"  {green('✔')}  uninstalled  {cyan(pkg)}")
            return True
        else:
            err = (result.stderr or result.stdout).strip().splitlines()[-1]
            print(f"  {yellow('!')}  {pkg}  →  {err}")
            return False
    except Exception as exc:
        print(f"  {red('✗')}  {pkg}  →  {exc}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Filesystem helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _human_size(path: Path) -> str:
    """Return a human-readable size string for a file or directory."""
    try:
        if path.is_file():
            total = path.stat().st_size
        else:
            total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        for unit in ("B", "KB", "MB", "GB"):
            if total < 1024:
                return f"{total:.0f} {unit}"
            total /= 1024
        return f"{total:.1f} TB"
    except Exception:
        return "?"


def _remove_dir(path: Path, label: str, dry_run: bool = False) -> bool:
    """Remove a directory tree.  Returns True if anything was (or would be) removed."""
    if not path.exists():
        print(f"  {dim('–')}  {label}  {dim('(not found — skipping)')}")
        return False
    size = _human_size(path)
    if dry_run:
        print(f"  {dim('[dry-run]')}  would remove  {cyan(str(path))}  {dim(f'({size})')}")
        return True
    try:
        shutil.rmtree(path)
        print(f"  {green('✔')}  removed  {cyan(str(path))}  {dim(f'({size})')}")
        return True
    except Exception as exc:
        print(f"  {red('✗')}  could not remove  {path}  →  {exc}")
        return False


def _remove_file(path: Path, dry_run: bool = False) -> bool:
    """Remove a single file.  Returns True if anything was (or would be) removed."""
    if not path.exists():
        return False
    if dry_run:
        print(f"  {dim('[dry-run]')}  would remove  {cyan(str(path))}")
        return True
    try:
        path.unlink()
        print(f"  {green('✔')}  removed  {cyan(str(path))}")
        return True
    except Exception as exc:
        print(f"  {red('✗')}  {path}  →  {exc}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Prompt helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _ask(prompt: str, yes_all: bool) -> bool:
    """Return True if the user confirms (or yes_all is set)."""
    if yes_all:
        print(f"  {dim('[--yes]')}  {prompt}")
        return True
    try:
        answer = input(f"\n  {bold(prompt)}  {dim('[y/N]')}: ").strip().lower()
        return answer in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Uninstall steps
# ═══════════════════════════════════════════════════════════════════════════════

def step_pip_packages(dry_run: bool, yes_all: bool) -> int:
    """Uninstall all Spoaken pip packages. Returns count removed."""
    print(bold("\n── Step 1: pip packages ─────────────────────────────────────────"))

    installed = [p for p in _PIP_PACKAGES if _is_installed(p)]
    if not installed:
        print(f"  {dim('No Spoaken pip packages found — nothing to remove.')}")
        return 0

    print(f"  Found {len(installed)} installed package(s):")
    for p in installed:
        print(f"    {dim('•')} {cyan(p)}")

    if not _ask(f"Uninstall these {len(installed)} packages?", yes_all):
        print(f"  {yellow('Skipped.')}")
        return 0

    removed = 0
    for pkg in installed:
        if _pip_uninstall(pkg, dry_run):
            removed += 1

    print(f"\n  {green(str(removed))}/{len(installed)} packages removed.")
    return removed


def step_models(paths: dict, dry_run: bool, yes_all: bool) -> None:
    """Remove downloaded model files (Vosk, Whisper, T5/HappyTransformer)."""
    print(bold("\n── Step 2: downloaded models ────────────────────────────────────"))

    model_dirs = [
        (paths["vosk_dir"],    "Vosk models"),
        (paths["whisper_dir"], "Whisper models"),
        (paths["happy_dir"],   "T5 / HappyTransformer cache"),
    ]

    any_found = any(p.exists() for p, _ in model_dirs)
    if not any_found:
        print(f"  {dim('No model directories found — nothing to remove.')}")
        return

    total_size = sum(
        int(_human_size(p).split()[0])
        for p, _ in model_dirs if p.exists()
    )

    print("  Found model directories:")
    for p, label in model_dirs:
        if p.exists():
            print(f"    {dim('•')} {label:<36} {cyan(str(p))}  {dim(f'({_human_size(p)})')}")

    if not _ask("Remove all downloaded model files?", yes_all):
        print(f"  {yellow('Skipped.')}")
        return

    for p, label in model_dirs:
        _remove_dir(p, label, dry_run)


def step_pki(paths: dict, dry_run: bool, yes_all: bool) -> None:
    """Remove ~/.spoaken PKI / security data."""
    print(bold("\n── Step 3: security / PKI data ──────────────────────────────────"))

    user_dir = paths["spoaken_user_dir"]
    if not user_dir.exists():
        print(f"  {dim('~/.spoaken not found — nothing to remove.')}")
        return

    size = _human_size(user_dir)
    print(f"  {cyan(str(user_dir))}  {dim(f'({size})')}")
    print(f"  {dim('Contains: CA certs, HMAC secrets, beacon keys, token.secret')}")

    if not _ask("Remove ~/.spoaken (PKI keys and secrets)?", yes_all):
        print(f"  {yellow('Skipped.')}")
        return

    _remove_dir(user_dir, "~/.spoaken", dry_run)


def step_logs(paths: dict, dry_run: bool, yes_all: bool) -> None:
    """Remove Spoaken log files."""
    print(bold("\n── Step 4: log files ────────────────────────────────────────────"))

    log_dir = paths["log_dir"]
    if not log_dir.exists():
        print(f"  {dim('Log directory not found — nothing to remove.')}")
        return

    size = _human_size(log_dir)
    print(f"  {cyan(str(log_dir))}  {dim(f'({size})')}")

    if not _ask("Remove log files?", yes_all):
        print(f"  {yellow('Skipped.')}")
        return

    _remove_dir(log_dir, "Logs", dry_run)


def step_source(paths: dict, dry_run: bool, yes_all: bool, keep_config: bool) -> None:
    """Remove Spoaken source files and the install directory."""
    print(bold("\n── Step 5: Spoaken source files ─────────────────────────────────"))

    spoaken_dir = paths["spoaken_dir"]
    root_dir    = paths["root_dir"]
    cfg_file    = root_dir / "spoaken_config.json"

    print(f"  Source directory : {cyan(str(spoaken_dir))}")
    print(f"  Install root     : {cyan(str(root_dir))}")

    if keep_config and cfg_file.exists():
        print(f"  {yellow('--keep-config')} : {cyan('spoaken_config.json')} will be preserved")

    # List source files that exist
    present = [spoaken_dir / f for f in _SOURCE_FILES if (spoaken_dir / f).exists()]
    if not present:
        print(f"  {dim('No source files found in expected location.')}")
    else:
        print(f"  {len(present)} source file(s) found.")

    if not _ask("Remove Spoaken source files and install directory?", yes_all):
        print(f"  {yellow('Skipped.')}")
        return

    # Remove individual source files first
    for f in present:
        if keep_config and f.name == "spoaken_config.json":
            continue
        _remove_file(f, dry_run)

    # Remove Art/ directory
    art_dir = paths["art_dir"]
    if art_dir.exists():
        _remove_dir(art_dir, "Art/", dry_run)

    # Remove the spoaken/ package directory itself if now empty
    if not dry_run:
        try:
            remaining = list(spoaken_dir.iterdir())
            if not remaining:
                spoaken_dir.rmdir()
                print(f"  {green('✔')}  removed empty dir  {cyan(str(spoaken_dir))}")
            else:
                print(f"  {yellow('!')}  {spoaken_dir.name}/ not empty — leaving in place")
                print(f"       remaining: {', '.join(p.name for p in remaining[:6])}")
        except Exception as exc:
            print(f"  {yellow('!')}  could not remove dir: {exc}")
    else:
        print(f"  {dim('[dry-run]')}  would remove empty dir  {cyan(str(spoaken_dir))}")

    # Remove root-level config (unless --keep-config)
    if cfg_file.exists() and not keep_config:
        _remove_file(cfg_file, dry_run)

    # Remove models/ parent if now empty
    models_dir = root_dir / "models"
    if not dry_run and models_dir.exists():
        try:
            remaining = list(models_dir.iterdir())
            if not remaining:
                models_dir.rmdir()
                print(f"  {green('✔')}  removed empty dir  {cyan(str(models_dir))}")
        except Exception:
            pass

    # Offer to remove the entire root_dir if empty
    if not dry_run:
        try:
            remaining = list(root_dir.iterdir())
            if not remaining:
                if _ask(f"Install root {root_dir} is now empty — remove it?", yes_all):
                    root_dir.rmdir()
                    print(f"  {green('✔')}  removed  {cyan(str(root_dir))}")
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        prog="spoaken_uninstall",
        description="Uninstall Spoaken and all its dependencies.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              python spoaken_uninstall.py              # interactive
              python spoaken_uninstall.py --yes        # no prompts
              python spoaken_uninstall.py --dry-run    # preview only
              python spoaken_uninstall.py --keep-models --keep-config
        """),
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip all confirmation prompts (non-interactive / CI mode)",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would be removed without actually deleting anything",
    )
    parser.add_argument(
        "--keep-models",
        action="store_true",
        help="Preserve downloaded Vosk, Whisper, and T5 model files",
    )
    parser.add_argument(
        "--keep-config",
        action="store_true",
        help="Preserve spoaken_config.json after source removal",
    )
    parser.add_argument(
        "--skip-packages",
        action="store_true",
        help="Skip pip package uninstallation (remove files only)",
    )

    args = parser.parse_args()

    # ── Banner ─────────────────────────────────────────────────────────────────
    print()
    print(bold(cyan("  ╔══════════════════════════════════════╗")))
    print(bold(cyan("  ║     SPOAKEN  —  Uninstaller          ║")))
    print(bold(cyan("  ╚══════════════════════════════════════╝")))
    print()

    if args.dry_run:
        print(f"  {yellow(bold('DRY RUN MODE'))} — no files or packages will be touched.")

    print(f"  Python     : {sys.executable}")
    print(f"  Platform   : {platform.system()} {platform.release()}")
    print(f"  Script dir : {Path(__file__).resolve().parent}")

    # ── Resolve paths ──────────────────────────────────────────────────────────
    paths = _resolve_paths()
    print(f"  Install root : {cyan(str(paths['root_dir']))}")
    print(f"  PKI / config : {cyan(str(paths['spoaken_user_dir']))}")

    # ── Final confirmation before anything destructive ─────────────────────────
    if not args.dry_run:
        print()
        print(f"  {red(bold('WARNING'))}: This will permanently remove Spoaken and selected data.")
        print(f"  {dim('Downloaded models can be large (several GB) and cannot be undone.')}")
        if not _ask("Proceed with uninstallation?", args.yes):
            print(f"\n  {yellow('Uninstall cancelled.')}\n")
            sys.exit(0)

    # ── Run steps ──────────────────────────────────────────────────────────────
    if not args.skip_packages:
        step_pip_packages(args.dry_run, args.yes)

    if not args.keep_models:
        step_models(paths, args.dry_run, args.yes)
    else:
        print(bold("\n── Step 2: downloaded models ────────────────────────────────────"))
        print(f"  {yellow('--keep-models')} set — skipping model removal.")

    step_pki(paths,    args.dry_run, args.yes)
    step_logs(paths,   args.dry_run, args.yes)
    step_source(paths, args.dry_run, args.yes, args.keep_config)

    # ── Summary ────────────────────────────────────────────────────────────────
    print()
    print(bold(cyan("  ─────────────────────────────────────────")))
    if args.dry_run:
        print(f"  {bold(yellow('Dry run complete.'))}  Re-run without --dry-run to apply.")
    else:
        print(f"  {bold(green('Uninstall complete.'))}  Spoaken has been removed.")
        print()
        print(f"  {dim('Note: Ollama itself (the desktop daemon) is NOT uninstalled here.')}")
        print(f"  {dim('  To remove Ollama visit: https://ollama.com')}")
        print(f"  {dim('Note: System packages (portaudio, ffmpeg, etc.) are NOT removed.')}")
    print(bold(cyan("  ─────────────────────────────────────────")))
    print()


if __name__ == "__main__":
    main()
