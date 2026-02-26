"""
spoaken_summarize.py
────────────────────
Extractive text summariser for Spoaken.

Uses sentence-scoring (TF-IDF style) with no heavy dependencies.
If transformers/pipeline is available, an optional abstractive path
can be taken, but the extractive fallback is always used otherwise.

Public API
──────────
  summarize(text, ratio=0.3, max_sentences=10) -> str
"""

import re
from collections import Counter

# ── Stopword list (English) ───────────────────────────────────────────────────
_STOPWORDS = frozenset({
    "a","about","above","after","again","against","all","am","an","and","any",
    "are","as","at","be","because","been","before","being","below","between",
    "both","but","by","could","did","do","does","doing","down","during","each",
    "few","for","from","further","get","got","had","has","have","having","he",
    "her","here","him","himself","his","how","i","if","in","into","is","it",
    "its","itself","just","me","more","most","my","myself","no","nor","not","of",
    "off","on","once","only","or","other","our","out","over","own","same","she",
    "should","so","some","such","than","that","the","their","them","then","there",
    "these","they","this","those","through","to","too","under","until","up","us",
    "very","was","we","were","what","when","where","which","while","who","whom",
    "why","will","with","would","you","your",
})


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _split_sentences(text: str) -> list[str]:
    """Split text into sentences using basic punctuation heuristics."""
    # Handle common abbreviations that contain dots
    cleaned = re.sub(r"\b(Mr|Mrs|Ms|Dr|Prof|Sr|Jr|vs|etc|i\.e|e\.g)\.", r"\1<DOT>", text)
    parts   = re.split(r"(?<=[.!?])\s+(?=[A-Z\"'\(])", cleaned)
    return [p.replace("<DOT>", ".").strip() for p in parts if p.strip()]


def _word_tokens(text: str) -> list[str]:
    return [w.lower() for w in re.findall(r"\b[a-z]{2,}\b", text.lower())]


def _term_frequencies(words: list[str]) -> dict[str, float]:
    """Normalised term frequency, ignoring stopwords."""
    content = [w for w in words if w not in _STOPWORDS]
    if not content:
        return {}
    counts  = Counter(content)
    top     = counts.most_common(1)[0][1]
    return {word: count / top for word, count in counts.items()}


def _score_sentence(sentence: str, tf: dict[str, float]) -> float:
    """Average TF score of content words in a sentence."""
    tokens = _word_tokens(sentence)
    scored = [tf.get(t, 0.0) for t in tokens if t not in _STOPWORDS]
    return sum(scored) / len(scored) if scored else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Core summariser
# ─────────────────────────────────────────────────────────────────────────────

def summarize_extractive(
    text        : str,
    ratio       : float = 0.33,
    max_sentences: int  = 12,
) -> str:
    """
    Extractive summarisation via sentence TF scoring with positional weighting.

    Spoken transcripts tend to open with the topic statement and close with
    a conclusion, so the first and last sentences get a mild score boost.

    Parameters
    ----------
    text           : Raw transcript text.
    ratio          : Fraction of sentences to keep (0 < ratio ≤ 1).
    max_sentences  : Hard cap on sentences returned.

    Returns
    -------
    Summary string, or the original text if it is already short.
    """
    if not text or not text.strip():
        return "No text to summarise."

    sentences = _split_sentences(text)
    n         = len(sentences)

    if n <= 3:
        return text.strip()

    tf = _term_frequencies(_word_tokens(text))

    # Positional bonus: first sentence (topic), second (expansion), last (wrap-up)
    last_idx = n - 1
    _pos_weight = {0: 1.20, 1: 1.05, last_idx: 1.10}

    # Score every sentence and apply positional weight
    scored = [
        (i, s, _score_sentence(s, tf) * _pos_weight.get(i, 1.0))
        for i, s in enumerate(sentences)
    ]

    # Number of sentences to keep
    num_sentences_to_keep = max(1, min(max_sentences, round(n * ratio)))

    # Select top-k by score, restore original order
    top_indices = sorted(
        sorted(range(len(scored)), key=lambda i: scored[i][2], reverse=True)[:num_sentences_to_keep]
    )

    summary = " ".join(sentences[i] for i in top_indices)
    return summary.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def summarize(text: str, ratio: float = 0.33, max_sentences: int = 12) -> str:
    """
    Summarise transcript text.

    Falls back cleanly to extractive summarisation if no neural model is
    available; designed to run without GPU or internet access.
    """
    return summarize_extractive(text, ratio=ratio, max_sentences=max_sentences)
    
    
    
    
    
    
    
    
