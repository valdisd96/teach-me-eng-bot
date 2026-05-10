"""Google Translate wrapper via deep-translator.

Kept thin and dependency-injectable so the rest of the code can stay out of
HTTP-client territory and tests can substitute a fake `translate_fn`.
Translation is intentionally offloaded to Google — Gemma's quality on short
dictionary-style translations (especially into Russian) is not good enough,
and this command should feel instantaneous.

Exposed primitives:

  * `normalize_target(s)` — accept a language code ('ru') or English name
    ('russian'); return the ISO 639-1 code. Pure, no network.
  * `translate(text, target, source='auto')` — call Google Translate via
    `deep_translator`. Network call; wrap in `asyncio.to_thread` from async
    contexts.
  * `is_target_script(text, target)` — True if `text` is majority-written in
    the script of `target`. Used to detect reverse-translate intent (e.g.
    user typed Russian while target='ru' means they want target→English).
    Only non-Latin targets can ever return True; Latin-script targets
    (de/fr/es/…) always return False because script alone can't distinguish
    them from English.
  * `vocab_target(source, translated, reverse)` — which half of the pair is
    the English one, and therefore the one that belongs in the learner's
    vocab list.
  * `format_vocab_note(word_count, added, max_words=5)` — the 1-line status
    (`added to vocab ✅` / `already in vocab` / `not added (N words)`) shown
    beneath a /tr reply when the 5-word cap is exceeded.
  * `PendingVocab` — token↔word registry for the 'Add to vocab' button under
    a /tr reply, sidestepping Telegram's 64-byte callback_data limit.

`_name_to_code()` is cached because `deep_translator`'s language list is a
static dict (no network), so we only build it once per process.
"""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def _name_to_code() -> dict[str, str]:
    """Return {'english': 'en', 'russian': 'ru', …} — the full supported map."""
    from deep_translator import GoogleTranslator

    return GoogleTranslator().get_supported_languages(as_dict=True)


def normalize_target(s: str) -> str:
    """Return the ISO code for a language given as a name or code.

    Case-insensitive. Raises ValueError on empty input or unknown language.
    """
    raw = s.strip().lower()
    if not raw:
        raise ValueError("Language cannot be empty.")
    mapping = _name_to_code()
    if raw in mapping:  # full name, e.g. "russian"
        return mapping[raw]
    if raw in mapping.values():  # already an ISO code, e.g. "ru"
        return raw
    raise ValueError(
        f"Unknown language {raw!r}. Try a code like 'ru' or a name like 'russian'."
    )


def translate(text: str, target: str, source: str = "auto") -> str:
    """Google-translate `text` into `target` (ISO 639-1).

    `source` defaults to 'auto' (Google sniffs the source language). Pass an
    explicit ISO code to force direction — used by the reverse-translate
    flow, where we want target→'en' regardless of what auto-detect guesses.
    """
    from deep_translator import GoogleTranslator

    return GoogleTranslator(source=source, target=target).translate(text)


# Unicode script ranges we care about. Kept as inline ranges rather than
# pulling in `unicodedata.script` (3.12+) or `regex` — this handles every
# non-Latin target we support and is trivially testable.
_RANGES: list[tuple[int, int, str]] = [
    (0x0400, 0x052F, "Cyrillic"),
    (0x0370, 0x03FF, "Greek"),
    (0x1F00, 0x1FFF, "Greek"),
    (0x0590, 0x05FF, "Hebrew"),
    (0x0600, 0x06FF, "Arabic"),
    (0x0750, 0x077F, "Arabic"),
    (0x0E00, 0x0E7F, "Thai"),
    (0x3040, 0x309F, "Japanese"),
    (0x30A0, 0x30FF, "Japanese"),
    (0x4E00, 0x9FFF, "CJK"),
    (0x3400, 0x4DBF, "CJK"),
    (0xAC00, 0xD7AF, "Hangul"),
    (0x0900, 0x097F, "Devanagari"),
    (0x0980, 0x09FF, "Bengali"),
    (0x0B80, 0x0BFF, "Tamil"),
    (0x0C00, 0x0C7F, "Telugu"),
    (0x0530, 0x058F, "Armenian"),
    (0x10A0, 0x10FF, "Georgian"),
    (0x2D00, 0x2D2F, "Georgian"),
    (0x1780, 0x17FF, "Khmer"),
    (0x0E80, 0x0EFF, "Lao"),
    (0x1000, 0x109F, "Myanmar"),
    (0x0D80, 0x0DFF, "Sinhala"),
    (0x0041, 0x007A, "Latin"),
    (0x00C0, 0x024F, "Latin"),
]

# Which scripts the text should be in for a given target language. Only
# non-Latin targets are listed — Latin targets can't be disambiguated from
# English by script alone, so we never flip to reverse for them.
_TARGET_SCRIPTS: dict[str, set[str]] = {
    "ru": {"Cyrillic"},
    "uk": {"Cyrillic"},
    "be": {"Cyrillic"},
    "bg": {"Cyrillic"},
    "mk": {"Cyrillic"},
    "sr": {"Cyrillic"},
    "el": {"Greek"},
    "he": {"Hebrew"},
    "iw": {"Hebrew"},
    "ar": {"Arabic"},
    "fa": {"Arabic"},
    "ur": {"Arabic"},
    "th": {"Thai"},
    "ja": {"Japanese", "CJK"},
    "zh-CN": {"CJK"},
    "zh-TW": {"CJK"},
    "ko": {"Hangul"},
    "hi": {"Devanagari"},
    "bn": {"Bengali"},
    "ta": {"Tamil"},
    "te": {"Telugu"},
    "hy": {"Armenian"},
    "ka": {"Georgian"},
    "km": {"Khmer"},
    "lo": {"Lao"},
    "my": {"Myanmar"},
    "si": {"Sinhala"},
}


def _char_script(ch: str) -> str | None:
    code = ord(ch)
    for lo, hi, name in _RANGES:
        if lo <= code <= hi:
            return name
    return None


def is_target_script(text: str, target: str) -> bool:
    """Return True if `text` is majority-written in the script of `target`.

    Non-letter characters (digits, punctuation, whitespace) are ignored in
    the count. Latin-script targets always return False.
    """
    expected = _TARGET_SCRIPTS.get(target)
    if not expected:
        return False
    in_target = 0
    in_other = 0
    for ch in text:
        script = _char_script(ch)
        if script is None:
            continue
        if script in expected:
            in_target += 1
        else:
            in_other += 1
    return in_target > 0 and in_target > in_other


def format_vocab_note(word_count: int, added: bool, max_words: int = 5) -> str:
    """One-line status shown under the reply of a /tr vocab add.

    Used for both directions: reverse (target→English, adds the translation)
    and forward (English→target, adds the source). The rules are identical:

    * word_count > max_words → 'not added (N words)'
    * within limit + newly added → 'added to vocab ✅'
    * within limit + already there → 'already in vocab'
    """
    if word_count > max_words:
        return f"not added ({word_count} words)"
    return "added to vocab ✅" if added else "already in vocab"


def vocab_target(source_text: str, translated: str, reverse: bool) -> str:
    """Return the English side of a /tr pair — the one that lands in vocab.

    Forward flow (English→target) adds the source. Reverse flow (target→English)
    adds the translation. Either way, the learner's English vocab is what grows.
    """
    return translated if reverse else source_text


class PendingVocab:
    """Short-token → (chat_id, word) registry for /tr's 'Add to vocab' button.

    Telegram caps `callback_data` at 64 bytes, so we can't stuff arbitrary UTF-8
    vocab phrases in there. Each /tr reply registers its candidate word
    and stamps the button with the resulting integer token; tapping the button
    pops the token and yields the word for a DB write.

    In-memory only: a bot restart orphans outstanding buttons, which taps
    surface as 'expired'. Acceptable — the user can just /tr again.

    The `max_size` cap evicts the oldest entry whenever the dict grows past it,
    so a long-running process can't leak memory from never-tapped buttons.
    """

    def __init__(self, max_size: int = 1000) -> None:
        self._store: dict[int, tuple[int, str]] = {}
        self._seq = 0
        self._max = max_size

    def register(self, chat_id: int, word: str) -> int:
        self._seq += 1
        token = self._seq
        self._store[token] = (chat_id, word)
        while len(self._store) > self._max:
            # Python dicts preserve insertion order, so the first key is oldest.
            self._store.pop(next(iter(self._store)))
        return token

    def pop(self, token: int) -> tuple[int, str] | None:
        return self._store.pop(token, None)

    def __len__(self) -> int:
        return len(self._store)
