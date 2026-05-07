"""Tests for issue #83 — `/label`, `/unlabel`, `/labels` Telegram commands.

Spec lives in the latest `<!-- agent-plan v1 -->` comment on the issue. The
public surface is:
- `vocab.parse_label_spec` and `vocab.find_word_id` — covered in test_vocab.py.
- `bot.cmd_label`, `bot.cmd_unlabel`, `bot.cmd_labels` — the user-facing
  command handlers covered here.

Telegram Update/Context is mocked at the seam (AsyncMock + MagicMock); the
project's other handler tests (test_play_game_button.py) follow the same shape.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import bot  # noqa: E402  (env var must be set first)
import vocab  # noqa: E402


CHAT = 7777


# -- helpers -----------------------------------------------------------------


def _make_command_update(chat_id: int = CHAT) -> MagicMock:
    """Build a MagicMock Update with an async message.reply_text."""
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = 1
    update.effective_user.is_bot = False
    update.message.reply_text = AsyncMock()
    return update


def _make_context(args: list[str]) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args
    return ctx


def _patch_conn(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection) -> None:
    monkeypatch.setattr(bot, "conn", conn)


def _all_replies(update: MagicMock) -> list[str]:
    """Return the positional/keyword text of every reply_text call on this update."""
    out: list[str] = []
    for call in update.message.reply_text.call_args_list:
        if call.args:
            out.append(call.args[0])
        elif "text" in call.kwargs:
            out.append(call.kwargs["text"])
    return out


def _last_reply(update: MagicMock) -> str:
    replies = _all_replies(update)
    assert replies, f"expected at least one reply_text call, got none on {update}"
    return replies[-1]


def _word_id(conn: sqlite3.Connection, chat_id: int, text: str) -> int:
    return conn.execute(
        "SELECT id FROM words WHERE chat_id = ? AND text = ?", (chat_id, text)
    ).fetchone()["id"]


def _word_label_count(conn: sqlite3.Connection, word_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM word_labels WHERE word_id = ?", (word_id,)
    ).fetchone()["n"]


# === cmd_label ==============================================================


def test_cmd_label_attaches_each_spec_token(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC1 — every spec token is attached to the named word
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    update = _make_command_update()
    ctx = _make_context(["horse", "pos:noun", "type:animal"])

    asyncio.run(bot.cmd_label(update, ctx))

    wid = _word_id(conn, CHAT, "horse")
    assert vocab.labels_for_word(conn, wid) == ["pos:noun", "type:animal"], (
        f"both spec tokens must end up attached; got {vocab.labels_for_word(conn, wid)!r}"
    )


def test_cmd_label_idempotent_re_run_no_extra_rows(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC1 — re-applying same spec inserts no extra rows
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")

    asyncio.run(bot.cmd_label(_make_command_update(), _make_context(
        ["horse", "pos:noun", "type:animal"]
    )))
    wid = _word_id(conn, CHAT, "horse")
    first_count = _word_label_count(conn, wid)

    # Same spec, second run.
    update = _make_command_update()
    asyncio.run(bot.cmd_label(update, _make_context(
        ["horse", "pos:noun", "type:animal"]
    )))

    assert _word_label_count(conn, wid) == first_count, (
        f"second /label call must not insert extra rows; "
        f"before={first_count}, after={_word_label_count(conn, wid)}"
    )
    # And the reply must still be a non-empty success — not an error.
    reply = _last_reply(update)
    assert "⚠️" not in reply and not reply.startswith("Not found"), (
        f"idempotent re-run should still succeed; got reply {reply!r}"
    )


def test_cmd_label_unknown_word_replies_not_found(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC6 — unknown word → "Not found: <normalized>"
    _patch_conn(monkeypatch, conn)
    vocab.ensure_chat(conn, CHAT)
    update = _make_command_update()

    asyncio.run(bot.cmd_label(update, _make_context(["Ghost", "pos:noun"])))

    reply = _last_reply(update)
    assert reply == "Not found: ghost", (
        f'expected exact "Not found: ghost" reply (normalized); got {reply!r}'
    )


def test_cmd_label_word_lookup_is_case_insensitive(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC6 — /remove-style normalize: case-insensitive lookup
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")

    update = _make_command_update()
    asyncio.run(bot.cmd_label(update, _make_context(
        ["HORSE", "type:animal"]
    )))

    wid = _word_id(conn, CHAT, "horse")
    assert vocab.labels_for_word(conn, wid) == ["type:animal"], (
        f"upper-case word arg must resolve to the same row; "
        f"got labels {vocab.labels_for_word(conn, wid)!r}"
    )


def test_cmd_label_malformed_spec_replies_warning(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC4 — bot catches ValueError and replies "⚠️ <message>"
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")

    update = _make_command_update()
    asyncio.run(bot.cmd_label(update, _make_context(["horse", ":foo"])))

    reply = _last_reply(update)
    assert reply.startswith("⚠️ "), (
        f"malformed spec must yield a ⚠️-prefixed reply; got {reply!r}"
    )
    assert "malformed label spec" in reply, (
        f'reply must include "malformed label spec" from the parser; got {reply!r}'
    )
    assert ":foo" in reply, f"reply must echo the offending token; got {reply!r}"


def test_cmd_label_pos_swap_announces_replaced(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC5 + example — replaced pos:noun → pos:verb
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    wid = _word_id(conn, CHAT, "horse")
    # Pre-attach the old pos:noun.
    vocab.attach_label(conn, wid, vocab.get_or_create_label(conn, CHAT, "pos:noun"))

    update = _make_command_update()
    asyncio.run(bot.cmd_label(update, _make_context(["horse", "pos:verb"])))

    reply = _last_reply(update)
    assert "replaced pos:noun → pos:verb" in reply, (
        f'reply must name both old and new pos:* (AC5 example); got {reply!r}'
    )
    # And only pos:verb survives.
    assert vocab.labels_for_word(conn, wid) == ["pos:verb"], (
        f"only the new pos:* should remain; got {vocab.labels_for_word(conn, wid)!r}"
    )


def test_cmd_label_pos_swap_plus_other_additions(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC5 — replaced + added: ... in the same call
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    wid = _word_id(conn, CHAT, "horse")
    vocab.attach_label(conn, wid, vocab.get_or_create_label(conn, CHAT, "pos:noun"))

    update = _make_command_update()
    asyncio.run(bot.cmd_label(update, _make_context(
        ["horse", "pos:verb", "type:animal"]
    )))

    reply = _last_reply(update)
    assert "replaced pos:noun → pos:verb" in reply, (
        f"swap message must appear; got {reply!r}"
    )
    assert "added" in reply and "type:animal" in reply, (
        f'pure additions must surface as "added: ..." in the reply; got {reply!r}'
    )


def test_cmd_label_missing_args_replies_usage(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — missing args → Usage line per spec
    _patch_conn(monkeypatch, conn)
    vocab.ensure_chat(conn, CHAT)

    # No args at all.
    update = _make_command_update()
    asyncio.run(bot.cmd_label(update, _make_context([])))
    assert _last_reply(update) == "Usage: /label <word> <spec> [<spec> ...]"

    # Only the word — still missing a spec.
    update2 = _make_command_update()
    asyncio.run(bot.cmd_label(update2, _make_context(["horse"])))
    assert _last_reply(update2) == "Usage: /label <word> <spec> [<spec> ...]"


def test_cmd_label_dedupes_duplicate_spec_tokens(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — duplicate spec tokens dedupe before attach
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")

    update = _make_command_update()
    asyncio.run(bot.cmd_label(update, _make_context(
        ["horse", "type:animal", "Type:animal", "  type:animal  "]
    )))

    wid = _word_id(conn, CHAT, "horse")
    assert vocab.labels_for_word(conn, wid) == ["type:animal"], (
        f"duplicates must collapse before attach; got {vocab.labels_for_word(conn, wid)!r}"
    )
    assert _word_label_count(conn, wid) == 1, (
        f"only one row should exist; got {_word_label_count(conn, wid)}"
    )


def test_cmd_label_mixed_valid_malformed_rejects_all(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — mixed valid+malformed → reject all (no partial writes)
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")

    update = _make_command_update()
    asyncio.run(bot.cmd_label(update, _make_context(
        ["horse", "type:animal", ":foo"]
    )))

    wid = _word_id(conn, CHAT, "horse")
    assert vocab.labels_for_word(conn, wid) == [], (
        "no tokens should be attached when one is malformed (no partial writes); "
        f"got {vocab.labels_for_word(conn, wid)!r}"
    )
    # No labels row should have been created either.
    rows = conn.execute(
        "SELECT name FROM labels WHERE chat_id = ?", (CHAT,)
    ).fetchall()
    assert rows == [], f"no labels row should be inserted; got {[r['name'] for r in rows]!r}"


def test_cmd_label_two_pos_in_one_call_keeps_last(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge example — /label horse pos:noun pos:verb → only pos:verb
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")

    update = _make_command_update()
    asyncio.run(bot.cmd_label(update, _make_context(
        ["horse", "pos:noun", "pos:verb"]
    )))

    wid = _word_id(conn, CHAT, "horse")
    assert vocab.labels_for_word(conn, wid) == ["pos:verb"], (
        f"second pos:* in the same call must replace the first; "
        f"got {vocab.labels_for_word(conn, wid)!r}"
    )


def test_cmd_label_word_arg_unicode_and_whitespace_normalized(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — unicode word + leading/trailing whitespace stripped on lookup
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "café")

    update = _make_command_update()
    asyncio.run(bot.cmd_label(update, _make_context(
        ["  Café  ", "type:place"]
    )))

    wid = _word_id(conn, CHAT, "café")
    assert vocab.labels_for_word(conn, wid) == ["type:place"], (
        f"unicode word arg with surrounding whitespace must resolve; "
        f"got labels {vocab.labels_for_word(conn, wid)!r}"
    )
    reply = _last_reply(update)
    assert not reply.startswith("Not found"), (
        f"normalised lookup should succeed, not 404; got {reply!r}"
    )


# === cmd_unlabel ============================================================


def test_cmd_unlabel_detaches_each_named_label(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — /unlabel detaches each named label
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    wid = _word_id(conn, CHAT, "horse")
    for n in ("pos:noun", "type:animal", "category:fauna"):
        vocab.attach_label(conn, wid, vocab.get_or_create_label(conn, CHAT, n))

    update = _make_command_update()
    asyncio.run(bot.cmd_unlabel(update, _make_context(
        ["horse", "pos:noun", "type:animal"]
    )))

    assert vocab.labels_for_word(conn, wid) == ["category:fauna"], (
        f"named labels must be detached; got {vocab.labels_for_word(conn, wid)!r}"
    )


def test_cmd_unlabel_silently_skips_absent(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — absent or unattached → silent skip (idempotent no-op)
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    wid = _word_id(conn, CHAT, "horse")
    vocab.attach_label(conn, wid, vocab.get_or_create_label(conn, CHAT, "pos:noun"))

    update = _make_command_update()
    asyncio.run(bot.cmd_unlabel(update, _make_context(
        ["horse", "pos:noun", "ghost:label"]
    )))

    # The "real" detach succeeds, the absent token is silently ignored — no raise.
    assert vocab.labels_for_word(conn, wid) == [], (
        f"existing pos:noun should still be detached; got {vocab.labels_for_word(conn, wid)!r}"
    )
    # No "ghost:label" labels row should have been created either (silent skip ≠ get_or_create).
    rows = [r["name"] for r in conn.execute(
        "SELECT name FROM labels WHERE chat_id = ?", (CHAT,)
    ).fetchall()]
    assert "ghost:label" not in rows, (
        f"absent label must not be auto-created during unlabel; got labels {rows!r}"
    )


def test_cmd_unlabel_reports_nothing_to_remove(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — when nothing is detached → "nothing to remove"
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")

    update = _make_command_update()
    asyncio.run(bot.cmd_unlabel(update, _make_context(
        ["horse", "ghost:one", "ghost:two"]
    )))

    reply = _last_reply(update)
    assert "nothing to remove" in reply.lower(), (
        f'reply must say "nothing to remove" when no detach happens; got {reply!r}'
    )


def test_cmd_unlabel_reports_detached_names(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — reply names the labels actually detached
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    wid = _word_id(conn, CHAT, "horse")
    vocab.attach_label(conn, wid, vocab.get_or_create_label(conn, CHAT, "pos:noun"))
    vocab.attach_label(conn, wid, vocab.get_or_create_label(conn, CHAT, "type:animal"))

    update = _make_command_update()
    asyncio.run(bot.cmd_unlabel(update, _make_context(
        ["horse", "pos:noun", "type:animal", "ghost:absent"]
    )))

    reply = _last_reply(update)
    assert "pos:noun" in reply, f'reply must name "pos:noun"; got {reply!r}'
    assert "type:animal" in reply, f'reply must name "type:animal"; got {reply!r}'
    assert "ghost:absent" not in reply, (
        f"absent labels must not be reported as detached; got {reply!r}"
    )


def test_cmd_unlabel_unknown_word_replies_not_found(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC6 — unknown word → "Not found: <normalized>"
    _patch_conn(monkeypatch, conn)
    vocab.ensure_chat(conn, CHAT)

    update = _make_command_update()
    asyncio.run(bot.cmd_unlabel(update, _make_context(["GHOST", "pos:noun"])))

    assert _last_reply(update) == "Not found: ghost", (
        f"expected normalized 404 reply; got {_last_reply(update)!r}"
    )


def test_cmd_unlabel_missing_args_replies_usage(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — same Usage shape for /unlabel
    _patch_conn(monkeypatch, conn)
    vocab.ensure_chat(conn, CHAT)

    update = _make_command_update()
    asyncio.run(bot.cmd_unlabel(update, _make_context([])))
    assert _last_reply(update) == "Usage: /unlabel <word> <spec> [<spec> ...]", (
        f"got {_last_reply(update)!r}"
    )

    update2 = _make_command_update()
    asyncio.run(bot.cmd_unlabel(update2, _make_context(["horse"])))
    assert _last_reply(update2) == "Usage: /unlabel <word> <spec> [<spec> ...]"


def test_cmd_unlabel_malformed_spec_replies_warning(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC4 — malformed spec → "⚠️ <message>"
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")

    update = _make_command_update()
    asyncio.run(bot.cmd_unlabel(update, _make_context(["horse", "a:b:c"])))

    reply = _last_reply(update)
    assert reply.startswith("⚠️ "), f"⚠️-prefixed reply expected; got {reply!r}"
    assert "malformed label spec" in reply, (
        f'reply must include "malformed label spec"; got {reply!r}'
    )


# === cmd_labels =============================================================


def test_cmd_labels_lists_name_count_alphabetically(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3 — "name (count)" lines, alphabetically ascending
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    vocab.add_word(conn, CHAT, "ibuprofen")
    wid_h = _word_id(conn, CHAT, "horse")
    wid_i = _word_id(conn, CHAT, "ibuprofen")

    pos_noun = vocab.get_or_create_label(conn, CHAT, "pos:noun")
    type_med = vocab.get_or_create_label(conn, CHAT, "type:medicine")
    cat_fruit = vocab.get_or_create_label(conn, CHAT, "category:fruit")

    vocab.attach_label(conn, wid_h, pos_noun)
    vocab.attach_label(conn, wid_i, pos_noun)
    vocab.attach_label(conn, wid_i, type_med)
    # category:fruit exists but is unattached.
    _ = cat_fruit

    update = _make_command_update()
    asyncio.run(bot.cmd_labels(update, _make_context([])))

    reply = _last_reply(update)
    # Lines mention each label with its count and ascend by name.
    assert "category:fruit" in reply, f"missing category:fruit; got {reply!r}"
    assert "pos:noun" in reply, f"missing pos:noun; got {reply!r}"
    assert "type:medicine" in reply, f"missing type:medicine; got {reply!r}"
    # Alphabetic order: category:fruit < pos:noun < type:medicine
    pos_a = reply.index("category:fruit")
    pos_b = reply.index("pos:noun")
    pos_c = reply.index("type:medicine")
    assert pos_a < pos_b < pos_c, (
        f"labels must be listed alphabetically ascending; got order in {reply!r}"
    )
    # Counts: pos:noun → 2, type:medicine → 1, category:fruit → 0.
    assert "pos:noun (2)" in reply, f"pos:noun count must be 2; got {reply!r}"
    assert "type:medicine (1)" in reply, f"type:medicine count must be 1; got {reply!r}"


def test_cmd_labels_empty_chat_replies_no_labels(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3 — empty chat → "No labels yet."
    _patch_conn(monkeypatch, conn)
    vocab.ensure_chat(conn, CHAT)  # chat exists but no labels

    update = _make_command_update()
    asyncio.run(bot.cmd_labels(update, _make_context([])))

    reply = _last_reply(update)
    assert "No labels yet." in reply, (
        f'expected friendly "No labels yet." message; got {reply!r}'
    )


def test_cmd_labels_unattached_label_shows_zero(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — labels exist but none attached → still listed with "(0)"
    _patch_conn(monkeypatch, conn)
    vocab.ensure_chat(conn, CHAT)
    vocab.get_or_create_label(conn, CHAT, "type:medicine")

    update = _make_command_update()
    asyncio.run(bot.cmd_labels(update, _make_context([])))

    reply = _last_reply(update)
    assert "type:medicine (0)" in reply, (
        f"unattached label must surface with (0) count; got {reply!r}"
    )


def test_cmd_labels_scopes_to_chat(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3 — listing scopes to the requesting chat
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    vocab.add_word(conn, CHAT + 1, "horse")  # different chat
    wid_a = _word_id(conn, CHAT, "horse")
    wid_b = _word_id(conn, CHAT + 1, "horse")
    vocab.attach_label(conn, wid_a, vocab.get_or_create_label(conn, CHAT, "pos:noun"))
    vocab.attach_label(
        conn, wid_b, vocab.get_or_create_label(conn, CHAT + 1, "other:label")
    )

    update = _make_command_update(chat_id=CHAT)
    asyncio.run(bot.cmd_labels(update, _make_context([])))
    reply = _last_reply(update)

    assert "pos:noun" in reply, f"own-chat label missing; got {reply!r}"
    assert "other:label" not in reply, (
        f"other chat's label leaked into reply; got {reply!r}"
    )
