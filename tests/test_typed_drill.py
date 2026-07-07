"""Tests for typed_drill.py — the single typed-answer drill engine.

The two `/games` typed buttons (Repeat + Focus drill) were folded into ONE
"Typed drill" button (`gm:drill`). `Game.kind` is gone; each `Round` carries
`judged: bool` instead:

- `judged=False` — a focus-pool word (non-remembered, `/focus`-scoped):
  strict `grade_answer`, recorded `source="game"`.
- `judged=True` — a salted `remembered`-not-`mastered` word (a mastery
  check): tolerant `grade_answer_llm`, recorded `source="repeat"`, which
  drives the forget-flip / mastered machinery.

`draw_rounds(focus_rows, remembered_rows=(), *, n_max=MAX_ROUNDS,
n_salt=MAX_SALT, min_rounds=MIN_ROUNDS, rng=...)` filters both pools for
translatability, requires the FOCUS pool to keep `min_rounds` rows (salt
never rescues the minimum), plays overlap words as focus rounds, salts in up
to `n_salt` judged rounds, fills focus rounds to `min(n_max - salt, pool)`,
and shuffles the final order so mastery checks don't cluster at the end.

Spec-driven assertions are ported verbatim where semantics are unchanged
(grading, apply_answer, format_result, filtering, direction, the
`grade_answer_llm` YES/NO/error matrix from #143). Bot-side wiring — the
single `bot.typed_drills` map, the gm:drill start, stale gm:repeat/gm:focus
callbacks, the judged-keyed handle_message branch (LLM judge +
source="repeat" vs strict + source="game"), and the in-progress gate — is
covered at the bottom.
"""

from __future__ import annotations

import asyncio
import os
import random
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import bot  # noqa: E402  (env var must be set first)
import typed_drill as td  # noqa: E402
import vocab  # noqa: E402


# -- helpers -----------------------------------------------------------------


def _row(wid: int, text: str, translation: str | None) -> tuple[int, str, str | None]:
    return (wid, text, translation)


def _five_translatable_rows() -> list[tuple[int, str, str]]:
    return [
        _row(1, "cat", "кошка"),
        _row(2, "dog", "собака"),
        _row(3, "tree", "дерево"),
        _row(4, "apple", "яблоко"),
        _row(5, "house", "дом"),
    ]


def _salt_rows(n: int, start: int = 100) -> list[tuple[int, str, str]]:
    return [_row(start + i, f"salt{i}", f"соль{i}") for i in range(n)]


def _round(
    *,
    word_id: int = 1,
    prompt: str = "cat",
    expected: str = "кошка",
    direction: str = "en2ru",
    judged: bool = False,
) -> td.Round:
    # `direction` is informational per the spec; grading is direction-agnostic.
    return td.Round(
        word_id=word_id,
        prompt=prompt,
        expected=expected,
        direction=direction,
        judged=judged,
    )


def _fresh_game(n: int = 3) -> td.Game:
    rounds = [
        _round(word_id=i, prompt=f"w{i}", expected=f"t{i}") for i in range(1, n + 1)
    ]
    return td.Game(chat_id=42, rounds=rounds)


# -- module constants ---------------------------------------------------------


def test_module_constants_match_spec() -> None:
    # One merged game: pool floor 5, total cap 10, at most 2 salted mastery
    # checks, judge capped at 10 s.
    assert td.MIN_ROUNDS == 5, f"MIN_ROUNDS must equal 5; got {td.MIN_ROUNDS}"
    assert td.MAX_ROUNDS == 10, f"MAX_ROUNDS must equal 10; got {td.MAX_ROUNDS}"
    assert td.MAX_SALT == 2, f"MAX_SALT must equal 2; got {td.MAX_SALT}"
    assert td.JUDGE_TIMEOUT_S == 10, (
        f"JUDGE_TIMEOUT_S must equal 10; got {td.JUDGE_TIMEOUT_S}"
    )
    # The per-kind constants died with Game.kind.
    assert not hasattr(td, "REPEAT_ROUNDS"), (
        "REPEAT_ROUNDS must be gone — the merged drill has no repeat kind"
    )
    assert not hasattr(td, "MAX_FOCUS_ROUNDS"), (
        "MAX_FOCUS_ROUNDS must be gone — the merged drill has one MAX_ROUNDS cap"
    )


def test_round_judged_field_defaults_false() -> None:
    # Round carries the judged discriminator; the default is False (focus word).
    rd = td.Round(word_id=1, prompt="cat", expected="кошка", direction="en2ru")
    assert rd.judged is False, f"default judged must be False; got {rd.judged!r}"
    assert _round(judged=True).judged is True


# -- draw_rounds: sizing --------------------------------------------------------


def test_draw_rounds_focus_five_no_salt() -> None:  # minimal pool, no salt → 5 focus rounds
    rounds = td.draw_rounds(_five_translatable_rows(), rng=random.Random(0))
    assert len(rounds) == 5, f"expected 5 rounds, got {len(rounds)}"
    assert {r.word_id for r in rounds} == {1, 2, 3, 4, 5}
    assert all(r.judged is False for r in rounds), (
        f"no salt pool → every round must be judged=False; got "
        f"{[(r.word_id, r.judged) for r in rounds]}"
    )


def test_draw_rounds_large_focus_with_salt_caps_at_max() -> None:  # 20 focus + 5 salt → 10 total, exactly 2 judged
    focus = [_row(i, f"w{i}", f"t{i}") for i in range(1, 21)]  # 20 rows
    salt = _salt_rows(5)
    rounds = td.draw_rounds(focus, salt, rng=random.Random(0))
    assert len(rounds) == td.MAX_ROUNDS == 10, (
        f"total must cap at MAX_ROUNDS (10); got {len(rounds)}"
    )
    judged = [r for r in rounds if r.judged]
    assert len(judged) == td.MAX_SALT == 2, (
        f"exactly MAX_SALT (2) salted judged rounds expected; got {len(judged)}"
    )
    salt_ids = {r[0] for r in salt}
    assert {r.word_id for r in judged} <= salt_ids, (
        "judged rounds must come from the salt pool"
    )
    focus_ids = {r[0] for r in focus}
    assert {r.word_id for r in rounds if not r.judged} <= focus_ids, (
        "non-judged rounds must come from the focus pool"
    )


def test_draw_rounds_salt_added_on_top_of_min_pool() -> None:  # 5 focus + 1 salt → 6 rounds
    rounds = td.draw_rounds(
        _five_translatable_rows(), _salt_rows(1), rng=random.Random(0)
    )
    assert len(rounds) == 6, (
        f"5 focus + 1 salt must give 6 rounds (salt is on top of the pool "
        f"minimum); got {len(rounds)}"
    )
    assert {r.word_id for r in rounds if not r.judged} == {1, 2, 3, 4, 5}
    judged = [r for r in rounds if r.judged]
    assert len(judged) == 1 and judged[0].word_id == 100


def test_draw_rounds_salt_never_rescues_minimum() -> None:  # focus 4 + salt 10 → ValueError
    focus = _five_translatable_rows()[:4]
    with pytest.raises(ValueError):
        td.draw_rounds(focus, _salt_rows(10), rng=random.Random(0))


def test_draw_rounds_empty_pool_raises() -> None:  # edge: empty focus pool → ValueError
    with pytest.raises(ValueError):
        td.draw_rounds([], rng=random.Random(0))
    with pytest.raises(ValueError):
        td.draw_rounds([], _salt_rows(5), rng=random.Random(0))


def test_draw_rounds_n_salt_zero_yields_no_judged_rounds() -> None:  # n_salt=0 → salt ignored
    rounds = td.draw_rounds(
        _five_translatable_rows(), _salt_rows(3), n_salt=0, rng=random.Random(0)
    )
    assert len(rounds) == 5
    assert all(r.judged is False for r in rounds), (
        "n_salt=0 must produce zero judged rounds even with a non-empty salt pool"
    )


def test_draw_rounds_overlap_word_plays_once_as_focus() -> None:  # word in both pools → one judged=False round
    focus = _five_translatable_rows()
    # word_id 1 is in both pools; word_id 100 is salt-only.
    salt = [focus[0], _row(100, "salt0", "соль0")]
    rounds = td.draw_rounds(focus, salt, rng=random.Random(0))
    ids = [r.word_id for r in rounds]
    assert ids.count(1) == 1, (
        f"a word present in both pools must play exactly once; got ids={ids}"
    )
    overlap = [r for r in rounds if r.word_id == 1]
    assert overlap[0].judged is False, (
        "a word in both pools must play as a focus round (judged=False), "
        "not as a mastery check"
    )
    judged = [r for r in rounds if r.judged]
    assert [r.word_id for r in judged] == [100], (
        f"only the salt-only word may be judged; got "
        f"{[(r.word_id, r.judged) for r in rounds]}"
    )
    assert len(rounds) == 6  # 5 focus + 1 real salt


def test_draw_rounds_salt_fully_overlapping_yields_no_judged() -> None:  # salt ⊆ focus → no mastery checks
    focus = _five_translatable_rows()
    rounds = td.draw_rounds(focus, list(focus), rng=random.Random(0))
    assert len(rounds) == 5
    assert all(r.judged is False for r in rounds), (
        "a salt pool fully contained in the focus pool must add no judged rounds"
    )


def test_draw_rounds_shuffle_mixes_salt_into_the_middle() -> None:  # salted rounds must not always sit last
    # Salt rounds are appended after the focus rounds before the final
    # shuffle; with seed 0 (8 focus + 2 salt) the judged rounds land at
    # positions [3, 9] — i.e. NOT the final two slots. If the shuffle were
    # dropped, they would always occupy the last n_salt positions.
    focus = [_row(i, f"w{i}", f"t{i}") for i in range(1, 9)]  # 8 rows
    rounds = td.draw_rounds(focus, _salt_rows(2), rng=random.Random(0))
    assert len(rounds) == 10
    judged_pos = [i for i, r in enumerate(rounds) if r.judged]
    assert len(judged_pos) == 2
    assert min(judged_pos) < len(rounds) - 2, (
        f"seed 0 must shuffle at least one mastery check out of the trailing "
        f"slots; judged positions were {judged_pos}"
    )


# -- draw_rounds: arg validation -----------------------------------------------


def test_draw_rounds_n_max_zero_raises() -> None:  # error: n_max == 0 (< min_rounds) → ValueError
    with pytest.raises(ValueError):
        td.draw_rounds(_five_translatable_rows(), n_max=0, rng=random.Random(0))


def test_draw_rounds_n_max_negative_raises() -> None:  # error: n_max < 0 → ValueError
    with pytest.raises(ValueError):
        td.draw_rounds(_five_translatable_rows(), n_max=-1, rng=random.Random(0))


def test_draw_rounds_min_rounds_zero_raises() -> None:  # error: min_rounds == 0 → ValueError
    with pytest.raises(ValueError):
        td.draw_rounds(
            _five_translatable_rows(), n_max=5, min_rounds=0, rng=random.Random(0)
        )


def test_draw_rounds_min_rounds_negative_raises() -> None:  # error: min_rounds < 0 → ValueError
    with pytest.raises(ValueError):
        td.draw_rounds(
            _five_translatable_rows(), n_max=5, min_rounds=-1, rng=random.Random(0)
        )


def test_draw_rounds_n_max_less_than_min_rounds_raises() -> None:  # error: n_max < min_rounds → ValueError
    with pytest.raises(ValueError):
        td.draw_rounds(
            _five_translatable_rows(), n_max=3, min_rounds=5, rng=random.Random(0)
        )


def test_draw_rounds_n_salt_negative_raises() -> None:  # error: n_salt < 0 → ValueError
    with pytest.raises(ValueError):
        td.draw_rounds(
            _five_translatable_rows(), _salt_rows(2), n_salt=-1, rng=random.Random(0)
        )


# -- draw_rounds: filtering ----------------------------------------------------


def test_draw_rounds_filters_empty_translation_rows() -> None:  # None/empty/whitespace filtered before size check
    # 5 translatable rows + 3 non-translatable; non-translatable rows are
    # dropped *before* the size check, so the floor of 5 is still met.
    pool = _five_translatable_rows() + [
        _row(101, "none_trans", None),
        _row(102, "empty_trans", ""),
        _row(103, "ws_trans", "   "),
    ]
    rounds = td.draw_rounds(pool, rng=random.Random(0))
    assert len(rounds) == 5
    sampled_ids = {r.word_id for r in rounds}
    forbidden = {101, 102, 103}
    assert sampled_ids.isdisjoint(forbidden), (
        f"non-translatable rows {forbidden & sampled_ids} leaked into sample"
    )


def test_draw_rounds_filters_then_pool_too_small_raises() -> None:  # filter happens before size check
    # 4 translatable + 2 non-translatable = 6 rows but only 4 usable → ValueError
    pool = _five_translatable_rows()[:4] + [
        _row(101, "none_trans", None),
        _row(102, "empty_trans", "   "),
    ]
    with pytest.raises(ValueError):
        td.draw_rounds(pool, rng=random.Random(0))


def test_draw_rounds_filters_salt_pool_translations() -> None:  # untranslatable salt rows never become rounds
    salt = [
        _row(100, "none_trans", None),
        _row(101, "empty_trans", ""),
        _row(102, "ws_trans", "   "),
    ]
    rounds = td.draw_rounds(_five_translatable_rows(), salt, rng=random.Random(0))
    assert len(rounds) == 5
    assert all(r.judged is False for r in rounds), (
        "untranslatable remembered rows must be filtered from the salt pool"
    )


def test_draw_rounds_raises_pool_below_min_rounds_message() -> None:  # exact ValueError message format
    pool = _five_translatable_rows()[:4]  # 4 translatable rows, min_rounds=5
    with pytest.raises(ValueError) as excinfo:
        td.draw_rounds(pool, rng=random.Random(0))
    msg = str(excinfo.value)
    assert msg == "need at least 5 translatable rows, got 4", (
        f"expected exact message 'need at least 5 translatable rows, got 4', got {msg!r}"
    )


# -- draw_rounds: direction ------------------------------------------------------


def test_draw_rounds_direction_deterministic_with_seed() -> None:  # independent per round, deterministic with seed
    pool = _five_translatable_rows()
    a = td.draw_rounds(pool, rng=random.Random(7))
    b = td.draw_rounds(pool, rng=random.Random(7))
    dirs_a = [r.direction for r in a]
    dirs_b = [r.direction for r in b]
    assert dirs_a == dirs_b, f"same seed must produce identical directions; got {dirs_a} vs {dirs_b}"
    for r in a:
        assert r.direction in ("en2ru", "ru2en"), f"unexpected direction {r.direction!r}"


def test_draw_rounds_prompt_expected_match_direction() -> None:  # en2ru → prompt=text, ru2en → prompt=translation
    pool = _five_translatable_rows()
    salt = _salt_rows(2)
    by_id = {wid: (text, translation) for wid, text, translation in pool + salt}
    rounds = td.draw_rounds(pool, salt, rng=random.Random(123))
    for r in rounds:
        text, translation = by_id[r.word_id]
        if r.direction == "en2ru":
            assert r.prompt == text, f"en2ru round must prompt with text, got prompt={r.prompt!r} text={text!r}"
            assert r.expected == translation, (
                f"en2ru round must expect translation, got expected={r.expected!r} translation={translation!r}"
            )
        else:
            assert r.direction == "ru2en"
            assert r.prompt == translation, (
                f"ru2en round must prompt with translation, got prompt={r.prompt!r} translation={translation!r}"
            )
            assert r.expected == text, (
                f"ru2en round must expect text, got expected={r.expected!r} text={text!r}"
            )


def test_draw_rounds_can_produce_both_directions() -> None:  # over many seeds, both en2ru and ru2en appear
    pool = _five_translatable_rows()
    seen: set[str] = set()
    for seed in range(50):
        rounds = td.draw_rounds(pool, rng=random.Random(seed))
        seen.update(r.direction for r in rounds)
        if seen >= {"en2ru", "ru2en"}:
            break
    assert seen == {"en2ru", "ru2en"}, (
        f"both directions must be reachable across seeds, only saw {seen}"
    )


def test_draw_rounds_unicode_preserved() -> None:  # edge: unicode prompts/translations preserved verbatim
    pool = [
        _row(1, "hello", "привет"),
        _row(2, "world", "мир"),
        _row(3, "snow", "снег"),
        _row(4, "ёлка", "fir tree"),
        _row(5, "сердце", "heart"),
    ]
    rounds = td.draw_rounds(pool, rng=random.Random(7))
    by_id = {wid: (text, translation) for wid, text, translation in pool}
    for r in rounds:
        text, translation = by_id[r.word_id]
        # Whichever direction, both halves must come from the original strings verbatim.
        assert r.prompt in (text, translation), (
            f"prompt {r.prompt!r} must come verbatim from source row {(text, translation)!r}"
        )
        assert r.expected in (text, translation), (
            f"expected {r.expected!r} must come verbatim from source row {(text, translation)!r}"
        )


def test_draw_rounds_unique_word_ids_under_cap() -> None:  # sample without replacement across both pools
    pool = _five_translatable_rows() + [
        _row(6, "book", "книга"),
        _row(7, "sun", "солнце"),
        _row(8, "sea", "море"),
    ]
    rounds = td.draw_rounds(pool, _salt_rows(2), rng=random.Random(42))
    wids = [r.word_id for r in rounds]
    assert len(set(wids)) == len(wids), (
        f"word_ids must be unique (sample-without-replacement), got {wids}"
    )
    pool_ids = {r[0] for r in pool} | {100, 101}
    assert set(wids) <= pool_ids, f"sampled word_ids {wids} must come from pool {pool_ids}"


# -- grade_answer ----------------------------------------------------------------


def test_grade_answer_case_insensitive_cat() -> None:  # "Cat" vs "cat" → True
    assert td.grade_answer("Cat", _round(expected="cat")) is True


def test_grade_answer_strips_user_whitespace() -> None:  # "  cat  " vs "cat" → True
    assert td.grade_answer("  cat  ", _round(expected="cat")) is True


def test_grade_answer_unicode_cyrillic() -> None:  # "кошка" vs "Кошка" → True
    assert td.grade_answer("кошка", _round(expected="Кошка")) is True


def test_grade_answer_cyrillic_yo_case_insensitive() -> None:  # "  ПЁС " matches "пёс"
    rd = _round(word_id=2, prompt="dog", expected="пёс", direction="en2ru")
    assert td.grade_answer("  ПЁС ", rd) is True


def test_grade_answer_mismatch_returns_false() -> None:  # "dog" vs "cat" → False
    assert td.grade_answer("dog", _round(expected="cat")) is False


def test_grade_answer_multi_word_phrase() -> None:  # multi-word phrases use same rule end-to-end
    assert td.grade_answer("go home ", _round(expected="go home")) is True
    assert td.grade_answer("Go Home", _round(expected="go home")) is True


def test_grade_answer_empty_user_input_against_nonempty() -> None:  # empty input ≠ non-empty expected
    assert td.grade_answer("", _round(expected="cat")) is False


def test_grade_answer_expected_with_whitespace_is_trimmed() -> None:  # strip applies to both sides
    # Spec rule: user_input.strip().casefold() == expected.strip().casefold()
    assert td.grade_answer("cat", _round(expected="  cat  ")) is True


def test_grade_answer_sync_unchanged_semantics() -> None:
    # #143 AC6 — sync grade_answer kept its signature and case-fold/strip
    # equality (the strict grader that non-judged focus rounds still run on).
    import inspect

    sig = inspect.signature(td.grade_answer)
    params = list(sig.parameters)
    assert params == ["user_text", "rd"], (
        f"sync grade_answer signature must be (user_text, rd); got {params}"
    )
    assert not inspect.iscoroutinefunction(td.grade_answer), (
        "sync grade_answer must remain synchronous (non-judged rounds still use it)"
    )
    rd = _round(expected="кошка")
    assert td.grade_answer("Кошка", rd) is True, "case-insensitive equality"
    assert td.grade_answer("  кошка  ", rd) is True, "strip applies to user input"
    assert td.grade_answer("уставший", rd) is False, (
        "strict semantics: synonyms/inflection variants still rejected by sync grader"
    )


# -- apply_answer ------------------------------------------------------------------


def test_apply_answer_correct_increments_and_advances() -> None:  # correct path
    g = _fresh_game()
    td.apply_answer(g, True, source_word="cat")
    assert g.score == 1, f"score should be 1 after one correct, got {g.score}"
    assert g.wrong == [], f"wrong list must remain empty on correct, got {g.wrong}"
    assert g.current_round == 1, f"current_round should advance to 1, got {g.current_round}"


def test_apply_answer_wrong_appends_source_word_and_advances() -> None:  # wrong path
    g = _fresh_game()
    td.apply_answer(g, False, source_word="cat")
    assert g.score == 0, f"score must remain 0 on wrong, got {g.score}"
    assert g.wrong == ["cat"], f"wrong must contain ['cat'], got {g.wrong}"
    assert g.current_round == 1, f"current_round must advance on wrong too, got {g.current_round}"


def test_apply_answer_done_game_is_noop() -> None:  # done game → no-op, judged or not
    for judged in (False, True):
        rounds = [_round(judged=judged)]
        g = td.Game(chat_id=1, rounds=rounds, score=1, current_round=1, wrong=[])
        assert g.done is True, "fixture precondition: game must be done"
        td.apply_answer(g, True, source_word="cat")
        assert g.score == 1, "score must not change once game is done"
        assert g.current_round == 1, "current_round must not advance past n_rounds"
        assert g.wrong == [], "wrong must not be mutated on done game"
        td.apply_answer(g, False, source_word="dog")
        assert g.score == 1
        assert g.current_round == 1
        assert g.wrong == [], f"wrong must stay empty after no-op call, got {g.wrong}"


def test_apply_answer_wrong_preserves_insertion_order() -> None:  # edge: wrong list preserves order
    g = _fresh_game(n=4)
    td.apply_answer(g, True, source_word="cat")
    td.apply_answer(g, False, source_word="dog")
    td.apply_answer(g, False, source_word="tree")
    td.apply_answer(g, False, source_word="apple")
    assert g.wrong == ["dog", "tree", "apple"], (
        f"wrong must preserve insertion order, got {g.wrong}"
    )
    assert g.score == 1
    assert g.current_round == 4


# -- format_result ------------------------------------------------------------------


def test_format_result_with_wrong_lists_words() -> None:  # "4/5" and wrong word appear
    out = td.format_result(4, 5, ["dog"])
    assert "4/5" in out, f"score '4/5' must appear in {out!r}"
    assert "dog" in out, f"wrong word 'dog' must appear in {out!r}"


def test_format_result_no_wrong_omits_wrong_line() -> None:  # empty wrong → no "Wrong" mention
    out = td.format_result(5, 5, [])
    assert "Wrong" not in out, f"output must not mention 'Wrong' when wrong=[], got {out!r}"
    assert "5/5" in out, f"score '5/5' must appear in {out!r}"


def test_format_result_exact_strings_from_spec_examples() -> None:  # exact strings from spec
    assert td.format_result(3, 5, ["apple", "tree"]) == "🎯 You scored 3/5\nWrong: apple, tree"
    assert td.format_result(5, 5, []) == "🎯 You scored 5/5"


def test_format_result_zero_score_with_wrong() -> None:  # "🎯 You scored 0/5\nWrong: a, b"
    assert td.format_result(0, 5, ["a", "b"]) == "🎯 You scored 0/5\nWrong: a, b"


# -- grade_answer_llm (#143 — AC1..AC5 + edges + errors) -----------------------------
#
# We mock at the architectural seam: `llm.chat` is module-imported by
# typed_drill (`import llm` then `llm.chat(...)`), so monkeypatching the
# attribute on the `llm` module is picked up at call-time.


class _LLMRecorder:
    """Tracks llm.chat invocations and replies with a canned string/exception."""

    def __init__(self, reply: object) -> None:
        self.reply = reply
        self.calls: list[list[dict]] = []

    async def __call__(self, messages: list[dict], **_kwargs) -> str:
        self.calls.append(messages)
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply  # type: ignore[return-value]


def _patch_llm_chat(monkeypatch: pytest.MonkeyPatch, reply: object) -> _LLMRecorder:
    import llm as llm_mod

    rec = _LLMRecorder(reply)
    monkeypatch.setattr(llm_mod, "chat", rec)
    return rec


class _LLMRecorderKwargs(_LLMRecorder):
    """`_LLMRecorder` variant that also records the kwargs of each call."""

    def __init__(self, reply: object) -> None:
        super().__init__(reply)
        self.kwargs: list[dict] = []

    async def __call__(self, messages: list[dict], **kwargs) -> str:
        self.kwargs.append(kwargs)
        return await super().__call__(messages, **kwargs)


def _patch_llm_chat_kwargs(
    monkeypatch: pytest.MonkeyPatch, reply: object
) -> _LLMRecorderKwargs:
    import llm as llm_mod

    rec = _LLMRecorderKwargs(reply)
    monkeypatch.setattr(llm_mod, "chat", rec)
    return rec


def _patch_llm_chat_must_not_be_called(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace llm.chat with one that fails the test if called. Used for fast-path."""
    import llm as llm_mod

    state = {"calls": 0}

    async def boom(*_a, **_kw) -> str:
        state["calls"] += 1
        raise AssertionError(
            "fast path must not invoke llm.chat"
        )

    monkeypatch.setattr(llm_mod, "chat", boom)
    return state


# --- AC1 — fast path: exact match returns True with no LLM call -------------


def test_grade_answer_llm_fast_path_exact_match(monkeypatch: pytest.MonkeyPatch) -> None:
    # AC1 — exact match returns True without calling llm.chat
    state = _patch_llm_chat_must_not_be_called(monkeypatch)
    rd = _round(expected="кошка")
    result = asyncio.run(td.grade_answer_llm("кошка", rd))
    assert result is True, f"AC1 — exact match must return True, got {result!r}"
    assert state["calls"] == 0, (
        f"AC1 — fast path must skip llm.chat entirely, got {state['calls']} call(s)"
    )


def test_grade_answer_llm_fast_path_strips_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC1 + edge: leading/trailing whitespace stripped on both sides
    state = _patch_llm_chat_must_not_be_called(monkeypatch)
    rd = _round(expected="  кошка  ")
    result = asyncio.run(td.grade_answer_llm("  кошка ", rd))
    assert result is True, (
        f"AC1 — strip both sides on fast path; expected True, got {result!r}"
    )
    assert state["calls"] == 0, "edge — whitespace-stripped match must skip llm.chat"


def test_grade_answer_llm_fast_path_case_folded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC1 + edge: mixed-case Latin and Cyrillic both case-fold equal
    state = _patch_llm_chat_must_not_be_called(monkeypatch)
    rd = _round(expected="Кошка")
    result = asyncio.run(td.grade_answer_llm("кОшКа", rd))
    assert result is True, (
        f"AC1 — case-fold across Cyrillic must match on fast path, got {result!r}"
    )
    assert state["calls"] == 0


# --- AC2 — LLM YES verdict --------------------------------------------------


def test_grade_answer_llm_yes_returns_true(monkeypatch: pytest.MonkeyPatch) -> None:
    # AC2 — bare "YES" → True
    rec = _patch_llm_chat(monkeypatch, "YES")
    rd = _round(expected="уставший от")
    result = asyncio.run(td.grade_answer_llm("устать", rd))
    assert result is True, f"AC2 — bare YES must return True, got {result!r}"
    assert len(rec.calls) == 1, (
        f"AC2 — LLM must be consulted once when fast path misses, got {len(rec.calls)}"
    )


def test_grade_answer_llm_yes_dot_returns_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC2 + edge: "Yes." → True (trailing punctuation on the first token)
    _patch_llm_chat(monkeypatch, "Yes.")
    rd = _round(expected="устать")
    result = asyncio.run(td.grade_answer_llm("уставший", rd))
    assert result is True, f"AC2 — 'Yes.' must return True, got {result!r}"


def test_grade_answer_llm_yes_comma_explanation_returns_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC2 + edge: "Yes, that works" — first token "Yes," starts with YES
    _patch_llm_chat(monkeypatch, "Yes, that works")
    rd = _round(expected="устать")
    result = asyncio.run(td.grade_answer_llm("уставший", rd))
    assert result is True, (
        f"AC2 — 'Yes, that works' must return True (first token starts with YES), got {result!r}"
    )


def test_grade_answer_llm_lowercase_yes_returns_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC2 + edge: "yes!" → True after upper-casing
    _patch_llm_chat(monkeypatch, "yes!")
    rd = _round(expected="устать")
    result = asyncio.run(td.grade_answer_llm("уставший", rd))
    assert result is True, f"AC2 — 'yes!' must return True after upper, got {result!r}"


# --- AC3 — LLM NO verdict ---------------------------------------------------


def test_grade_answer_llm_no_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    # AC3 — "NO" → False
    _patch_llm_chat(monkeypatch, "NO")
    rd = _round(expected="кошка")
    result = asyncio.run(td.grade_answer_llm("собака", rd))
    assert result is False, f"AC3 — bare NO must return False, got {result!r}"


def test_grade_answer_llm_maybe_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC3 + edge: "maybe" — first token does not start with YES → False
    _patch_llm_chat(monkeypatch, "maybe")
    rd = _round(expected="кошка")
    result = asyncio.run(td.grade_answer_llm("собака", rd))
    assert result is False, f"AC3 — 'maybe' must return False, got {result!r}"


def test_grade_answer_llm_perhaps_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC3 + edge: "perhaps" — first token does not start with YES → False
    _patch_llm_chat(monkeypatch, "perhaps")
    rd = _round(expected="кошка")
    result = asyncio.run(td.grade_answer_llm("собака", rd))
    assert result is False, f"AC3 — 'perhaps' must return False, got {result!r}"


# --- AC4 — exception path collapses to None (judge unavailable) --------------


def test_grade_answer_llm_request_error_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC4 + error: httpx.RequestError → None (judge unavailable)
    _patch_llm_chat(monkeypatch, httpx.ConnectError("simulated"))
    rd = _round(expected="кошка")
    # Must NOT raise.
    result = asyncio.run(td.grade_answer_llm("собака", rd))
    assert result is None, (
        f"transport error must yield None (unavailable, not a judgment) so the "
        f"caller can skip remembered-demotion; got {result!r}"
    )


def test_grade_answer_llm_http_status_error_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC4 + error: httpx.HTTPStatusError → None
    fake_req = httpx.Request("POST", "http://x.local/")
    fake_resp = httpx.Response(503, request=fake_req)
    _patch_llm_chat(
        monkeypatch, httpx.HTTPStatusError("503", request=fake_req, response=fake_resp)
    )
    rd = _round(expected="кошка")
    result = asyncio.run(td.grade_answer_llm("собака", rd))
    assert result is None, f"HTTPStatusError must yield None, got {result!r}"


def test_grade_answer_llm_keyerror_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC4 + error: KeyError on response shape → None
    _patch_llm_chat(monkeypatch, KeyError("choices"))
    rd = _round(expected="кошка")
    result = asyncio.run(td.grade_answer_llm("собака", rd))
    assert result is None, f"KeyError must yield None, got {result!r}"


def test_grade_answer_llm_generic_exception_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC4 — any other exception type → None
    _patch_llm_chat(monkeypatch, RuntimeError("anything else"))
    rd = _round(expected="кошка")
    result = asyncio.run(td.grade_answer_llm("собака", rd))
    assert result is None, (
        f"generic exception must yield None, got {result!r}"
    )


# --- judge call is capped at JUDGE_TIMEOUT_S ---------------------------------


def test_grade_answer_llm_passes_judge_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # the judge call must carry timeout=JUDGE_TIMEOUT_S (10) to llm.chat
    rec = _patch_llm_chat_kwargs(monkeypatch, "NO")
    rd = _round(expected="кошка")
    asyncio.run(td.grade_answer_llm("собака", rd))
    assert len(rec.kwargs) == 1, (
        f"fast-path miss must consult llm.chat exactly once; got {len(rec.kwargs)}"
    )
    assert rec.kwargs[0].get("timeout") == td.JUDGE_TIMEOUT_S == 10, (
        "the judge call must be capped at JUDGE_TIMEOUT_S=10 (PTB is sequential — "
        f"a hung backend would freeze every chat); got "
        f"timeout={rec.kwargs[0].get('timeout')!r}"
    )


# --- AC5 — empty replies mean the judge is unavailable -----------------------


def test_grade_answer_llm_empty_reply_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC5 + edge: "" reply → None (no judgment made)
    _patch_llm_chat(monkeypatch, "")
    rd = _round(expected="кошка")
    result = asyncio.run(td.grade_answer_llm("собака", rd))
    assert result is None, f"empty reply must yield None, got {result!r}"


def test_grade_answer_llm_whitespace_reply_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC5 + edge: "   " whitespace-only reply → None
    _patch_llm_chat(monkeypatch, "   ")
    rd = _round(expected="кошка")
    result = asyncio.run(td.grade_answer_llm("собака", rd))
    assert result is None, (
        f"whitespace-only reply must yield None, got {result!r}"
    )


# --- edge — empty user input is not an exact match; falls through to LLM ----


def test_grade_answer_llm_empty_user_input_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # edge: "" user input vs non-empty expected → fast-path miss → LLM consulted
    rec = _patch_llm_chat(monkeypatch, "NO")
    rd = _round(expected="кошка")
    result = asyncio.run(td.grade_answer_llm("", rd))
    assert result is False, f"edge — empty input + NO verdict must return False, got {result!r}"
    assert len(rec.calls) == 1, (
        f"edge — empty input is not an exact match; LLM must be consulted exactly once, got {len(rec.calls)}"
    )


# === bot wiring — single typed_drills map, judged-keyed handle_message branch ===
#
# Mocking shape mirrors tests/test_mastered_label.py — temp-DB `conn` fixture
# from tests/conftest.py, Telegram Update/Context mocked at the architectural
# seam, `bot.conn` patched.


CHAT = 9210


def _make_command_update(chat_id: int = CHAT) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = 1
    update.effective_user.is_bot = False
    update.message.reply_text = AsyncMock()
    return update


def _make_message_update(text: str, chat_id: int = CHAT) -> MagicMock:
    update = _make_command_update(chat_id)
    update.message.text = text
    return update


def _make_callback_update(callback_data: str, chat_id: int = CHAT) -> MagicMock:
    update = MagicMock()
    update.callback_query.data = callback_data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.edit_message_reply_markup = AsyncMock()
    update.callback_query.message.edit_reply_markup = AsyncMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = 1
    update.effective_user.is_bot = False
    return update


def _make_context(args: list[str] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args if args is not None else []
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


def _patch_bot(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection) -> None:
    monkeypatch.setattr(bot, "conn", conn)
    bot.games.clear()
    bot.typed_drills.clear()
    bot.irregulars.clear()
    bot.pending_game_filters.clear()


def _last_reply(reply_text_mock: MagicMock) -> str:
    calls = reply_text_mock.call_args_list
    assert calls, "expected at least one reply_text call, got none"
    last = calls[-1]
    if last.args:
        return last.args[0]
    return last.kwargs.get("text", "")


def _seed_words(
    conn: sqlite3.Connection,
    words: list[str],
    *,
    remembered: bool,
    chat: int = CHAT,
) -> dict[str, int]:
    vocab.ensure_chat(conn, chat)
    vocab.add_words_bulk(conn, chat, words, translations=[f"tr_{w}" for w in words])
    ids: dict[str, int] = {}
    for w in words:
        wid = conn.execute(
            "SELECT id FROM words WHERE chat_id = ? AND text = ?", (chat, w)
        ).fetchone()["id"]
        ids[w] = wid
        if remembered:
            vocab.attach_label(
                conn, wid, vocab.get_or_create_label(conn, chat, vocab.REMEMBERED_LABEL)
            )
    return ids


def _repeat_streak(conn: sqlite3.Connection, word_id: int) -> int:
    return conn.execute(
        "SELECT repeat_correct_streak FROM words WHERE id = ?", (word_id,)
    ).fetchone()["repeat_correct_streak"]


def _has_label(conn: sqlite3.Connection, word_id: int, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM word_labels wl JOIN labels l ON l.id = wl.label_id "
        "WHERE wl.word_id = ? AND l.name = ?",
        (word_id, name),
    ).fetchone() is not None


# --- gm:drill start ------------------------------------------------------------


def test_gm_drill_starts_game_from_focus_pool(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # gm:drill → Game in the single typed_drills map; pool scales past 5
    _patch_bot(monkeypatch, conn)
    words = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta"]
    ids = _seed_words(conn, words, remembered=False)

    update = _make_callback_update("gm:drill")
    ctx = _make_context()
    asyncio.run(bot.on_games_menu(update, ctx))

    game = bot.typed_drills.get(CHAT)
    assert game is not None, "gm:drill must store a game in bot.typed_drills"
    assert game.n_rounds == 7, (
        f"a 7-word focus pool with no remembered words must give 7 rounds "
        f"(scales up to MAX_ROUNDS=10); got {game.n_rounds}"
    )
    assert {r.word_id for r in game.rounds} == set(ids.values())
    assert all(r.judged is False for r in game.rounds), (
        "with no remembered words every round must be a non-judged focus round"
    )
    # First round prompt is sent with the shared drill hint.
    ctx.bot.send_message.assert_awaited_once()
    sent = ctx.bot.send_message.await_args.kwargs.get("text", "")
    assert bot.DRILL_PROMPT_HINT in sent, (
        f"round prompt must carry DRILL_PROMPT_HINT; got {sent!r}"
    )


def test_gm_drill_salts_remembered_words_as_judged_rounds(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # gm:drill with 10 focus + 3 remembered → 10 rounds, exactly 2 judged
    _patch_bot(monkeypatch, conn)
    focus_words = [f"word{i}" for i in range(10)]
    focus_ids = _seed_words(conn, focus_words, remembered=False)
    salt_ids = _seed_words(conn, ["rem_a", "rem_b", "rem_c"], remembered=True)

    update = _make_callback_update("gm:drill")
    ctx = _make_context()
    asyncio.run(bot.on_games_menu(update, ctx))

    game = bot.typed_drills.get(CHAT)
    assert game is not None, "gm:drill must store a game in bot.typed_drills"
    assert game.n_rounds == td.MAX_ROUNDS == 10, (
        f"total rounds must cap at MAX_ROUNDS (10); got {game.n_rounds}"
    )
    judged = [r for r in game.rounds if r.judged]
    assert len(judged) == td.MAX_SALT == 2, (
        f"exactly MAX_SALT (2) judged mastery checks expected; got {len(judged)}"
    )
    assert {r.word_id for r in judged} <= set(salt_ids.values()), (
        "judged rounds must be remembered words"
    )
    assert {r.word_id for r in game.rounds if not r.judged} <= set(focus_ids.values()), (
        "non-judged rounds must come from the non-remembered focus pool"
    )
    ctx.bot.send_message.assert_awaited_once()
    sent = ctx.bot.send_message.await_args.kwargs.get("text", "")
    assert bot.DRILL_PROMPT_HINT in sent


def test_gm_drill_not_enough_focus_words_replies(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # <5 focus words → DRILL_NOT_ENOUGH even with plenty of remembered salt
    _patch_bot(monkeypatch, conn)
    _seed_words(conn, ["alpha", "beta", "gamma", "delta"], remembered=False)  # only 4
    # A big remembered pool must NOT rescue the focus-pool minimum.
    _seed_words(conn, [f"rem{i}" for i in range(6)], remembered=True)

    update = _make_callback_update("gm:drill")
    asyncio.run(bot.on_games_menu(update, _make_context()))

    assert _last_reply(update.callback_query.message.reply_text) == bot.DRILL_NOT_ENOUGH
    assert CHAT not in bot.typed_drills, "no game may be stored on the not-enough path"


def test_gm_repeat_and_gm_focus_are_outdated(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # stale gm:repeat / gm:focus taps → GAMES_MENU_OUTDATED, no game started
    _patch_bot(monkeypatch, conn)
    _seed_words(conn, ["alpha", "beta", "gamma", "delta", "epsilon"], remembered=False)

    for data in ("gm:repeat", "gm:focus"):
        update = _make_callback_update(data)
        ctx = _make_context()
        asyncio.run(bot.on_games_menu(update, ctx))
        assert (
            _last_reply(update.callback_query.message.reply_text)
            == bot.GAMES_MENU_OUTDATED
        ), f"stale {data} tap must reply GAMES_MENU_OUTDATED"
        assert CHAT not in bot.typed_drills, (
            f"stale {data} tap must not start a game"
        )
        ctx.bot.send_message.assert_not_awaited()


# --- handle_message answer flow ------------------------------------------------


def test_handle_message_judged_correct_records_source_repeat(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # judged round → grade_answer_llm (fast path here) + record_outcome(source="repeat")
    _patch_bot(monkeypatch, conn)
    state = _patch_llm_chat_must_not_be_called(monkeypatch)  # exact answer → no LLM call
    ids = _seed_words(conn, ["alpha", "beta"], remembered=True)
    rounds = [
        td.Round(word_id=ids["alpha"], prompt="alpha", expected="tr_alpha", direction="en2ru", judged=True),
        td.Round(word_id=ids["beta"], prompt="beta", expected="tr_beta", direction="en2ru", judged=True),
    ]
    bot.typed_drills[CHAT] = td.Game(chat_id=CHAT, rounds=rounds)

    update = _make_message_update("tr_alpha")
    asyncio.run(bot.handle_message(update, _make_context()))

    assert _repeat_streak(conn, ids["alpha"]) == 1, (
        "a correct judged answer must record_outcome(source='repeat') — "
        "repeat_correct_streak must bump to 1"
    )
    assert state["calls"] == 0, "exact match must take the strict fast path"
    game = bot.typed_drills[CHAT]
    assert game.score == 1 and game.current_round == 1
    first_reply = update.message.reply_text.call_args_list[0].args[0]
    assert first_reply == "✅ tr_alpha", f"correct feedback must be '✅ tr_alpha'; got {first_reply!r}"


def test_handle_message_judged_wrong_consults_llm_judge(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # judged round + fast-path miss → LLM judge; NO verdict → wrong, streak reset
    _patch_bot(monkeypatch, conn)
    rec = _patch_llm_chat(monkeypatch, "NO")
    ids = _seed_words(conn, ["alpha", "beta"], remembered=True)
    conn.execute(
        "UPDATE words SET repeat_correct_streak = 1 WHERE id = ?", (ids["alpha"],)
    )
    rounds = [
        td.Round(word_id=ids["alpha"], prompt="alpha", expected="tr_alpha", direction="en2ru", judged=True),
        td.Round(word_id=ids["beta"], prompt="beta", expected="tr_beta", direction="en2ru", judged=True),
    ]
    bot.typed_drills[CHAT] = td.Game(chat_id=CHAT, rounds=rounds)

    update = _make_message_update("совсем не то")
    asyncio.run(bot.handle_message(update, _make_context()))

    assert len(rec.calls) == 1, (
        f"a judged round must consult the LLM judge on a fast-path miss; got {len(rec.calls)} call(s)"
    )
    assert _repeat_streak(conn, ids["alpha"]) == 0, (
        "a wrong judged answer must record_outcome(source='repeat') — streak reset to 0"
    )
    game = bot.typed_drills[CHAT]
    assert game.wrong == ["alpha"], (
        f"the English word must be recorded in game.wrong; got {game.wrong!r}"
    )
    first_reply = update.message.reply_text.call_args_list[0].args[0]
    assert first_reply == "❌ correct: tr_alpha"


def test_handle_message_judged_yes_verdict_counts_correct(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # judge returns True on a non-exact answer → correct, source="repeat"
    _patch_bot(monkeypatch, conn)
    ids = _seed_words(conn, ["alpha", "beta"], remembered=True)

    async def judge_yes(user_text: str, rd: td.Round) -> bool | None:
        return True

    monkeypatch.setattr(td, "grade_answer_llm", judge_yes)
    rounds = [
        td.Round(word_id=ids["alpha"], prompt="alpha", expected="tr_alpha", direction="en2ru", judged=True),
        td.Round(word_id=ids["beta"], prompt="beta", expected="tr_beta", direction="en2ru", judged=True),
    ]
    bot.typed_drills[CHAT] = td.Game(chat_id=CHAT, rounds=rounds)

    update = _make_message_update("близкий синоним")
    asyncio.run(bot.handle_message(update, _make_context()))

    game = bot.typed_drills[CHAT]
    assert game.score == 1 and game.wrong == [], (
        "a YES verdict must score the round correct"
    )
    assert _repeat_streak(conn, ids["alpha"]) == 1, (
        "a YES verdict is a real judgment — source='repeat' must bump the streak"
    )
    first_reply = update.message.reply_text.call_args_list[0].args[0]
    assert first_reply == "✅ tr_alpha"


def test_handle_message_judge_unavailable_does_not_demote(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # judge returns None → scored wrong but source="game": no forget-flip
    _patch_bot(monkeypatch, conn)
    ids = _seed_words(conn, ["alpha", "beta"], remembered=True)
    conn.execute(
        "UPDATE words SET repeat_correct_streak = 1 WHERE id = ?", (ids["alpha"],)
    )

    async def judge_unavailable(user_text: str, rd: td.Round) -> bool | None:
        return None

    monkeypatch.setattr(td, "grade_answer_llm", judge_unavailable)
    rounds = [
        td.Round(word_id=ids["alpha"], prompt="alpha", expected="tr_alpha", direction="en2ru", judged=True),
        td.Round(word_id=ids["beta"], prompt="beta", expected="tr_beta", direction="en2ru", judged=True),
    ]
    bot.typed_drills[CHAT] = td.Game(chat_id=CHAT, rounds=rounds)

    update = _make_message_update("совсем не то")
    asyncio.run(bot.handle_message(update, _make_context()))

    # Scored as strict-wrong for the round…
    first_reply = update.message.reply_text.call_args_list[0].args[0]
    assert first_reply == "❌ correct: tr_alpha", (
        f"judge-unavailable round must be scored wrong; got {first_reply!r}"
    )
    game = bot.typed_drills[CHAT]
    assert game.score == 0 and game.wrong == ["alpha"]
    # …but recorded with source="game": no consequences that assume a real
    # judgment. The remembered word must NOT be demoted.
    assert _has_label(conn, ids["alpha"], vocab.REMEMBERED_LABEL), (
        "judge unavailable (None) must not detach `remembered` — that demotion "
        "requires a real NO judgment"
    )
    assert not _has_label(conn, ids["alpha"], vocab.FOCUS_HARD_LABEL), (
        "judge unavailable (None) must not attach `focus:hard`"
    )
    assert _repeat_streak(conn, ids["alpha"]) == 1, (
        "source='game' must leave repeat_correct_streak untouched when the "
        "judge was unavailable"
    )


def test_handle_message_judged_real_no_verdict_demotes(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # contrast: judge returns False (real judgment) → forget-flip fires
    _patch_bot(monkeypatch, conn)
    ids = _seed_words(conn, ["alpha", "beta"], remembered=True)
    conn.execute(
        "UPDATE words SET repeat_correct_streak = 1 WHERE id = ?", (ids["alpha"],)
    )

    async def judge_no(user_text: str, rd: td.Round) -> bool | None:
        return False

    monkeypatch.setattr(td, "grade_answer_llm", judge_no)
    rounds = [
        td.Round(word_id=ids["alpha"], prompt="alpha", expected="tr_alpha", direction="en2ru", judged=True),
        td.Round(word_id=ids["beta"], prompt="beta", expected="tr_beta", direction="en2ru", judged=True),
    ]
    bot.typed_drills[CHAT] = td.Game(chat_id=CHAT, rounds=rounds)

    update = _make_message_update("совсем не то")
    asyncio.run(bot.handle_message(update, _make_context()))

    first_reply = update.message.reply_text.call_args_list[0].args[0]
    assert first_reply == "❌ correct: tr_alpha"
    # A real NO is recorded with source="repeat" — the forget-flip fires.
    assert not _has_label(conn, ids["alpha"], vocab.REMEMBERED_LABEL), (
        "a real NO verdict must detach `remembered` (forget-flip)"
    )
    assert _has_label(conn, ids["alpha"], vocab.FOCUS_HARD_LABEL), (
        "a real NO verdict must attach `focus:hard`"
    )
    assert _repeat_streak(conn, ids["alpha"]) == 0, (
        "source='repeat' wrong must reset repeat_correct_streak"
    )


def test_handle_message_nonjudged_grades_strictly_source_game(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # judged=False round → strict grade_answer (never LLM) + record_outcome(source="game")
    _patch_bot(monkeypatch, conn)
    state = _patch_llm_chat_must_not_be_called(monkeypatch)
    ids = _seed_words(conn, ["alpha", "beta"], remembered=False)
    conn.execute(
        "UPDATE words SET repeat_correct_streak = 1 WHERE id = ?", (ids["alpha"],)
    )
    rounds = [
        td.Round(word_id=ids["alpha"], prompt="alpha", expected="tr_alpha", direction="en2ru"),
        td.Round(word_id=ids["beta"], prompt="beta", expected="tr_beta", direction="en2ru"),
    ]
    bot.typed_drills[CHAT] = td.Game(chat_id=CHAT, rounds=rounds)

    # Wrong answer: strict grader rejects, and the LLM judge is NEVER consulted.
    update = _make_message_update("not the translation")
    asyncio.run(bot.handle_message(update, _make_context()))
    assert state["calls"] == 0, "non-judged rounds must never call the LLM judge"
    assert (
        update.message.reply_text.call_args_list[0].args[0] == "❌ correct: tr_alpha"
    )
    # source="game", not "repeat": the primed repeat streak survives untouched.
    assert _repeat_streak(conn, ids["alpha"]) == 1, (
        "non-judged outcomes are source='game' — repeat_correct_streak must be untouched"
    )

    # Correct answer on the next round: still no LLM, still source="game".
    update2 = _make_message_update("tr_beta")
    asyncio.run(bot.handle_message(update2, _make_context()))
    assert state["calls"] == 0
    assert _repeat_streak(conn, ids["beta"]) == 0, (
        "a correct non-judged answer must NOT bump repeat_correct_streak (source='game')"
    )
    assert update2.message.reply_text.call_args_list[0].args[0] == "✅ tr_beta"


def test_handle_message_drill_completion_sends_summary_and_clears(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # last answer → format_result summary, game removed from typed_drills
    _patch_bot(monkeypatch, conn)
    ids = _seed_words(conn, ["alpha"], remembered=False)
    rounds = [
        td.Round(word_id=ids["alpha"], prompt="alpha", expected="tr_alpha", direction="en2ru"),
    ]
    bot.typed_drills[CHAT] = td.Game(chat_id=CHAT, rounds=rounds)

    update = _make_message_update("tr_alpha")
    asyncio.run(bot.handle_message(update, _make_context()))

    assert CHAT not in bot.typed_drills, "finished drill must be popped from typed_drills"
    replies = [c.args[0] for c in update.message.reply_text.call_args_list]
    assert replies[-1] == td.format_result(1, 1, []), (
        f"final message must be the format_result summary; got {replies[-1]!r}"
    )


# --- in-progress gate ------------------------------------------------------------


def test_cmd_games_in_progress_reply_for_drill(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # /games during a drill → DRILL_IN_PROGRESS
    _patch_bot(monkeypatch, conn)
    vocab.ensure_chat(conn, CHAT)
    bot.typed_drills[CHAT] = td.Game(chat_id=CHAT, rounds=[_round()])
    update = _make_command_update()
    asyncio.run(bot.cmd_games(update, _make_context([])))
    assert _last_reply(update.message.reply_text) == bot.DRILL_IN_PROGRESS, (
        f"/games during a typed drill must reply {bot.DRILL_IN_PROGRESS!r}"
    )
    bot.typed_drills.clear()


def test_games_menu_blocked_while_drill_running(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # gm:* taps during a drill → in-progress reply, no second game
    _patch_bot(monkeypatch, conn)
    vocab.ensure_chat(conn, CHAT)
    running = td.Game(chat_id=CHAT, rounds=[_round()])
    bot.typed_drills[CHAT] = running

    update = _make_callback_update("gm:drill")
    asyncio.run(bot.on_games_menu(update, _make_context()))

    assert (
        _last_reply(update.callback_query.message.reply_text)
        == bot.DRILL_IN_PROGRESS
    )
    assert bot.typed_drills[CHAT] is running, (
        "the running drill must not be replaced by a second gm: tap"
    )
    bot.typed_drills.clear()
