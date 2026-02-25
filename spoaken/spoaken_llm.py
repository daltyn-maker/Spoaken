"""
spoaken_llm.py
──────────────
Local LLM backend for Spoaken — v2.

Wraps Ollama's REST API to expose:
  • translate(text, target_lang, model) → str
  • summarize_llm(text, model)          → str

Supported models (auto-detected from running Ollama instance):
  Mistral-Small-24B-Instruct-2501-GGUF  (Q8_0)
  deepseek-r1:14b
  huihui_ai/qwen2.5-1m-abliterated:14b
  (any other Ollama-served model the user has pulled)

Fallback chain
──────────────
  1. Try each preferred model in order until one responds
  2. If Ollama is unavailable → fall back to deep_translator (translate)
     or spoaken_summarize (summarize)

Install requirements
────────────────────
  • Ollama desktop app:  https://ollama.com
  • Python client:       pip install ollama
  • Models (pull once):
      ollama pull mistral-small:24b
      ollama pull deepseek-r1:14b
      ollama pull huihui_ai/qwen2.5-1m-abliterated:14b

Public API
──────────
  list_ollama_models()                          → list[str]
  translate(text, target_lang, model=None)      → str
  summarize_llm(text, model=None, ratio=0.33)   → str
  is_ollama_running()                            → bool
"""

from __future__ import annotations

import sys
import threading
from typing import Optional

# ── Preferred model order (user can override via config/GUI) ──────────────────
PREFERRED_TRANSLATE_MODELS = [
    "mistral-small:24b",                          # Mistral-Small-24B-Instruct
    "deepseek-r1:14b",                            # DeepSeek-R1 14B
    "huihui_ai/qwen2.5-1m-abliterated:14b",       # Qwen2.5 14B abliterated
]

PREFERRED_SUMMARIZE_MODELS = [
    "deepseek-r1:14b",
    "mistral-small:24b",
    "huihui_ai/qwen2.5-1m-abliterated:14b",
]

# Ollama base URL — override with OLLAMA_HOST env var if needed
import os as _os
OLLAMA_BASE = _os.environ.get("OLLAMA_HOST", "http://localhost:11434")

# ── Lazy Ollama client ────────────────────────────────────────────────────────
_ollama_client = None
_ollama_lock   = threading.Lock()
_ollama_ok     = None   # None = untested, True/False = cached result


def _get_client():
    global _ollama_client, _ollama_ok
    with _ollama_lock:
        if _ollama_client is not None:
            return _ollama_client
        try:
            import ollama
            _ollama_client = ollama.Client(host=OLLAMA_BASE)
            return _ollama_client
        except ImportError:
            return None
        except Exception:
            return None


def is_ollama_running() -> bool:
    """Return True if the Ollama daemon is reachable."""
    global _ollama_ok
    if _ollama_ok is not None:
        return _ollama_ok
    try:
        import urllib.request
        with urllib.request.urlopen(f"{OLLAMA_BASE}/api/tags", timeout=2) as r:
            _ollama_ok = r.status == 200
    except Exception:
        _ollama_ok = False
    return _ollama_ok


def list_ollama_models() -> list[str]:
    """Return list of model names currently available in Ollama."""
    global _ollama_ok
    try:
        import urllib.request, json
        with urllib.request.urlopen(f"{OLLAMA_BASE}/api/tags", timeout=3) as r:
            data   = json.loads(r.read())
            _ollama_ok = True
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        _ollama_ok = False
        return []


def _ollama_generate(model: str, prompt: str, timeout: int = 60) -> str:
    """
    Call Ollama /api/generate with streaming disabled.
    Returns the response text or raises RuntimeError.
    """
    import urllib.request, json, urllib.error
    payload = json.dumps({
        "model" : model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "num_predict": 512,
        },
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{OLLAMA_BASE}/api/generate",
        data    = payload,
        method  = "POST",
        headers = {"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
            return data.get("response", "").strip()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama unreachable: {exc}") from exc


def _pick_model(preferred: list[str], override: Optional[str] = None) -> Optional[str]:
    """
    Select the best available model.
    1. If override is provided and matches something in Ollama, use it.
    2. Walk preferred list and return first that is installed.
    3. Return first available Ollama model as last resort.
    4. Return None if Ollama is empty.
    """
    available = list_ollama_models()
    if not available:
        return None

    if override and any(override in m or m in override for m in available):
        # fuzzy match override against available names
        for m in available:
            if override.lower().replace(" ", "") in m.lower().replace(" ", ""):
                return m
        return override   # pass verbatim; Ollama may still know it

    for pref in preferred:
        for m in available:
            if pref.lower().replace(" ", "") in m.lower().replace(" ", ""):
                return m

    # fall back to first available
    return available[0]


# ─────────────────────────────────────────────────────────────────────────────
# Translate
# ─────────────────────────────────────────────────────────────────────────────

def translate(
    text       : str,
    target_lang: str,
    model      : Optional[str] = None,
) -> str:
    """
    Translate *text* into *target_lang* using a local Ollama model.

    Falls back to deep_translator if Ollama is unavailable.

    Parameters
    ----------
    text        : Text to translate.
    target_lang : Target language name or code, e.g. "french", "fr", "Spanish".
    model       : Specific model name override (None = auto-pick from preferred list).

    Returns
    -------
    Translated string, or original text if all backends fail.
    """
    if not text or not text.strip():
        return text

    # ── Try Ollama first ──────────────────────────────────────────────────────
    if is_ollama_running():
        chosen = _pick_model(PREFERRED_TRANSLATE_MODELS, model)
        if chosen:
            prompt = (
                f"Translate the following text to {target_lang}. "
                f"Output ONLY the translated text, nothing else.\n\n"
                f"Text: {text}\n\nTranslation:"
            )
            try:
                result = _ollama_generate(chosen, prompt, timeout=45)
                if result:
                    return result
            except Exception as exc:
                print(f"[LLM Translate]: {chosen} failed — {exc}", file=sys.stderr)

    # ── Fallback: deep_translator ─────────────────────────────────────────────
    try:
        from deep_translator import GoogleTranslator
        result = GoogleTranslator(source="auto", target=target_lang).translate(text)
        return result or text
    except ImportError:
        pass
    except Exception as exc:
        print(f"[Translate Fallback]: deep_translator failed — {exc}", file=sys.stderr)

    return text   # last resort: return original


# ─────────────────────────────────────────────────────────────────────────────
# Summarize
# ─────────────────────────────────────────────────────────────────────────────

def summarize_llm(
    text    : str,
    model   : Optional[str] = None,
    ratio   : float = 0.33,
) -> str:
    """
    Summarize *text* using a local Ollama model.

    Falls back to spoaken_summarize (extractive) if Ollama is unavailable.

    Parameters
    ----------
    text  : Source transcript text.
    model : Specific model name override.
    ratio : Summary ratio for extractive fallback.

    Returns
    -------
    Summary string.
    """
    if not text or not text.strip():
        return "No text to summarise."

    # ── Try Ollama ────────────────────────────────────────────────────────────
    if is_ollama_running():
        chosen = _pick_model(PREFERRED_SUMMARIZE_MODELS, model)
        if chosen:
            word_count = len(text.split())
            target_words = max(50, int(word_count * ratio))
            prompt = (
                f"Summarize the following transcript in approximately {target_words} words. "
                f"Be concise and capture the key points. "
                f"Output ONLY the summary, no preamble.\n\n"
                f"Transcript:\n{text}\n\nSummary:"
            )
            try:
                result = _ollama_generate(chosen, prompt, timeout=60)
                if result:
                    return result
            except Exception as exc:
                print(f"[LLM Summarize]: {chosen} failed — {exc}", file=sys.stderr)

    # ── Fallback: extractive ──────────────────────────────────────────────────
    try:
        from spoaken_summarize import summarize as _extractive
        return _extractive(text, ratio=ratio)
    except Exception as exc:
        print(f"[Summarize Fallback]: extractive failed — {exc}", file=sys.stderr)

    # ── Last resort: simple truncation ────────────────────────────────────────
    words = text.split()
    keep  = max(20, int(len(words) * ratio))
    return " ".join(words[:keep]) + " …"


# ─────────────────────────────────────────────────────────────────────────────
# Install helper  (called from update window or on first use)
# ─────────────────────────────────────────────────────────────────────────────

def ensure_ollama_pkg(log_fn=print) -> bool:
    """
    Ensure the 'ollama' Python package is installed.
    Returns True if available after the call.
    """
    try:
        import ollama   # noqa: F401
        return True
    except ImportError:
        pass

    log_fn("[LLM]: 'ollama' package not found — installing …")
    import subprocess, shutil
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "ollama"]
    if shutil.which("apt"):
        cmd.append("--break-system-packages")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            log_fn("[LLM]: ollama package installed ✔")
            return True
        else:
            log_fn(f"[LLM]: pip install ollama failed — {result.stderr.strip()}")
            return False
    except Exception as exc:
        log_fn(f"[LLM]: install failed — {exc}")
        return False


def ensure_summarize_pkgs(log_fn=print) -> dict[str, bool]:
    """
    Check and optionally install summarization dependencies.
    Returns dict of {package: installed_bool}.
    """
    import importlib.util, subprocess, shutil

    pkgs = {
        "sumy"          : "sumy",
        "nltk"          : "nltk",
        "scikit-learn"  : "sklearn",
    }
    status = {}
    for pip_name, import_name in pkgs.items():
        if importlib.util.find_spec(import_name.split(".")[0]):
            status[pip_name] = True
        else:
            log_fn(f"[Summarize]: installing {pip_name} …")
            cmd = [sys.executable, "-m", "pip", "install", "--quiet", pip_name]
            if shutil.which("apt"):
                cmd.append("--break-system-packages")
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                status[pip_name] = r.returncode == 0
                if r.returncode == 0:
                    log_fn(f"[Summarize]: {pip_name} installed ✔")
                else:
                    log_fn(f"[Summarize]: {pip_name} failed — {r.stderr.strip()}")
            except Exception as exc:
                log_fn(f"[Summarize]: {pip_name} error — {exc}")
                status[pip_name] = False

    # Download NLTK punkt tokenizer if nltk was just installed
    if status.get("nltk"):
        try:
            import nltk
            nltk.download("punkt", quiet=True)
            nltk.download("punkt_tab", quiet=True)
            nltk.download("stopwords", quiet=True)
        except Exception:
            pass

    return status
    
    
    
    
    
    
    
    
    
    
    
    
