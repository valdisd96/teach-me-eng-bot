"""Google Translate wrapper via deep-translator.

Kept thin and dependency-injectable so the rest of the code can stay out of
HTTP-client territory and tests can substitute a fake `translate_fn`.
Translation is intentionally offloaded to Google — Gemma's quality on short
dictionary-style translations (especially into Russian) is not good enough,
and this command should feel instantaneous.

Only two primitives are exposed:

  * `normalize_target(s)` — accept a language code ('ru') or English name
    ('russian'); return the ISO 639-1 code. Pure, no network.
  * `translate(text, target)` — call Google Translate via `deep_translator`.
    Network call; wrap in `asyncio.to_thread` from async contexts.

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


def translate(text: str, target: str) -> str:
    """Google-translate `text` into `target` (ISO 639-1). Source auto-detected."""
    from deep_translator import GoogleTranslator

    return GoogleTranslator(source="auto", target=target).translate(text)
