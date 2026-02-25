"""
spoaken_writer.py
─────────────────
Platform-agnostic window writer.
Supports Linux (wmctrl + xdotool), Windows (pywinauto), macOS (osascript).
Falls back to pyautogui (focus-stealing) if the native backend fails.
"""

import os
import platform
import shutil
import socket
import subprocess

import pyautogui
from rapidfuzz import fuzz

_SYSTEM    = platform.system()
_THRESHOLD = 65


def _best_fuzzy_match(query: str, candidates: list) -> tuple:
    q          = query.lower().strip()
    best_item  = None
    best_score = 0

    for candidate in candidates:
        if isinstance(candidate, tuple):
            key, label = candidate
            text = label.lower()
        else:
            key  = candidate
            text = candidate.lower()

        # token_set_ratio handles extra words in titles well
        # e.g. "libreoffice" → "Untitled 1 - LibreOffice Writer" scores 100
        score = fuzz.token_set_ratio(q, text)
        if score > best_score:
            best_score = score
            best_item  = candidate

    if best_score >= _THRESHOLD:
        # Extra guard: query word should appear somewhere in the matched title
        label = best_item[1] if isinstance(best_item, tuple) else best_item
        if q in label.lower():
            return best_item, best_score
        elif best_score >= 85:
            # Very high confidence — allow even without substring match
            return best_item, best_score

    return None, 0


# ─────────────────────────────────────────────────────────────────────────────
# Fallback  (pyautogui — focus-stealing)
# ─────────────────────────────────────────────────────────────────────────────

def _fallback_write(text: str):
    pyautogui.write(text + " ", interval=0.01)


def _fallback_backspace(count: int):
    pyautogui.press("backspace", presses=count, interval=0.01)


# ─────────────────────────────────────────────────────────────────────────────
# Windows backend  (pywinauto)
# ─────────────────────────────────────────────────────────────────────────────

class _WindowsWriter:

    def __init__(self, query: str, log_cb):
        self._query  = query
        self._log    = log_cb
        self._window = None
        self._connect()

    def _get_all_windows(self):
        from pywinauto import Desktop
        desk    = Desktop(backend="uia")
        results = []
        for w in desk.windows(visible_only=True):
            try:
                title = w.window_text().strip()
                if title:
                    results.append((w, title))
            except Exception:
                pass
        return results

    def _connect(self):
        try:
            windows = self._get_all_windows()
            if not windows:
                self._log("[Writer]: no visible windows found")
                self._window = None
                return

            match, score = _best_fuzzy_match(self._query, windows)

            if match is None:
                self._log(
                    f"[Writer]: no window matched '{self._query}' "
                    f"above {_THRESHOLD}% — check spelling or try a shorter name"
                )
                self._window = None
                return

            self._window      = match[0]
            matched_title     = match[1]
            self._log(
                f"[Writer]: locked → '{matched_title}'  "
                f"(match {score}%)  [Windows/UIA]"
            )

        except Exception as e:
            self._log(f"[Writer]: pywinauto error — {e}")
            self._window = None

    def write(self, text: str):
        if self._window is None:
            _fallback_write(text)
            return
        try:
            escaped = (
                text
                .replace("{", "{{").replace("}", "}}")
                .replace("+", "{+}").replace("^", "{^}")
                .replace("%", "{%}").replace("~", "{~}")
            )
            self._window.type_keys(
                escaped + " ", with_spaces=True, set_foreground=False
            )
        except Exception as e:
            self._log(f"[Writer]: type_keys failed — {e} — falling back")
            _fallback_write(text)

    def backspace(self, count: int):
        if self._window is None:
            _fallback_backspace(count)
            return
        try:
            self._window.type_keys(
                "{BACKSPACE}" * count, set_foreground=False
            )
        except Exception as e:
            self._log(f"[Writer]: backspace failed — {e} — falling back")
            _fallback_backspace(count)

    def refresh(self, query: str):
        self._query = query
        self._connect()


# ─────────────────────────────────────────────────────────────────────────────
# Linux backend  (wmctrl + xdotool)
# ─────────────────────────────────────────────────────────────────────────────

def _check_tool(name: str) -> bool:
    return shutil.which(name) is not None


class _LinuxWriter:

    def __init__(self, query: str, log_cb):
        self._query = query
        self._log   = log_cb
        self._wid   = None
        self._connect()

    def _list_windows(self) -> list:
        result = subprocess.run(
            ["wmctrl", "-l"], capture_output=True, text=True, timeout=3
        )
        hostname = socket.gethostname()
        windows  = []

        for line in result.stdout.strip().splitlines():
            # wmctrl format: id  desktop  hostname  title
            parts = line.split(None, 3)
            if len(parts) == 4:
                hex_id = parts[0]
                title  = parts[3].strip()

                # Strip hostname prefix if wmctrl prepended it
                if title.startswith(hostname + " "):
                    title = title[len(hostname) + 1:].strip()
                elif title.startswith(hostname):
                    title = title[len(hostname):].strip()

                if title and title not in ("-", ""):
                    windows.append((hex_id, title))

        return windows

    def _connect(self):
        missing = [t for t in ("wmctrl", "xdotool") if not _check_tool(t)]
        if missing:
            self._log(
                f"[Writer]: {', '.join(missing)} not found — "
                f"install with: sudo apt install {' '.join(missing)}"
            )
            self._wid = None
            return

        try:
            windows = self._list_windows()
            if not windows:
                self._log("[Writer]: wmctrl returned no windows")
                self._wid = None
                return

            match, score = _best_fuzzy_match(self._query, windows)

            if match is None:
                self._log(
                    f"[Writer]: no window matched '{self._query}' "
                    f"above {_THRESHOLD}% — check spelling or try a shorter name"
                )
                self._wid = None
                return

            self._wid     = match[0]
            matched_title = match[1]
            self._log(
                f"[Writer]: locked → '{matched_title}'  "
                f"(id {self._wid}, match {score}%)  [Linux/xdotool]"
            )

        except Exception as e:
            self._log(f"[Writer]: window scan failed — {e}")
            self._wid = None

    def write(self, text: str):
        if self._wid is None:
            _fallback_write(text)
            return
        try:
            subprocess.run(
                [
                    "xdotool", "type",
                    "--window",         self._wid,
                    "--clearmodifiers",
                    "--delay",          "0",
                    text + " ",
                ],
                timeout=5,
            )
        except Exception as e:
            self._log(f"[Writer]: xdotool type failed — {e} — falling back")
            _fallback_write(text)

    def backspace(self, count: int):
        if self._wid is None:
            _fallback_backspace(count)
            return
        try:
            keys = ["BackSpace"] * count
            subprocess.run(
                [
                    "xdotool", "key",
                    "--window",         self._wid,
                    "--clearmodifiers",
                ] + keys,
                timeout=5,
            )
        except Exception as e:
            self._log(f"[Writer]: xdotool backspace failed — {e} — falling back")
            _fallback_backspace(count)

    def refresh(self, query: str):
        self._query = query
        self._connect()


# ─────────────────────────────────────────────────────────────────────────────
# macOS backend  (osascript System Events)
# ─────────────────────────────────────────────────────────────────────────────

_MAC_ALIASES = {
    "libreoffice":         "soffice",
    "libre office":        "soffice",
    "libreoffice writer":  "soffice",
    "writer":              "soffice",
    "libreoffice calc":    "soffice",
    "calc":                "soffice",
    "libreoffice impress": "soffice",
    "impress":             "soffice",
    "soffice":             "soffice",
    "chrome":              "Google Chrome",
    "google chrome":       "Google Chrome",
    "firefox":             "Firefox",
    "safari":              "Safari",
    "arc":                 "Arc",
    "brave":               "Brave Browser",
    "edge":                "Microsoft Edge",
    "word":                "Microsoft Word",
    "microsoft word":      "Microsoft Word",
    "excel":               "Microsoft Excel",
    "microsoft excel":     "Microsoft Excel",
    "powerpoint":          "Microsoft PowerPoint",
    "onenote":             "Microsoft OneNote",
    "outlook":             "Microsoft Outlook",
    "textedit":            "TextEdit",
    "text edit":           "TextEdit",
    "notes":               "Notes",
    "pages":               "Pages",
    "numbers":             "Numbers",
    "keynote":             "Keynote",
    "mail":                "Mail",
    "calendar":            "Calendar",
    "terminal":            "Terminal",
    "iterm":               "iTerm2",
    "iterm2":              "iTerm2",
    "vscode":              "Code",
    "vs code":             "Code",
    "visual studio code":  "Code",
    "sublime":             "Sublime Text",
    "sublime text":        "Sublime Text",
    "bbedit":              "BBEdit",
    "atom":                "Atom",
    "xcode":               "Xcode",
    "pycharm":             "PyCharm",
    "intellij":            "IntelliJ IDEA",
    "slack":               "Slack",
    "discord":             "Discord",
    "zoom":                "zoom.us",
    "notion":              "Notion",
    "obsidian":            "Obsidian",
    "bear":                "Bear",
    "ulysses":             "Ulysses",
    "scrivener":           "Scrivener 3",
    "typora":              "Typora",
}


def _escape_applescript(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _list_mac_processes() -> list:
    script = (
        'tell application "System Events"\n'
        '    get name of every process where background only is false\n'
        'end tell'
    )
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0:
        return []
    return [p.strip() for p in result.stdout.strip().split(",") if p.strip()]


class _MacWriter:

    _CHUNK = 80

    def __init__(self, query: str, log_cb):
        self._query   = query
        self._log     = log_cb
        self._process = None
        self._connect()

    def _resolve(self, query: str) -> tuple:
        q = query.lower().strip()

        alias_keys   = list(_MAC_ALIASES.keys())
        match, score = _best_fuzzy_match(q, alias_keys)
        if match is not None:
            return _MAC_ALIASES[match], score, "alias map"

        live_procs = _list_mac_processes()
        if live_procs:
            match, score = _best_fuzzy_match(query, live_procs)
            if match is not None:
                return match, score, "live processes"

        return None, 0, None

    def _connect(self):
        process, score, source = self._resolve(self._query)
        if process is None:
            self._log(
                f"[Writer]: no process matched '{self._query}' "
                f"above {_THRESHOLD}% — check spelling or try a shorter name"
            )
            self._process = None
            return

        self._process = process
        self._log(
            f"[Writer]: locked → '{self._process}'  "
            f"(match {score}% via {source})  [macOS/osascript]"
        )

    def _run_applescript(self, script: str) -> bool:
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                self._log(f"[Writer]: osascript error — {result.stderr.strip()}")
                return False
            return True
        except Exception as e:
            self._log(f"[Writer]: osascript exception — {e}")
            return False

    def write(self, text: str):
        if self._process is None:
            _fallback_write(text)
            return
        text_with_space = text + " "
        for i in range(0, len(text_with_space), self._CHUNK):
            chunk = text_with_space[i:i + self._CHUNK]
            safe  = _escape_applescript(chunk)
            script = (
                f'tell application "System Events"\n'
                f'    tell process "{self._process}"\n'
                f'        keystroke "{safe}"\n'
                f'    end tell\n'
                f'end tell'
            )
            if not self._run_applescript(script):
                _fallback_write(chunk)

    def backspace(self, count: int):
        if self._process is None:
            _fallback_backspace(count)
            return
        remaining = count
        while remaining > 0:
            batch  = min(remaining, 50)
            script = (
                f'tell application "System Events"\n'
                f'    tell process "{self._process}"\n'
                f'        repeat {batch} times\n'
                f'            key code 51\n'
                f'        end repeat\n'
                f'    end tell\n'
                f'end tell'
            )
            if not self._run_applescript(script):
                _fallback_backspace(batch)
            remaining -= batch

    def refresh(self, query: str):
        self._query = query
        self._connect()


# ─────────────────────────────────────────────────────────────────────────────
# Public interface
# ─────────────────────────────────────────────────────────────────────────────

class DirectWindowWriter:
    """
    Platform-agnostic writer. Instantiate with a query string and call
    .write() / .backspace().  Call .refresh(new_query) to retarget.

    Examples:
        DirectWindowWriter("libreoffice")
        DirectWindowWriter("chrome")
        DirectWindowWriter("vs code")
        DirectWindowWriter("notepad")
    """

    def __init__(self, title: str, log_cb=print):
        self._log     = log_cb
        self._backend = self._make_backend(title)

    def _make_backend(self, title: str):
        if not title.strip():
            self._log(
                "[Writer]: no target window set — "
                "will write to whatever window is currently active"
            )
            return None

        if _SYSTEM == "Windows":
            return _WindowsWriter(title, self._log)

        elif _SYSTEM == "Linux":
            if os.environ.get("DISPLAY"):
                return _LinuxWriter(title, self._log)
            else:
                self._log(
                    "[Writer]: no DISPLAY found (Wayland?) — "
                    "xdotool unavailable, falling back to pyautogui"
                )
                return None

        elif _SYSTEM == "Darwin":
            return _MacWriter(title, self._log)

        else:
            self._log(
                f"[Writer]: unsupported platform '{_SYSTEM}' — "
                "falling back to pyautogui"
            )
            return None

    def write(self, text: str):
        if self._backend:
            self._backend.write(text)
        else:
            _fallback_write(text)

    def backspace(self, count: int):
        if self._backend:
            self._backend.backspace(count)
        else:
            _fallback_backspace(count)

    def refresh(self, title: str):
        if self._backend:
            self._backend.refresh(title)
        else:
            self._backend = self._make_backend(title)
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
