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


def format_reverse_note(word_count: int, added: bool, max_words: int = 5) -> str:
    """One-line status shown under the English reply of a reverse-translate.

    * word_count > max_words → 'not added (N words)'
    * within limit + newly added → 'added to vocab ✅'
    * within limit + already there → 'already in vocab'
    """
    if word_count > max_words:
        return f"not added ({word_count} words)"
    return "added to vocab ✅" if added else "already in vocab"
