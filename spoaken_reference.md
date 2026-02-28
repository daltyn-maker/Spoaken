# Spoaken v2.0 — Complete Reference

> Voice-to-text engine with LAN/P2P chat, LLM processing, and direct window writing.  
> All offline features work with no internet. Online features are clearly marked.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Folder Layout](#2-folder-layout)
3. [File-by-File Reference](#3-file-by-file-reference)
   - [paths.py](#pathspy)
   - [spoaken_config.py](#spoaken_configpy)
   - [spoaken_main.py](#spoaken_mainpy)
   - [spoaken_splash.py](#spoaken_splashpy)
   - [spoaken_gui.py](#spoaken_guipy)
   - [spoaken_control.py](#spoaken_controlpy)
   - [spoaken_connect.py](#spoaken_connectpy)
   - [spoaken_vad.py](#spoaken_vadpy)
   - [spoaken_writer.py](#spoaken_writerpy)
   - [spoaken_commands.py](#spoaken_commandspy)
   - [spoaken_llm.py](#spoaken_llmpy)
   - [spoaken_summarize.py](#spoaken_summarizepy)
   - [spoaken_chat.py](#spoaken_chatpy)
   - [spoaken_chat_lan.py](#spoaken_chat_lanpy)
   - [spoaken_chat_lan_secure.py](#spoaken_chat_lan_securepy)
   - [spoaken_chat_online.py](#spoaken_chat_onlinepy)
   - [spoaken_crypto.py](#spoaken_cryptopy)
   - [spoaken_sysenviron.py](#spoaken_sysenvironpy)
   - [spoaken_mic_config.py](#spoaken_mic_configpy)
   - [spoaken_update.py](#spoaken_updatepy)
4. [Threading Model](#4-threading-model)
5. [Audio Pipeline](#5-audio-pipeline)
6. [What Can Be Deleted](#6-what-can-be-deleted)
7. [Online vs Offline Feature Matrix](#7-online-vs-offline-feature-matrix)
8. [Required Packages](#8-required-packages)
9. [Configuration Reference](#9-configuration-reference)
10. [Command Reference](#10-command-reference)

---

## 1. Architecture Overview

Spoaken is structured as a classic **Model–View–Controller** application with a layered set of optional subsystems.

```
spoaken_main.py          ← entry point, threading root
│
├── SpoakenSplash        ← splash screen (CTk mainloop on main thread)
│
└── [init thread]
    ├── spoaken_config   ← loads spoaken_config.json
    ├── TranscriptionModel  (spoaken_connect)   ← loads Vosk + Whisper
    ├── TranscriptionController (spoaken_control) ← glue layer
    └── TranscriptionView  (spoaken_gui)         ← main window
         │
         ├── DirectWindowWriter  (spoaken_writer)
         ├── CommandParser       (spoaken_commands)
         ├── SysEnviron          (spoaken_sysenviron)
         ├── ChatServer / SSEServer (spoaken_chat)
         ├── SpoakenLANClient    (spoaken_chat_lan)
         └── SpoakenP2PNode      (spoaken_chat_online)
```

**Main thread** runs the tkinter/CTk event loop exclusively — all GUI calls must go through `view.after()`.  
**Init thread** does all heavy imports and model loading while the splash is visible.  
**Audio, LLM, and chat** each run on their own daemon threads.

---

## 2. Folder Layout

```
<install_dir>/
  spoaken_config.json       ← user settings (created by installer)
  models/
    whisper/                ← faster-whisper model cache
    vosk/                   ← vosk model folders (one folder per model)
  happy/                    ← T5 grammar model cache (HuggingFace hub layout)
  Logs/
    vosk_log.txt            ← Vosk session transcriptions (rotating, 5 MB max)
    whisper_log.txt         ← Whisper session transcriptions (rotating, 5 MB max)
    final_session_log.txt   ← polish / summary output (rotating, 5 MB max)
    llm_summary.txt         ← Ollama summary chunks
    llm_translation.txt     ← Ollama translation chunks
    lan_transfers/          ← received LAN file transfers (content-addressed)
    received_files/         ← received P2P file transfers
    chat.db                 ← SQLite chat history (rooms, events, files, bans)
  spoaken/                  ← all .py files live here
    Art/
      logo.ico              ← window/taskbar icon (Windows)
      logo.png              ← window/taskbar icon (macOS / Linux)
      plane.gif             ← animated GIF for the system-load alert popup
      splash.gif            - animated GIF for the splash screen
```

---

## 3. File-by-File Reference

---

### paths.py

**Purpose:** Central directory resolver. Every other module imports paths from here instead of hardcoding them.  
**Online:** ❌ Fully offline.  
**Deletable:** ❌ Required — almost every other module imports from it.

#### What it does
Reads `spoaken_config.json` to find where model caches and logs live, then exposes clean `Path` objects. Auto-creates all required folders on import.

#### Exports

| Name | Type | Points to |
|---|---|---|
| `SPOAKEN_DIR` | `Path` | The `spoaken/` package directory (where the `.py` files live) |
| `ROOT_DIR` | `Path` | The parent of `spoaken/` — the install root |
| `WHISPER_DIR` | `Path` | `models/whisper/` — faster-whisper model cache |
| `VOSK_DIR` | `Path` | `models/vosk/` — Vosk model folders |
| `HAPPY_DIR` | `Path` | `happy/` — T5 grammar model cache |
| `ART_DIR` | `Path` | `spoaken/Art/` — icons and images |
| `LOG_DIR` | `Path` | `Logs/` — session logs and chat DB |

#### Important notes
- If `spoaken_config.json` cannot be parsed, all paths fall back to sensible defaults relative to `ROOT_DIR`.
- There is a harmless variable-name typo in the error handler (`_cp` / `_e` instead of `config_path` / `parse_error`). It doesn't affect runtime — the error is swallowed silently.

---

### spoaken_config.py

**Purpose:** Single source of truth for all user-configurable settings. Reads `spoaken_config.json` at import time and exposes every value as a module-level constant.  
**Online:** ⚠️ Partially — `is_online()` makes a TCP probe to `1.1.1.1:443`. All constants themselves are offline.  
**Deletable:** ❌ Required — nearly every module imports from it.

#### Key settings sections

**Audio / Transcription**

| Setting | Default | Description |
|---|---|---|
| `VOSK_ENABLED` | `True` | Whether Vosk is loaded at startup |
| `VOSK_MODEL` | `"vosk-model-small-en-us-0.15"` | Folder name inside `VOSK_DIR` |
| `WHISPER_ENABLED` | `True` | Whether faster-whisper is loaded |
| `WHISPER_MODEL` | `"small"` | Model size string passed to faster-whisper |
| `WHISPER_COMPUTE` | `"auto"` | `"auto"` / `"int8"` / `"float16"` etc. |
| `GPU_ENABLED` | `False` | Route Whisper to CUDA |
| `GRAMMAR_ENABLED` | `True` | Allow T5 grammar correction |
| `NOISE_SUPPRESSION` | `True` | Enable noisereduce in the audio pipeline |
| `MEMORY_CAP_WORDS` | `2000` | Wipe oldest sentences when transcript exceeds this |
| `OFFLINE_MODE` | `False` | Force all network calls off globally |
| `HAPPY_ONLINE_ONLY` | `True` | Prevent T5 from downloading weights at runtime |

**Chat / Network**

| Setting | Default | Description |
|---|---|---|
| `CHAT_SERVER_PORT` | `55300` | WebSocket server port |
| `CHAT_SERVER_TOKEN` | `"spoaken"` | Shared auth token |
| `ANDROID_STREAM_PORT` | `55301` | SSE HTTP stream port |
| `BIND_ADDRESS` | `""` | LAN IP to bind to (blank = all interfaces) |

**Security / PKI**

| Setting | Default | Description |
|---|---|---|
| `USE_TLS` | `True` | Enable WSS (recommended; set False only to debug) |
| `MTLS_ENABLED` | `True` | Require client certificates |
| `PKI_DIR` | `~/.spoaken/pki` | Where SpoakenCA stores all key material |
| `BEACON_SIGN` | `True` | Sign UDP discovery packets with Ed25519 |
| `MSG_ENVELOPE` | `False` | Wrap messages in AES-256-GCM (opt-in) |

#### Key functions

| Function | Online | Description |
|---|---|---|
| `is_online(timeout)` | ✅ Yes | TCP probe to `1.1.1.1:443` then `8.8.8.8:53`. Returns `False` immediately if `OFFLINE_MODE` is True |
| `get_token_secret()` | ❌ No | Returns the 32-byte HMAC secret, generating and saving it to `PKI_DIR/token.secret` on first call |
| `get_server_ssl_context()` | ❌ No | Returns an `ssl.SSLContext` for the WS server with mTLS enforced |
| `get_client_ssl_context(with_client_cert)` | ❌ No | Returns an `ssl.SSLContext` for a WS client |
| `get_hmac_token_minter()` | ❌ No | Returns an `HMACToken` instance for minting/verifying auth tokens |
| `get_beacon_signer()` | ❌ No | Returns an `Ed25519Beacon` for signing UDP discovery packets |
| `get_beacon_verifier()` | ❌ No | Returns an `Ed25519Beacon` for verifying UDP discovery packets |

---

### spoaken_main.py

**Purpose:** Application entry point. Owns the threading model and startup sequence.  
**Online:** ❌ Fully offline (the modules it loads may be online-optional).  
**Deletable:** ❌ Required — it is the entry point.

#### `main()`
1. Creates `SpoakenSplash` — splash screen appears immediately on the main thread.
2. Starts `init_thread` — a daemon thread that does all heavy work in the background.
3. Calls `splash.mainloop()` — blocks the main thread while the splash runs.
4. After the splash closes, builds `TranscriptionView` and calls its `mainloop()`.

#### `init_background()` (runs on init thread)
Imports and instantiates in this order, updating the splash progress bar at each step:
1. Loads config constants (`spoaken_config`)
2. Imports `TranscriptionModel` (`spoaken_connect`)
3. Imports `TranscriptionController` (`spoaken_control`)
4. Imports `TranscriptionView` (`spoaken_gui`)
5. Instantiates `TranscriptionController()`
6. Instantiates `TranscriptionModel(vosk_model=…)` — this is where Vosk and Whisper are actually loaded
7. Sets progress to 100% and calls `splash._finish()` after 600 ms

#### Important notes
- If `init_background()` raises any exception, it is stored in `init_errors` and re-raised on the main thread after the splash closes, giving a clean traceback.
- The init thread is attached to the splash as `splash._init_thread` so it can be inspected (but not killed — it is a daemon thread).

---

### spoaken_splash.py

**Purpose:** Themed splash screen shown during model loading. Provides a progress bar, animated GIF support, and emergency controls.  
**Online:** ❌ Fully offline.  
**Deletable:** ❌ Required — `spoaken_main.py` imports it directly.

#### `SpoakenSplash(ctk.CTk)`

**Constructor behaviour:**
1. Runs a background thread to check for missing packages (2 s timeout).
2. Tries to load `Art/plane.gif` as an animated GIF; falls back to `Art/logo.png` / `Art/logo.ico`; falls back to a plain spacer.
3. Builds the splash UI with progress bar, status label, and three window control buttons.

#### Key methods

| Method | Description |
|---|---|
| `set_progress(value, text)` | Updates the progress bar (0.0–1.0) and status label. Thread-safe — called via `splash.after(0, …)` from the init thread |
| `_animate_gif()` | Scheduled via `after()` — advances the `plane.gif` animation frame by frame at its own per-frame delay |
| `_finish()` | Cancels all pending `after()` callbacks and destroys the window |
| `_minimize()` | Hides the splash to the taskbar (re-enables `overrideredirect` first so iconify works) |
| `_dismiss()` | Calls `withdraw()` to hide the splash. The init thread keeps running — the main window will still appear |
| `_force_quit()` | Opens a confirmation dialog, then calls `sys.exit(0)` if confirmed |
| `_drag_start(event)` / `_drag_motion(event)` | Enables dragging the frameless splash window |

#### Important notes
- Python version gate runs at **module import** time — importing this module on Python < 3.9 immediately prints an error and calls `sys.exit(1)`.
- Missing packages are checked via `importlib.util.find_spec()` — not importing them — so this is safe and fast.
- The animated GIF label is a plain `tk.Label` (not `CTkLabel`) because CTk does not support frame-swapping for animation.
- A 30-second safety timeout calls `_finish()` automatically if loading somehow hangs forever.

---

### spoaken_gui.py

**Purpose:** The entire main application window. Builds and manages all UI panels, wires button events to the controller, and handles all incoming events from the chat backends.  
**Online:** ⚠️ Partially — the GUI itself is offline; the chat sidebar can connect to LAN/P2P backends which may be online.  
**Deletable:** ❌ Required.

#### `TranscriptionView(ctk.CTk)`

The window is structured as three areas:
- **Left pane** — Scrollable transcript display (inside a `PanedWindow`)
- **Right pane** — Controls (engines, mic, writing, LLM, model swap)
- **Sidebar** — Collapsible chat panel with LAN/P2P tabs and command bar

#### Transcript panel methods

| Method | Description |
|---|---|
| `insert_pending_segment(text, seg_id, tag)` | Inserts a new transcript segment with a tag for later replacement |
| `thread_safety_insert_pending(…)` | Thread-safe wrapper via `after(0, …)` |
| `replace_segments(seg_ids, corrected_text, tag)` | Replaces a span of tagged segments with corrected text (used by grammar correction) |
| `thread_safety_replace_segments(…)` | Thread-safe wrapper |

#### Waveform methods

| Method | Description |
|---|---|
| `_wf_loop()` | Runs every 40 ms via `after()`. Smoothly animates bar heights toward `_wf_targets` |
| `set_waveform_state(state)` | Sets the colour scheme: `"idle"` (blue) / `"recording"` (green) / `"correcting"` (cyan) |
| `thread_safety_waveform(state)` | Thread-safe wrapper |
| `update_audio_rms(rms)` | Called from the audio capture callback with live RMS level to drive waveform heights |

#### Status and button methods

| Method | Description |
|---|---|
| `update_status(label, color)` | Updates the recording status indicator dot |
| `update_lock_btn(locked)` | Switches the Lock/Unlock button between red and blue |
| `set_writing_btn(active)` | Switches Write button between ON (amber) and OFF (dark) |
| `update_chat_port_btn(is_open)` | Switches the Host Server button between active (amber) and idle (dark red) |
| `update_console(message)` | Inserts a timestamped, severity-tagged line into the console log |
| `thread_safety_*()` | Thread-safe `after(0, …)` wrappers for all of the above |

#### Chat sidebar methods

| Method | Description |
|---|---|
| `chat_receive(message)` | Inserts a colour-coded line into the chat log. Handles peer messages, system lines, errors, and headers |
| `_on_chat_send(event)` | Sends the chat entry text to the active transport (LAN client, P2P node, or legacy `ChatServer`) |
| `_on_lan_event(event)` | Handles all incoming WebSocket events from `SpoakenLANClient` — room lists, messages, members, files, errors, shutdown |
| `_on_p2p_event(ev)` | Handles all incoming events from `SpoakenP2PNode` — messages, member join/leave, room creation, files |
| `_toggle_sidebar()` | Shows/hides the 290 px chat sidebar and resizes the window accordingly |

#### File transfer methods

| Method | Description |
|---|---|
| `_on_send_file()` | Opens a file picker (`.txt` only, ≤ 1 MB), then sends via the active transport on a daemon thread |
| `_on_list_files()` | Requests the LAN room's file list; shows the picker dialog when the `m.file.list` event arrives |
| `_show_file_received_banner(filename, saved_path)` | Shows a small teal banner inside the sidebar with Open/Dismiss buttons. Auto-dismisses after 12 s |
| `_show_file_list_dialog(files, source)` | Scrollable popup listing room files with metadata and per-file ⬇ Save buttons |

#### LAN room management

| Method | Description |
|---|---|
| `_on_lan_connect()` | Reads host/port/username/token from the LAN panel, spawns a connect thread, calls `SpoakenLANClient.connect()` |
| `_on_lan_disconnect()` | Disconnects the client and resets all LAN UI state |
| `_on_lan_scan()` | Calls `discover_servers(wait=2.0)` on a background thread; auto-fills the first result into the host/port fields |
| `_on_create_room()` | Opens a room-creation dialog and calls `client.create_room()` |
| `_open_room_picker()` | Scrollable popup showing all LAN or P2P rooms with single-click join |

#### P2P / Tor methods

| Method | Description |
|---|---|
| `_on_p2p_start()` | Starts a `SpoakenP2PNode`, waits for `m.started` event to get the onion address |
| `_on_p2p_stop()` | Stops the P2P node and resets all P2P state |
| `_on_p2p_claim_identity()` | Calls `create_identity()` on the node and shows the DID in the panel |
| `_on_p2p_join_room()` | Parses `host.onion/!roomid` from the join entry and calls `join_room()` |
| `_open_room_browser()` | Dialog showing locally hosted and joined P2P rooms with Select buttons |

---

### spoaken_control.py

**Purpose:** Controller layer — glues `TranscriptionModel` (data) to `TranscriptionView` (GUI). Owns the audio capture loop, grammar correction, LLM processing, chat servers, and close dialog.  
**Online:** ⚠️ Partially — the controller itself is offline; LLM summarize/translate calls Ollama (local) and deep_translator (online).  
**Deletable:** ❌ Required.

#### `TranscriptionController`

#### Initialisation

| Method | Description |
|---|---|
| `set_objects(model, view)` | Called from `spoaken_main.py` after both model and view are instantiated. Starts `SysEnviron`, `ChatServer`, `SSEServer`, and `CommandParser` |
| `_ensure_logs()` | Creates the three rotating log file handlers |
| `_check_display_server()` | Warns if Wayland is detected (xdotool unavailable) |

#### Recording control

| Method | Description |
|---|---|
| `start_recording()` | Opens the sounddevice `InputStream`, starts the Vosk loop and Whisper loop threads |
| `stop_recording()` | Sets `model.is_running = False`, waits for threads, runs `flush_llm_full()` |
| `_stop_if_running()` | Internal helper — stops recording if active and returns whether it was running (used before model hot-swaps) |
| `_audio_callback(indata, frames, time, status)` | Called by sounddevice on the audio capture thread. Converts to bytes, runs noise suppression and VAD, pushes to both `vosk_queue` and `whisper_queue` |

#### Transcription processing

| Method | Description |
|---|---|
| `audio_stream_loop()` | Daemon thread. Drains `vosk_queue`, runs `KaldiRecognizer`, pushes partials to GUI log and finals to `data_store`. Never writes partials to the target window |
| `whisper_loop()` | Daemon thread. Accumulates audio from `whisper_queue` into 8-second chunks, calls `model.transcribe_whisper()`, pushes result to `data_store` |
| `_commit_text(text, source)` | Adds a final sentence to `data_store`, runs duplicate filter, triggers grammar correction, updates GUI, broadcasts to chat, writes to target window (if writing is on), triggers LLM chunk check |
| `_is_duplicate(text)` | Returns True if the text overlaps > 82% with any of the last 30 committed sentences |

#### Grammar and T5

| Method | Description |
|---|---|
| `swap_polishing()` | Starts a background thread that runs `model.run_polish()` on the current transcript and replaces GUI segments with corrected text |
| `run_t5_correction(text)` | Runs the configured T5 model directly (via `transformers`) on a given text or the full transcript |
| `set_t5_model(model)` | Changes the T5 model and persists it to `spoaken_config.json` |
| `swap_vosk_model(model_name)` | Hot-swaps the Vosk model while stopped. Calls `model.reload_vosk()` |
| `swap_whisper_model(model_name)` | Hot-swaps the Whisper model while stopped. Calls `model.reload_whisper()` |

#### LLM background processing

| Method | Description |
|---|---|
| `_maybe_trigger_llm_chunk()` | Called after every committed sentence. Fires a chunk pass if the LLM is enabled, enough new words have accumulated (≥ chunk budget), and the system load gate passes |
| `_llm_chunk_worker(text, cursor_end, mode, model_name, lang)` | Daemon thread. Calls `translate()` or `summarize_llm()` on one chunk, appends to the log file, advances `_llm_word_cursor` |
| `flush_llm_full(mode, lang)` | Processes all text from `_llm_word_cursor` to end in one Ollama call. Called when recording stops. Falls back to extractive summarise if Ollama is offline |
| `run_summarize(text)` | On-demand summarise: tries Ollama first, falls back to `spoaken_summarize` |

#### Writing control

| Method | Description |
|---|---|
| `toggle_writing(state)` | Enables/disables writing to the target window |
| `lock_writer(target_title)` | Creates a `DirectWindowWriter` targeting the fuzzy-matched window title |
| `unlock_writer()` | Destroys the current writer and clears the lock |

#### Chat and broadcast

| Method | Description |
|---|---|
| `chat_send(message)` | Broadcasts a message via the legacy `ChatServer` |
| `toggle_chat_port()` | Starts or stops the `ChatServer` |
| `_broadcast(text)` | Sends to both `ChatServer` and `SSEServer` |
| `_on_chat_message(sender_ip, message)` | Callback from `ChatServer` — pushes incoming messages to the chat sidebar |

#### Logs and session end

| Method | Description |
|---|---|
| `clear_all_logs()` | Truncates all log files, clears GUI transcript, clears `data_store`, resets LLM cursor |
| `copy_transcript()` | Copies the full transcript text to the clipboard |
| `open_logs()` | Opens the three log files in the system default text editor |
| `on_close_request()` | Called by the window's `WM_DELETE_WINDOW` handler — shows the close dialog |
| `_show_close_dialog()` | CTk dialog with three choices: Summarise / Polish / Close without altering |
| `_handle_close_choice(choice)` | Runs the chosen close action on a background thread then destroys the window |

---

### spoaken_connect.py

**Purpose:** Data / model layer. Loads and wraps Vosk and Whisper. Provides the audio device utilities, noise suppression pipeline, grammar polish, and model hot-swap.  
**Online:** ⚠️ `translate_text()` calls Google Translate via `deep_translator` — online only. Whisper model download requires internet the first time (subsequent runs use cache).  
**Deletable:** ❌ Required.

#### Module-level flags

| Flag | Description |
|---|---|
| `VOSK_ACTIVE` | Runtime toggle — set by `controller.set_engine_enabled("vosk", …)` |
| `WHISPER_ACTIVE` | Runtime toggle — set by `controller.set_engine_enabled("whisper", …)` |
| `_happy_cached` | True if a local T5 model hub directory was found in `HAPPY_DIR` |
| `_DEVICE_CACHE` | Cached result of `sd.query_devices()` — populated once on first call |

#### Audio utilities

| Function | Online | Description |
|---|---|---|
| `list_input_devices()` | ❌ | Returns `[(index, name), …]` for all input-capable audio devices |
| `default_device_name()` | ❌ | Returns the name of the system default input device |
| `maybe_suppress_noise(audio_bytes, sr)` | ❌ | Full audio pipeline: high-pass EQ → optional spectral noise reduction → returns processed bytes |
| `audio_gate(pcm_bytes)` | ❌ | Passes audio through the global VAD singleton. Returns audio if speech, `None` if silence |
| `reset_vad()` | ❌ | Resets the VAD gate (call when recording starts to clear stale state) |
| `translate_text(text, target_lang)` | ✅ Online | Sends text to Google Translate via `deep_translator`. Returns translated string or `None` if offline or package missing |

#### Installed model scanners

| Function | Description |
|---|---|
| `scan_installed_vosk_models()` | Returns sorted folder names from `VOSK_DIR`. Returns `["(none installed)"]` if empty |
| `scan_installed_whisper_models()` | Returns sorted model names from `WHISPER_DIR` hub folders. Returns `["(none installed)"]` if empty |

#### `TranscriptionModel`

| Method | Online | Description |
|---|---|---|
| `__init__(vosk_model, status_callback)` | ⚠️ First Whisper run downloads model | Loads Vosk from `VOSK_DIR`, loads Whisper (downloads if not cached), sets up audio queues |
| `_background_load()` | ❌ | Loads the T5 grammar model from local cache only. Sets `TRANSFORMERS_OFFLINE=1` to prevent any HuggingFace download |
| `get_fast_recognizer()` | ❌ | Returns a configured `KaldiRecognizer` for Vosk |
| `reload_vosk(model_name)` | ❌ | Hot-swaps the Vosk model. Must be called while stopped |
| `reload_whisper(model_name)` | ⚠️ Downloads if not cached | Hot-swaps the Whisper model. Must be called while stopped |
| `transcribe_whisper(audio_bytes)` | ❌ | Runs faster-whisper on a raw PCM buffer with built-in VAD filtering |
| `run_polish(store)` | ❌ | Runs T5 grammar correction on the data store in 100-word chunks. Returns `(raw, corrected)` tuple |

---

### spoaken_vad.py

**Purpose:** Voice Activity Detection — decides whether an audio frame contains speech. Bridges short silence gaps and debounces noise pops.  
**Online:** ❌ Fully offline.  
**Deletable:** ⚠️ Optional — if deleted, `spoaken_connect._get_vad()` catches the `ImportError` and disables VAD. Audio will still flow without silence gating.

#### Backends (preference order)
1. **webrtcvad** — Google WebRTC VAD. Fast, 30 ms frame-level detection. `pip install webrtcvad`
2. **Energy gate** — pure numpy fallback. No extra dependencies. Uses RMS threshold.

#### `VAD`

| Method | Description |
|---|---|
| `process(pcm_bytes)` | Stateful gate. Returns `pcm_bytes` when the gate is open (speech detected), `None` when closed (silence). Gate opens after `min_speech_ms` of continuous speech; closes after `silence_gap_ms` of continuous silence |
| `is_speech(pcm_bytes)` | Quick non-stateful check — used by the UI RMS meter |
| `set_aggressiveness(0–3)` | WebRTC VAD aggressiveness. 0 = least aggressive (more false positives), 3 = most aggressive (more false negatives) |
| `set_energy_threshold(rms)` | Energy gate threshold (used when webrtcvad is unavailable) |
| `set_min_speech(ms)` | How long continuous speech must be detected before the gate opens |
| `set_silence_gap(ms)` | How long silence must persist before the gate closes |
| `reset()` | Clears all accumulators and leftover bytes |

---

### spoaken_writer.py

**Purpose:** Sends transcribed text directly into another application's window by simulating keystrokes. Platform-agnostic — picks the best native backend automatically.  
**Online:** ❌ Fully offline.  
**Deletable:** ✅ Optional — if deleted, only the "Write to window" feature is lost. The rest of the app is unaffected.

#### Backend selection

| Platform | Backend | Requirement |
|---|---|---|
| Windows | `pywinauto` (UIA) | `pip install pywinauto` |
| Linux (X11) | `wmctrl` + `xdotool` | `sudo apt install wmctrl xdotool` |
| Linux (Wayland) | pyautogui fallback | Wayland lacks window-targeting APIs |
| macOS | `osascript` (AppleScript) | Built into macOS |
| Any (fallback) | `pyautogui` | `pip install pyautogui` (focus-stealing) |

#### `DirectWindowWriter`

| Method | Description |
|---|---|
| `__init__(title, log_cb)` | Fuzzy-matches `title` against all open windows and locks onto the best match (≥ 65% score required) |
| `write(text)` | Types `text + " "` into the locked window |
| `backspace(count)` | Sends `count` backspace keypresses to the locked window |
| `refresh(title)` | Re-runs the window search with a new query — useful when the target window title changes |

#### `_best_fuzzy_match(query, candidates)`
Uses `rapidfuzz.token_set_ratio` to score window titles. Requires a title word to appear as a substring for scores 65–84%; allows pure score match at 85%+. This handles titles like `"Untitled — Notepad"` matching the query `"notepad"`.

#### Mac alias map (`_MAC_ALIASES`)
A hardcoded dict mapping common app names (`"word"`, `"chrome"`, `"vscode"`) to their actual macOS process names (`"Microsoft Word"`, `"Google Chrome"`, `"Code"`). Checked before the live process list.

---

### spoaken_commands.py

**Purpose:** Single command bus for all user-typed and voice-triggered commands.  
**Online:** ⚠️ `translate` uses `deep_translator` (online). `llm` commands use Ollama (local). `update` opens `spoaken_update.py` which needs internet to check/download packages.  
**Deletable:** ⚠️ Soft-optional — if deleted, the sidebar command bar will produce errors. The rest of the GUI works. Could be replaced with stub `CommandParser` returning `(False, None)` for everything.

#### `CommandParser`

| Method | Description |
|---|---|
| `parse(text)` | Attempts to match text against the command registry. Returns `(handled: bool, console_output: str or None)`. Strips `spoaken.` prefix, `/`, `!`, `:` prefixes |
| `help_text()` | Returns a formatted string listing every registered command |
| `_register(name, handler, description, usage, aliases)` | Adds a command to the registry with optional aliases |

#### Registered commands

| Command | Aliases | Online | Description |
|---|---|---|---|
| `help` | `?`, `commands`, `cmds` | ❌ | List all commands |
| `translate <lang\|off>` | — | ✅ Online | Enable/disable live translation via `deep_translator` |
| `clear` | `wipe`, `reset` | ❌ | Wipe transcript, logs, data stores |
| `polish` | `fix`, `correct`, `grammar` | ❌ | Run T5 grammar correction pass |
| `noise <on\|off>` | `denoise` | ❌ | Toggle noise suppression |
| `port <on\|off>` | — | ❌ | Toggle the chat WebSocket server |
| `record` / `start` | — | ❌ | Start recording |
| `stop` | — | ❌ | Stop recording |
| `copy` | — | ❌ | Copy transcript to clipboard |
| `status` | — | ❌ | Print engine status to console |
| `logs` | — | ❌ | Open log files in default editor |
| `llm <subcommand>` | — | ⚠️ Local Ollama | Control LLM mode (on/off/translate/summarize/model/install/pull) |
| `summarize` | `summary` | ⚠️ Ollama preferred | Summarise current transcript |
| `update` | — | ✅ Online | Open the Update & Repair window |
| `chat.list` | — | ❌ | Request LAN room list |
| `chat.send <msg>` | — | ❌ | Send a message to the active LAN room |
| `chat.connect <host[:port]> [user] [token]` | — | ❌ LAN | Connect to a LAN server programmatically |
| `chat.disconnect` | — | ❌ | Disconnect from LAN server |

#### Invocation styles
Every command can be invoked as:
- `clear` — bare word
- `spoaken.clear()` — dot-style
- `/clear`, `!clear`, `:clear` — prefixed

---

### spoaken_llm.py

**Purpose:** Local LLM backend wrapping Ollama's REST API. Provides translate and summarize functions.  
**Online:** ⚠️ Requires a running local Ollama daemon (`http://localhost:11434`). The LLM models themselves are stored locally; Ollama does not send text anywhere. Pulling new models requires internet.  
**Deletable:** ✅ Optional — if deleted or if Ollama is not running, `spoaken_control` falls back to `deep_translator` (translate) and `spoaken_summarize` (summarize).

#### Key functions

| Function | Description |
|---|---|
| `is_ollama_running()` | Quick cached check — tries `client.list()` |
| `list_ollama_models()` | Returns names of all locally available Ollama models |
| `translate(text, target_lang, model)` | Translates text using the best available model. Falls back to `deep_translator` if Ollama is down |
| `summarize_llm(text, model, ratio)` | Summarises text using the best available model. Falls back to `spoaken_summarize` if Ollama is down |
| `ensure_ollama_pkg(log_fn)` | Runs `pip install ollama` if the package is missing |

#### Preferred model order
- **Translate:** `mistral-small:24b` → `deepseek-r1:14b` → `qwen2.5-1m-abliterated:14b`
- **Summarize:** `deepseek-r1:14b` → `mistral-small:24b` → `qwen2.5-1m-abliterated:14b`

Models are tried in order; first one that responds is used.

#### Important notes
- Set `OLLAMA_HOST` environment variable to change the Ollama endpoint (default `http://localhost:11434`).
- `_ollama_ok` is cached after the first successful ping — subsequent calls do not re-probe.

---

### spoaken_summarize.py

**Purpose:** Lightweight extractive text summariser. No neural model required — pure Python.  
**Online:** ❌ Fully offline.  
**Deletable:** ✅ Optional — if deleted, the summarise feature falls back to Ollama only. If Ollama is also unavailable, summarise returns an error.

#### `summarize(text, ratio, max_sentences)`
Main entry point. Currently always calls `summarize_extractive`. The function signature leaves room for a neural path to be added.

#### `summarize_extractive(text, ratio, max_sentences)`
TF-IDF-style sentence scoring:
1. Splits text into sentences using punctuation heuristics (handles `Mr.`, `Dr.`, etc.)
2. Computes normalised term frequency for content words (stopwords excluded)
3. Scores each sentence by average TF of its content words
4. Applies positional bonuses: first sentence +20%, second +5%, last +10%
5. Selects top `round(n × ratio)` sentences (max `max_sentences`) and returns them in original order

Returns the original text unchanged if it is ≤ 3 sentences.

---

### spoaken_chat.py

**Purpose:** Compatibility shim that routes chat class imports to either the secure or plaintext variants, and optionally the online (P2P) module.  
**Online:** ⚠️ The online (P2P Tor) classes require Tor and are online-routed. The LAN classes are LAN-only.  
**Deletable:** ⚠️ If deleted, `spoaken_control` will fail to import. Replace with stubs if you want to remove all chat features.

#### What it does
1. Always imports the base LAN classes from `spoaken_chat_lan`.
2. Attempts to import online classes from `spoaken_chat_online` — silently degrades if that file is absent.
3. Calls `_load_secure()` — if `cryptography` is installed, imports the hardened classes from `spoaken_chat_lan_secure`; otherwise falls back to the plaintext originals.

#### Exported symbols
`ChatServer`, `SSEServer`, `LANServerBeacon`, `LANServerScanner`, `discover_servers`, `SpoakenLANServer`, `SpoakenLANClient`, `LANServerEntry`, `SpoakenRoom`, `SpoakenUser`, `ChatDB`, `ChatEvent`, `FileTransfer`, `SpoakenOnlineRelay`, `SpoakenOnlineClient`, `OnlineRoom`, `OnlineUser`, `FileRelay`

---

### spoaken_chat_lan.py

**Purpose:** Full WebSocket LAN chat server and client. Rooms, file transfer, persistence, rate limiting, UDP discovery.  
**Online:** ❌ LAN-only. No internet traffic.  
**Deletable:** ✅ Optional — delete to remove all LAN chat capability. `spoaken_chat.py` will fail to import. The rest of the app (transcription, writing, LLM) is unaffected if `spoaken_chat.py` is also removed and `spoaken_control.py` stubs are added.

#### Protocol
- JSON over WebSocket frames
- `c.*` prefix = client-to-server messages
- `m.*` prefix = server-to-client messages
- File chunks: base64 inside JSON, 64 KB per chunk, max file size 50 MB
- Room IDs: `!<8hex>:lan`
- Auth: HMAC-SHA256 challenge-response

#### Privacy guarantees
- IP addresses never logged to disk — ephemeral RAM only
- Room membership lists not sent to members
- Messages contain only sender username and timestamp
- Files stored under content-addressed names (SHA-256 hex) — original paths never kept by server

#### `SpoakenLANServer`

| Method | Description |
|---|---|
| `start()` | Starts the asyncio event loop + WebSocket server on a daemon thread |
| `stop()` | Signals the loop to stop and waits for it to shut down |
| `is_open()` | Returns True if the server is running and accepting connections |
| `peer_count()` | Returns the number of currently connected users |

#### `SpoakenLANClient`

| Method | Online | Description |
|---|---|---|
| `connect(host, port)` | ❌ LAN | Connects, performs HMAC handshake, starts receive loop thread |
| `disconnect()` | ❌ | Signals the receive loop to stop |
| `is_connected()` | ❌ | Returns True if the WebSocket is open |
| `send_message(room_id, text)` | ❌ | Enqueues a `c.room.message` packet |
| `create_room(name, password, public, topic)` | ❌ | Enqueues a `c.room.create` packet |
| `join_room(room_id, password)` | ❌ | Enqueues a `c.room.join` packet |
| `leave_room(room_id)` | ❌ | Enqueues a `c.room.leave` packet |
| `list_rooms()` | ❌ | Requests the server's room list; result arrives as `m.room.list` event |
| `send_file(room_id, filepath)` | ❌ LAN | Reads the file, sends `c.file.begin` + chunks + `c.file.end`. Runs on a background thread |
| `list_files(room_id)` | ❌ LAN | Requests the room's file list; result arrives as `m.file.list` event |
| `download_file(room_id, file_id, dest_path)` | ❌ LAN | Requests a file; result arrives as `m.file.received` event and is saved to `dest_path` if provided |

#### `ChatDB`
SQLite database wrapping rooms, events, files, and bans. Lives at `LOG_DIR/chat.db`. The server auto-creates and migrates the schema on startup.

#### `LANServerBeacon` and `LANServerScanner`
UDP broadcast/listen pair for zero-config LAN server discovery on port 55302. Beacons broadcast every 8 seconds with a 14-second TTL.

#### `discover_servers(wait)`
Blocks for `wait` seconds, returns a list of `LANServerEntry` objects (`name`, `ip`, `port`, `room_count`).

#### `ChatServer` (legacy shim)
Wraps `SpoakenLANServer` with a watchdog thread that restarts the server with exponential backoff if the asyncio loop crashes.

#### `SSEServer` (legacy shim)
HTTP server on port 55301 serving `text/event-stream` for Android/browser live transcript viewing. Has a minimal HTML page at `/` and the stream at `/stream`.

---

### spoaken_chat_lan_secure.py

**Purpose:** Hardened drop-in replacements for the security-sensitive classes in `spoaken_chat_lan.py`.  
**Online:** ❌ LAN-only.  
**Deletable:** ✅ Optional — if deleted, `spoaken_chat.py` falls back to the plaintext classes in `spoaken_chat_lan.py` automatically.

#### What it hardens

| Class | Hardening |
|---|---|
| `SecureLANServerBeacon` | Signs every UDP discovery packet with Ed25519. Unsigned/replayed packets are silently dropped |
| `SecureLANServerScanner` | Verifies Ed25519 signatures; maintains a nonce cache to reject replays |
| `SecureChatServer` | Passes mTLS `ssl_context` from `spoaken_config.get_server_ssl_context()` and binds to `BIND_ADDRESS` |
| `SecureSSEServer` | Binds to `BIND_ADDRESS`; requires a time-bound HMAC bearer token on `/stream` |

---

### spoaken_chat_online.py

**Purpose:** Fully peer-to-peer, Tor-routed chat. No external servers, no accounts, no relay.  
**Online:** ✅ Yes — routes all traffic through Tor. Requires Tor daemon running (`sudo systemctl start tor`).  
**Deletable:** ✅ Optional — `spoaken_chat.py` has a try/except around this import and degrades gracefully. All LAN and offline features are unaffected.

#### Required packages
```
pip install websockets PySocks stem cryptography
```

#### Identity model
Every user has a persistent local identity stored in `spoaken_config.json` under `p2p_identity`:
- `username` — human-readable display name
- `did` — `did:spoaken:<base58(sha256(pubkey)[:16])>` — derived identifier
- `did_key_hex` — persistent Ed25519 private key (64 hex chars)

A separate *session keypair* is generated each run — only the DID URI and an ephemeral pubkey are shared over the network. The full private key never leaves the device.

#### Room model
- The first peer to call `create_room()` becomes HOST
- The host runs a local WebSocket server; its `.onion:port` is the room address
- Members connect via Tor's SOCKS5 proxy to `host.onion`
- Room passwords use PBKDF2-HMAC-SHA256 (100,000 rounds)

#### `SpoakenP2PNode`

| Method | Online | Description |
|---|---|---|
| `start()` | ✅ Tor required | Creates a Tor hidden service, starts the WS server, sets `_onion` address |
| `stop()` | ✅ | Tears down the hidden service and closes all connections |
| `is_started()` | ✅ | Returns True if the node is running |
| `create_identity(username)` | ❌ | Creates or loads a persistent DID identity |
| `create_room(name, password, public, topic)` | ✅ | Creates a room on the local WS server |
| `join_room(host_onion, room_id, password)` | ✅ | Connects to a remote room via Tor |
| `leave_room(room_id)` | ✅ | Leaves a room |
| `send_message(room_id, text)` | ✅ | Sends a chat message |
| `send_file(room_id, filepath)` | ✅ | Sends a file in 32 KB base64 chunks |
| `list_rooms(notify)` | ❌ | Returns hosted and joined rooms; optionally fires `m.room.list` event |

#### `SpoakenOnlineClient` (legacy shim)
Subclasses `SpoakenP2PNode` to keep the old relay-based API working. `connect(url)` calls `start()`, `join_room("host.onion/!roomid")` parses and delegates.

---

### spoaken_crypto.py

**Purpose:** All cryptographic primitives: PKI/CA, TLS contexts, HMAC tokens, AES-GCM envelopes, and Ed25519 beacon signing.  
**Online:** ❌ Fully offline. Key generation, cert issuance, signing, and verification all run locally.  
**Deletable:** ✅ Optional — if deleted, TLS and signed beacons are disabled. `USE_TLS` must be set to `False` in `spoaken_config.json`. The `cryptography` package is required (`pip install cryptography`).

#### `SpoakenCA`
Self-signed certificate authority stored in `PKI_DIR`.

| Method | Description |
|---|---|
| `__init__(pki_dir)` | Generates CA key and self-signed cert if not already present |
| `issue(cn)` | Issues a leaf cert signed by the CA. Returns a `SpoakenCerts` dataclass with paths to cert/key/CA |
| `ca_cert_path` | Property — path to the CA cert PEM file |

#### `HMACToken`
Time-bound HMAC-SHA256 bearer tokens.

| Method | Description |
|---|---|
| `mint()` | Returns a base64 token containing `timestamp + HMAC(secret, timestamp)` |
| `verify(token)` | Returns True if the token is valid and within TTL ± clock_skew |

#### `AESEnvelope`
AES-256-GCM per-message encryption (opt-in via `MSG_ENVELOPE`).

| Method | Description |
|---|---|
| `encrypt(plaintext)` | Returns `nonce (12 B) + ciphertext + tag (16 B)` as bytes |
| `decrypt(data)` | Decrypts and verifies. Raises `ValueError` on authentication failure |

#### `Ed25519Beacon`
Signs and verifies UDP discovery packets.

| Method | Description |
|---|---|
| `sign(payload)` | Returns `payload + Ed25519_signature(payload)` |
| `verify(packet)` | Returns the original payload bytes if signature is valid, raises otherwise |

#### `build_server_ssl(certs)` / `build_client_ssl(certs)` / `build_client_ssl_no_mtls(ca_cert_path)`
Return `ssl.SSLContext` objects configured for WSS. The server context enforces mTLS by requiring client certificates signed by the shared CA.

---

### spoaken_sysenviron.py

**Purpose:** System environment monitor. Benchmarks the machine at startup, monitors CPU/RAM during sessions, throttles LLM activity under load, and shows a popup alert when the system is overloaded.  
**Online:** ❌ Fully offline.  
**Deletable:** ✅ Optional — if deleted or if the import fails, `spoaken_control` catches the exception and prints a warning. The LLM chunk budget falls back to 80 words/pass.

#### `SysEnviron`

| Method | Description |
|---|---|
| `start()` | Starts `benchmark()` on a daemon thread, then starts the polling loop |
| `benchmark()` | Measures CPU speed with a busy-loop, classifies the machine into a tier, and sets the LLM chunk budget |
| `can_run_llm()` | Returns True if current CPU < 70% and RAM < 85% (thresholds adjust by tier) |
| `get_llm_chunk_budget()` | Returns the calibrated word count per LLM pass |
| `_poll_loop()` | Runs every 5 s. Reads CPU/RAM, detects overload, calls `_show_alert()` if thresholds are exceeded |
| `_show_alert(cpu, ram, action)` | Creates a `CTkToplevel` popup with the animated `plane.gif` icon, stats, and an OK button. Auto-dismisses after 15 s. Throttled to once per `_alert_interval` |

#### Machine tiers and LLM budgets

| Tier | CPU speed | Words/pass |
|---|---|---|
| `fast` | > 800 M ops/s | 150 |
| `medium` | > 300 M ops/s | 80 |
| `slow` | > 100 M ops/s | 40 |
| `very_slow` | ≤ 100 M ops/s | 20 |

---

### spoaken_mic_config.py

**Purpose:** Microphone configuration and audio tuning panel. Opens as a floating window over the main app.  
**Online:** ❌ Fully offline.  
**Deletable:** ✅ Optional — if deleted, mic configuration is unavailable from the GUI but the default settings in `spoaken_connect._mic_config` are still applied.

#### `MicConfigPanel(ctk.CTkToplevel)`

| Feature | Description |
|---|---|
| Live RMS meter | Updates every 40 ms from a background sounddevice stream |
| VAD gate indicator | Shows `SPEECH` or `SILENCE` based on the live VAD result |
| VAD sliders | Aggressiveness (0–3), min-speech (50–2000 ms), silence-gap (100–3000 ms) |
| EQ presets | **Flat** (no filter), **Speech** (80 Hz HP, default), **Aggressive** (100 Hz HP + 60/120 Hz notches), **Custom** (manual HP cutoff slider) |
| Noise profile capture | Records 2 s of ambient sound for use as a stationary noise profile in `noisereduce` |
| NR strength slider | 0.0–1.0 strength for `noisereduce` spectral reduction |
| 5-second test record | Records 5 s of audio with current settings and runs it through Vosk AND Whisper. Shows word counts and transcription side-by-side |
| Apply | Writes all settings to `spoaken_connect._mic_config` immediately — no restart needed |

---

### spoaken_update.py

**Purpose:** Update & Repair window for Spoaken packages and models. Can run standalone or embedded.  
**Online:** ✅ Requires internet for version checks, package updates, and model downloads.  
**Deletable:** ✅ Optional — if deleted, the Update button in the header will show an error. All offline features work normally.

#### `DownloadProgressWindow(ctk.CTkToplevel)`
Reusable progress window for any background download task.

| Method | Description |
|---|---|
| `start_download(worker_fn, *args)` | Starts `worker_fn` on a daemon thread, passing the `DownloadProgressWindow` as a kwarg |
| `log(message)` | Appends a line to the scrolling log (thread-safe) |
| `set_progress(value, text)` | Updates the per-file progress bar and status label |
| `set_overall(current, total)` | Updates the `item N of M` counter |
| `is_cancelled()` | Returns True if the user clicked Cancel |
| `mark_done(success)` | Shows a success/failure banner and enables the Close button |

#### `SpoakenUpdater(ctk.CTkToplevel)`

**Package management tab:**
- Scans all Spoaken dependencies with `importlib.metadata` and `pip index versions`
- Shows current vs latest versions with ✔ / ↑ / ✗ status icons
- **Update** button (neon teal) — upgrades all out-of-date or missing packages in background
- **Repair** button — reinstalls every package regardless of current version
- **Check** button — refreshes version table without installing

**Model management tab:**

| Method | Description |
|---|---|
| `_on_install_vosk()` | Downloads selected Vosk model via `urllib.request`, extracts zip to `VOSK_DIR` |
| `_on_install_whisper()` | Downloads selected Whisper model by instantiating `WhisperModel` (faster-whisper handles the download) |
| `_download_vosk_worker(…)` | Background worker — streams download with progress hook, extracts, verifies |
| `_download_whisper_worker(…)` | Background worker — triggers faster-whisper's built-in model download |

**App update tab:**
- Checks GitHub API (`https://api.github.com/repos/daltyn-maker/Spoaken`) for new releases
- **Update Spoaken** button (indigo/violet) — downloads zip from GitHub, extracts, restarts

**T5 model tab:**
- Lists curated grammar/paraphrase T5 models from HuggingFace
- Downloads and caches chosen model to `HAPPY_DIR/hub/` with `TRANSFORMERS_OFFLINE` enforcement

---

## 4. Threading Model

| Thread | Owns | Notes |
|---|---|---|
| **Main thread** | tkinter/CTk event loop | All `widget.configure()`, `after()`, GUI updates must run here |
| **Init thread** | Model/controller instantiation | Daemon. Runs once at startup then exits |
| **Audio capture** (`sounddevice` callback) | `_audio_callback` | Called by PortAudio on a real-time thread. Must be fast — no blocking I/O |
| **Vosk loop** | `audio_stream_loop()` | Daemon. Drains `vosk_queue`, runs KaldiRecognizer |
| **Whisper loop** | `whisper_loop()` | Daemon. Accumulates 8 s chunks, runs `transcribe_whisper()` |
| **LLM chunk worker** | `_llm_chunk_worker()` | Daemon. Spawned per chunk when enough new words accumulate |
| **LAN receive loop** | `SpoakenLANClient._recv_loop()` | Daemon. One per connected client |
| **LAN server loop** | `SpoakenLANServer` asyncio | Daemon. Runs the asyncio event loop in its own thread |
| **P2P node** | `SpoakenP2PNode` | Daemon. One asyncio loop + Tor hidden service |
| **SysEnviron poll** | `_poll_loop()` | Daemon. Wakes every 5 s |
| **Chat server watchdog** | `ChatServer._watchdog()` | Daemon. Wakes every 2–60 s (exponential backoff) |

**Thread safety rule:** All GUI updates from non-main threads must go through `view.after(0, fn, *args)` or a `thread_safety_*()` wrapper method.

---

## 5. Audio Pipeline

```
sounddevice InputStream
        │
        ▼
_audio_callback()
  ├── maybe_suppress_noise()   [EQ → noisereduce]
  ├── audio_gate()             [VAD — returns None if silence]
  │
  ├── vosk_queue.put()         ─────────────────────────────────┐
  └── whisper_queue.put()      ──────────────────────┐          │
                                                      │          │
                                              whisper_loop()  audio_stream_loop()
                                              accumulate 8 s   KaldiRecognizer
                                              → transcribe_whisper()   │
                                                      │          │ partials → GUI only
                                                      │          │ finals ──────────┐
                                                      └──────────┴──────────────────▼
                                                                         _commit_text()
                                                                           │
                                                                    ├── duplicate filter
                                                                    ├── grammar T5 (if enabled)
                                                                    ├── GUI transcript insert
                                                                    ├── broadcast to chat
                                                                    ├── write to target window
                                                                    └── trigger LLM chunk
```

---

## 6. What Can Be Deleted

| File | Safe to delete? | What you lose |
|---|---|---|
| `spoaken_vad.py` | ✅ Yes | VAD silence gating — all audio passes through |
| `spoaken_writer.py` | ✅ Yes | "Write to window" feature entirely |
| `spoaken_llm.py` | ✅ Yes | Ollama LLM summarize/translate — extractive fallback still works |
| `spoaken_summarize.py` | ✅ Yes | Extractive summarise — Ollama-only summarise remains |
| `spoaken_sysenviron.py` | ✅ Yes | System monitoring and load-aware LLM throttling |
| `spoaken_mic_config.py` | ✅ Yes | Mic tuning panel — default settings still apply |
| `spoaken_update.py` | ✅ Yes | Update & Repair window — manual pip updates still work |
| `spoaken_chat_online.py` | ✅ Yes | P2P Tor chat — LAN chat unaffected |
| `spoaken_chat_lan_secure.py` | ✅ Yes | TLS/mTLS hardening — falls back to plaintext LAN |
| `spoaken_crypto.py` | ✅ Yes (if USE_TLS=false) | All TLS, mTLS, HMAC tokens, signed beacons |
| `spoaken_commands.py` | ⚠️ With stub | Sidebar command bar — replace with stub `CommandParser` |
| `spoaken_chat.py` | ⚠️ With stubs | All chat — requires stubbing in `spoaken_control.py` |
| `spoaken_chat_lan.py` | ⚠️ With stubs | LAN chat — `spoaken_chat.py` imports from it directly |
| `paths.py` | ❌ No | Everything — every module imports from it |
| `spoaken_config.py` | ❌ No | Everything — every module imports from it |
| `spoaken_main.py` | ❌ No | Entry point |
| `spoaken_splash.py` | ❌ No | `spoaken_main.py` imports it directly |
| `spoaken_gui.py` | ❌ No | The entire UI |
| `spoaken_control.py` | ❌ No | The controller — glues everything |
| `spoaken_connect.py` | ❌ No | Vosk, Whisper, audio processing |

---

## 7. Online vs Offline Feature Matrix

| Feature | Online Required? | Notes |
|---|---|---|
| Vosk transcription | ❌ | Fully offline after model download |
| Whisper transcription | ❌ | Offline after first model download |
| Grammar correction (T5) | ❌ | Offline after model cache built via Update & Repair |
| Write to window | ❌ | Uses OS-level keystroke injection |
| LAN chat | ❌ | Wi-Fi/LAN only |
| LAN file transfer | ❌ | Wi-Fi/LAN only |
| P2P Tor chat | ✅ Tor routing | Requires Tor daemon. Routes through Tor network |
| LLM summarize (Ollama) | ❌ Local | Ollama runs locally; no cloud |
| LLM translate (Ollama) | ❌ Local | Ollama runs locally; no cloud |
| Translation (deep_translator) | ✅ Google API | Sends text to Google Translate |
| Package updates | ✅ PyPI | `pip install` needs internet |
| Vosk model download | ✅ | Downloads from alphacephei.com |
| Whisper model download | ✅ | Downloads from HuggingFace via faster-whisper |
| T5 model cache | ✅ | Downloads from HuggingFace once; offline after |
| Spoaken app update | ✅ GitHub | Downloads from GitHub |

---

## 8. Required Packages

#### Core (always needed)
```
pip install customtkinter pillow sounddevice numpy vosk faster-whisper
pip install pyautogui rapidfuzz
```

#### Recommended
```
pip install webrtcvad noisereduce
```

#### Grammar correction
```
pip install "happytransformer<4.0.0" transformers torch
```

#### LAN chat + security
```
pip install websockets cryptography
```

#### P2P Tor chat
```
pip install PySocks stem
# System: sudo apt install tor
```

#### Window writing (Linux)
```
sudo apt install wmctrl xdotool
```

#### Window writing (Windows, optional upgrade from pyautogui)
```
pip install pywinauto
```

#### Translation
```
pip install deep-translator
```

#### Local LLM
```
pip install ollama
# Then: ollama pull mistral-small:24b
```

---

## 9. Configuration Reference

All settings live in `<install_dir>/spoaken_config.json`. Missing keys fall back to the defaults shown in `spoaken_config.py`.

```json
{
  "offline_mode": false,
  "happy_online_only": true,

  "mic_device": null,
  "noise_suppression": true,
  "vosk_enabled": true,
  "vosk_model": "vosk-model-small-en-us-0.15",
  "whisper_enabled": true,
  "whisper_model": "small",
  "whisper_compute": "auto",
  "gpu_enabled": false,
  "grammar_enabled": true,
  "memory_cap_words": 2000,
  "memory_cap_minutes": 60,
  "duplicate_filter": true,
  "t5_model": "vennify/t5-base-grammar-correction",

  "chat_server_enabled": false,
  "chat_server_port": 55300,
  "chat_server_token": "spoaken",
  "android_stream_enabled": false,
  "android_stream_port": 55301,
  "bind_address": "",

  "use_tls": true,
  "mtls_enabled": true,
  "pki_dir": "~/.spoaken/pki",
  "server_cert_cn": "spoaken-server",
  "client_cert_cn": "spoaken-client",
  "token_ttl": 300.0,
  "token_clock_skew": 60.0,
  "beacon_sign": true,
  "msg_envelope": false,
  "log_tls_events": true
}
```

---

## 10. Command Reference

Type any of these into the sidebar command bar (or speak them if voice commands are wired up).

```
help                          List all commands
translate french              Start live translation to French
translate off                 Stop translation
clear                         Wipe transcript and logs
polish                        Run grammar correction now
noise on                      Enable noise suppression
noise off                     Disable noise suppression
port on                       Open the chat WebSocket server
port off                      Close the chat WebSocket server
record                        Start recording
stop                          Stop recording
copy                          Copy transcript to clipboard
status                        Print engine status
logs                          Open log files
summarize                     Summarise transcript (Ollama → extractive fallback)
llm on                        Enable background LLM processing
llm off                       Disable background LLM processing
llm translate                 Set LLM mode to translation
llm summarize                 Set LLM mode to summarisation
llm model mistral-small:24b   Set active LLM model
llm status                    Show LLM status
llm install                   Install the ollama Python package
llm pull                      Show model pull commands
update                        Open the Update & Repair window
chat.list                     List LAN rooms
chat.send Hello               Send "Hello" to the active LAN room
chat.connect 192.168.1.5      Connect to LAN server at that IP
chat.disconnect               Disconnect from LAN server
```

All commands also accept `spoaken.` prefix and `/`, `!`, `:` prefix variants:
```
spoaken.clear()
/translate(french)
!stop
```
