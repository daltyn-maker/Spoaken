# Spoaken — Voice-to-Text Engine

Spoaken is a cross-platform voice-to-text tool powered by [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper) and optionally [Vosk](https://alphacephei.com/vosk/). It listens to your microphone, transcribes speech, and types the result directly into whatever window you have focused. It works as a voice to text service with text processing, including translation and summary. along with a chat service for local group projects, or (experimentally) online.


Supports **Windows 10/11**, **macOS 12+**, **Ubuntu/Debian**, **Fedora/RHEL**, and **Arch Linux**.

---

## Getting Started

### 1. Prerequisites

- **Python 3.9 or newer** — the bootstrap scripts will install this for you if it's missing
- An internet connection for the first run (to download models)
- A working microphone

### 2. Installation

**macOS / Linux:** in terminal
```bash
cd ~/Path/To/Your/File
chmod +x install.sh
./install.sh
```

**Windows** — right-click `windows_install.bat` and select **Run as administrator**.

The installer will:
- Install system dependencies (FFmpeg, PortAudio, etc.)
- Install all required Python packages
- Download your chosen Whisper speech model
- Copy the Spoaken app into your install directory
- Create a desktop shortcut

You'll be walked through a short setup (model size, install location, GPU support) the first time. Your choices are saved to `spoaken_config.json` so future installs skip the prompts.

Files can be updated and repaired within the app.

### 3. Launching Spoaken

**After installation, use the desktop shortcut** that was created automatically.

Or launch manually:
```bash
python3 ~/spoaken/spoaken/spoaken_main.py
```

On Windows:
```bat
python "%USERPROFILE%\spoaken\spoaken\spoaken_main.py"
```

---

## Configuration

Your settings are stored in `~/spoaken/spoaken_config.json` (or the install directory you chose). You can edit this file directly or re-run the installer interactively:

```bash
python install.py --interactive
```

| Key | Description | Default |
|---|---|---|
| `whisper_model` | Whisper model size (`tiny.en`, `base.en`, `small`, `medium`, `large-v3`, ...) | `base.en` |
| `vosk_enabled` | Enable Vosk for real-time partial transcription display | `false` |
| `vosk_model` | Which Vosk model to use | `null` |
| `gpu` | Use CUDA GPU acceleration (requires compatible NVIDIA GPU) | `false` |
| `grammar` | Enable grammar correction via HappyTransformer | `true` |
| `install_dir` | Where Spoaken is installed | `~/spoaken` |

**Model size guide:**

| Model | Size | Speed | Accuracy |
|---|---|---|---|
| `tiny.en` | ~75 MB | Fastest | Basic |
| `base.en` | ~145 MB | Fast | Good — recommended starting point |
| `small.en` | ~465 MB | Moderate | Better |
| `medium.en` | ~1.5 GB | Slow | High |
| `large-v3` | ~3 GB | Slowest | Best |

---

## macOS: Accessibility Permission

Spoaken types into other apps using macOS Accessibility APIs. Before first use you must grant permission:

**System Settings → Privacy & Security → Accessibility → add your Terminal (or the Spoaken launcher) and enable the toggle.**

Without this, transcribed text won't be typed into other windows.

---

## Troubleshooting

**"Permission denied" during install**
The installer tried to write to a system directory. Re-run `install.sh` without `sudo` — it defaults to `~/spoaken`.

**No audio / microphone not detected**
Make sure your mic is set as the system default input device. On Linux, also check that `portaudio` installed correctly (`python3 -m sounddevice` should list your devices).

**Transcription is slow**
Try a smaller model (`tiny.en` or `base.en`). If you have an NVIDIA GPU, re-run the installer and enable GPU/CUDA.

**Model download fails**
The first launch downloads the Whisper model from HuggingFace. If it fails, check your internet connection and try again — the installer will resume where it left off.

---

## Project File Overview

```
Spoaken-main/              ← project root (clone / download lands here)
├── install.py             ← cross-platform installer backend
├── bootstrap.sh           ← Unix entry point (macOS + Linux)
├── bootstrap.bat          ← Windows entry point
├── README.md
└── spoaken/               ← application source folder
    ├── all_spoaken_files.py
    └── Art/
        ├── logo.png       ← app icon (Linux / macOS)
        └── logo.ico       ← app icon (Windows)

~/spoaken/                 ← install directory (created by installer)
├── spoaken_config.json    ← your saved settings
├── models/
│   ├── whisper/           ← downloaded Whisper model files
│   └── vosk/              ← downloaded Vosk model files (if enabled)
├── happy/                 ← T5 grammar model cache
├── Logs/                  ← vosk_log.txt, whisper_log.txt, final_session_log.txt
└── spoaken/               ← copied here from Spoaken-main/spoaken/ by installer
    ├── spoaken_main.py
    └── ...
```

---

## Spoaken Source Files

All files live inside the `spoaken/` folder within your install directory.

| File | What it does |
|---|---|
| `spoaken_main.py` | Entry point. Launches the splash screen immediately in the main thread, then loads all models in a background thread so the UI stays responsive. Once loading is done it hands off to the main window. |
| `spoaken_splash.py` | The startup splash screen shown while models load. Also runs a Python version gate (3.9+ required) and checks for missing packages, showing install hints in the splash if anything is absent. |
| `spoaken_gui.py` | The entire main window UI — built with CustomTkinter. Handles the animated waveform display, microphone selector, model swap dropdowns, transcript display with colour-coded output (teal = Vosk, cyan = Whisper), the collapsible Chat sidebar, and all buttons. Strictly thread-safe: all UI updates go through `self.after()`. |
| `spoaken_control.py` | Controller layer — the brain of the app. Manages recording state, spins up audio capture and transcription threads, runs the duplicate-text filter, enforces memory caps, handles the `spoaken.translate()` command parser, drives page-writing through the writer, and hosts the optional LAN chat server and Android SSE stream server. |
| `spoaken_connect.py` | Data/model layer. Loads and owns the Vosk, Whisper, and HappyTransformer (T5 grammar) models. Provides the audio queues each transcription thread reads from, the `transcribe_whisper()` method, the `run_polish()` grammar correction pass, and utilities for listing installed models and translating text. |
| `spoaken_writer.py` | Platform-agnostic window writer. Given a target app name it uses fuzzy matching to find the right window, then types transcribed text directly into it — using `xdotool` on Linux, `pywinauto` on Windows, and `osascript` on macOS. Falls back to `pyautogui` (click-to-focus) if the native backend is unavailable. |
| `spoaken_config.py` | Config loader. Searches three locations for `spoaken_config.json` (install root → package folder → `~/.spoaken/`), merges the file with built-in defaults, and exposes every setting as a named constant (e.g. `VOSK_ENABLED`, `WHISPER_MODEL`, `MEMORY_CAP_WORDS`). |
| `paths.py` | Central directory resolver. Reads the installer-generated config to find where models were cached, then exposes `WHISPER_DIR`, `VOSK_DIR`, `HAPPY_DIR`, `ART_DIR`, and `LOG_DIR` as `Path` objects. Auto-creates any missing folders on import. |
| and so much more |

---

## Uninstalling

dependencies will need to be uninstalled separately, check installation for downloaded dependencies
Simply delete the install directory (default `~/spoaken`) and the desktop shortcut. No system files are modified outside that folder.














