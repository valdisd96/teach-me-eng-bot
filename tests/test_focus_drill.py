"""Tests for focus_drill.py — typed-answer Focus drill mini-game (issue #140).

Spec-driven: every assertion ties back to a numbered AC, an enumerated edge
case, or an error condition from the latest <!-- agent-plan v1 --> comment on
#140. Bot-side wiring (gm:focus picker button, in-flight session routing,
sticky-focus button hiding) is out of scope here — the spec only enumerates
ACs for the focus_drill module.
"""

from __future__ import annotations

import random

import pytest

import focus_drill as fd


# -- helpers -----------------------------------------------------------------


def _row(wid: int, text: str, translation: str | None) -> tuple[int, str, str | None]:
    return (wid, text, translation)


def _five_translatable_rows() -> list[tuple[int, str, str]]:
    return [
        _row(1, "cat", "кот"),
        _row(2, "dog", "пёс"),
        _row(3, "sun", "солнце"),
        _row(4, "sky", "небо"),
        _row(5, "sea", "море"),
    ]


def _round(
    *,
    word_id: int = 1,
    prompt: str = "cat",
    expected: str = "кот",
    direction: str = "en2ru",
) -> fd.Round:
    return fd.Round(word_id=word_id, prompt=prompt, expected=expected, direction=direction)


def _fresh_game(n: int = 3) -> fd.Game:
    rounds = [
        _round(word_id=i, prompt=f"w{i}", expected=f"t{i}") for i in range(1, n + 1)
    ]
    return fd.Game(chat_id=42, rounds=rounds)


# -- draw_rounds: pool sizing (AC1, edge cases) ------------------------------


def test_draw_rounds_returns_min_max_when_no_n_max_override() -> None:  # AC1 — default n_max=MAX_ROUNDS
    # 7 translatable rows; pool > MAX_ROUNDS → caps at MAX_ROUNDS.
    pool = _five_translatable_rows() + [
        _row(6, "book", "книга"),
        _row(7, "tree", "дерево"),
    ]
    rounds = fd.draw_rounds(pool, rng=random.Random(0))
    assert len(rounds) == min(fd.MAX_ROUNDS, 7), (
        f"expected min(MAX_ROUNDS, 7) rounds, got {len(rounds)}"
    )


def test_draw_rounds_returns_full_max_when_pool_larger() -> None:  # AC1 + edge: pool > MAX_ROUNDS → MAX_ROUNDS
    pool = [_row(i, f"w{i}", f"t{i}") for i in range(1, 21)]  # 20 rows
    rounds = fd.draw_rounds(pool, rng=random.Random(0))
    assert len(rounds) == fd.MAX_ROUNDS, (
        f"pool larger than MAX_ROUNDS must cap at MAX_ROUNDS ({fd.MAX_ROUNDS}), got {len(rounds)}"
    )


def test_draw_rounds_pool_exactly_min_rounds() -> None:  # edge: pool == min_rounds → exactly min_rounds rounds
    pool = _five_translatable_rows()  # 5 rows = MIN_ROUNDS
    rounds = fd.draw_rounds(pool, rng=random.Random(0))
    assert len(rounds) == fd.MIN_ROUNDS
    assert {r.word_id for r in rounds} == {1, 2, 3, 4, 5}


# -- draw_rounds: filtering (AC2) --------------------------------------------


def test_draw_rounds_filters_none_empty_whitespace() -> None:  # AC2 — None/empty/whitespace filtered before size check
    pool = _five_translatable_rows() + [
        _row(101, "none_trans", None),
        _row(102, "empty_trans", ""),
        _row(103, "ws_trans", "   "),
    ]
    rounds = fd.draw_rounds(pool, rng=random.Random(0))
    assert len(rounds) == 5
    sampled_ids = {r.word_id for r in rounds}
    forbidden = {101, 102, 103}
    assert sampled_ids.isdisjoint(forbidden), (
        f"non-translatable rows {forbidden & sampled_ids} leaked into sample"
    )


def test_draw_rounds_filter_then_min_floor_check() -> None:  # AC2 + AC3 — filter runs before min_rounds floor
    # 4 translatable + 2 non-translatable = 6 raw rows but only 4 usable → ValueError
    pool = _five_translatable_rows()[:4] + [
        _row(101, "none_trans", None),
        _row(102, "empty_trans", "   "),
    ]
    with pytest.raises(ValueError):
        fd.draw_rounds(pool, rng=random.Random(0))


# -- draw_rounds: error message (AC3) ----------------------------------------


def test_draw_rounds_raises_pool_below_min_rounds_message() -> None:  # AC3 — exact ValueError message format
    pool = _five_translatable_rows()[:4]  # 4 translatable rows, default min_rounds=5
    with pytest.raises(ValueError) as excinfo:
        fd.draw_rounds(pool, rng=random.Random(0))
    msg = str(excinfo.value)
    assert msg == "need at least 5 translatable rows, got 4", (
        f"expected exact message 'need at least 5 translatable rows, got 4', got {msg!r}"
    )


# -- draw_rounds: arg validation (AC4) ---------------------------------------


def test_draw_rounds_min_rounds_zero_raises() -> None:  # AC4 + error: min_rounds == 0 → ValueError
    with pytest.raises(ValueError):
        fd.draw_rounds(_five_translatable_rows(), min_rounds=0, rng=random.Random(0))


def test_draw_rounds_min_rounds_negative_raises() -> None:  # AC4 + error: min_rounds < 0 → ValueError
    with pytest.raises(ValueError):
        fd.draw_rounds(_five_translatable_rows(), min_rounds=-1, rng=random.Random(0))


def test_draw_rounds_n_max_less_than_min_rounds_raises() -> None:  # AC4 + error: n_max < min_rounds → ValueError
    with pytest.raises(ValueError):
        fd.draw_rounds(_five_translatable_rows(), n_max=3, min_rounds=5, rng=random.Random(0))


# -- draw_rounds: direction (AC5) --------------------------------------------


def test_draw_rounds_direction_prompt_expected_consistent() -> None:  # AC5 — en2ru→(text,translation); ru2en→(translation,text)
    pool = _five_translatable_rows()
    by_id = {wid: (text, translation) for wid, text, translation in pool}
    rounds = fd.draw_rounds(pool, rng=random.Random(123))
    for r in rounds:
        text, translation = by_id[r.word_id]
        if r.direction == "en2ru":
            assert r.prompt == text, (
                f"en2ru round must prompt with text, got prompt={r.prompt!r} text={text!r}"
            )
            assert r.expected == translation, (
                f"en2ru round must expect translation, got expected={r.expected!r} translation={translation!r}"
            )
        else:
            assert r.direction == "ru2en", f"unexpected direction {r.direction!r}"
            assert r.prompt == translation, (
                f"ru2en round must prompt with translation, got prompt={r.prompt!r} translation={translation!r}"
            )
            assert r.expected == text, (
                f"ru2en round must expect text, got expected={r.expected!r} text={text!r}"
            )


# -- draw_rounds: uniqueness (AC6) -------------------------------------------


def test_draw_rounds_unique_word_ids() -> None:  # AC6 — sample without replacement
    pool = _five_translatable_rows() + [
        _row(6, "book", "книга"),
        _row(7, "tree", "дерево"),
        _row(8, "house", "дом"),
    ]
    rounds = fd.draw_rounds(pool, rng=random.Random(42))
    wids = [r.word_id for r in rounds]
    assert len(set(wids)) == len(wids), (
        f"word_ids must be unique (sample-without-replacement), got {wids}"
    )
    pool_ids = {r[0] for r in pool}
    assert set(wids) <= pool_ids, f"sampled word_ids {wids} must come from pool {pool_ids}"


# -- draw_rounds: empty / unicode edges --------------------------------------


def test_draw_rounds_empty_pool_raises() -> None:  # edge: empty rows → ValueError (pool 0 < min_rounds)
    with pytest.raises(ValueError):
        fd.draw_rounds([], rng=random.Random(0))


def test_draw_rounds_unicode_preserved() -> None:  # edge: unicode prompts/translations preserved verbatim
    pool = [
        _row(1, "hello", "привет"),
        _row(2, "world", "мир"),
        _row(3, "snow", "снег"),
        _row(4, "ёлка", "fir tree"),
        _row(5, "сердце", "heart"),
    ]
    rounds = fd.draw_rounds(pool, rng=random.Random(7))
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


# -- grade_answer (AC7) ------------------------------------------------------


def test_grade_answer_case_insensitive_whitespace() -> None:  # AC7 — "  Hello " matches "hello"
    assert fd.grade_answer("  Hello ", _round(expected="hello")) is True


def test_grade_answer_mismatch_returns_false() -> None:  # AC7 — wrong answer → False
    assert fd.grade_answer("dog", _round(expected="cat")) is False


def test_grade_answer_cyrillic_case_insensitive() -> None:  # AC7 + example — "  ПЁС " matches "пёс"
    rd = _round(word_id=2, prompt="dog", expected="пёс", direction="en2ru")
    assert fd.grade_answer("  ПЁС ", rd) is True


# -- apply_answer (AC8, AC9) -------------------------------------------------


def test_apply_answer_correct_increments_and_advances() -> None:  # AC8 — correct: +1 score, +1 round, wrong unchanged
    g = _fresh_game()
    fd.apply_answer(g, True, source_word="cat")
    assert g.score == 1, f"score should be 1 after one correct, got {g.score}"
    assert g.wrong == [], f"wrong list must remain empty on correct, got {g.wrong}"
    assert g.current_round == 1, f"current_round should advance to 1, got {g.current_round}"


def test_apply_answer_wrong_appends_and_advances() -> None:  # AC8 — wrong: source_word appended, +1 round, score unchanged
    g = _fresh_game()
    fd.apply_answer(g, False, source_word="cat")
    assert g.score == 0, f"score must remain 0 on wrong, got {g.score}"
    assert g.wrong == ["cat"], f"wrong must contain ['cat'], got {g.wrong}"
    assert g.current_round == 1, f"current_round must advance on wrong too, got {g.current_round}"


def test_apply_answer_no_op_when_done() -> None:  # AC9 — done game → no mutation (score, current_round, wrong all unchanged)
    rounds = [_round()]
    g = fd.Game(chat_id=1, rounds=rounds, score=1, current_round=1, wrong=[])
    assert g.done is True, "fixture precondition: game must be done"
    fd.apply_answer(g, True, source_word="cat")
    assert g.score == 1, "score must not change once game is done"
    assert g.current_round == 1, "current_round must not advance past n_rounds"
    assert g.wrong == [], "wrong must not be mutated on done game"
    fd.apply_answer(g, False, source_word="dog")
    assert g.score == 1
    assert g.current_round == 1
    assert g.wrong == [], f"wrong must stay empty after no-op call, got {g.wrong}"


# -- format_result (AC10) ----------------------------------------------------


def test_format_result_no_wrong_omits_line() -> None:  # AC10 — "🎯 You scored 3/5" exact
    assert fd.format_result(3, 5, []) == "🎯 You scored 3/5"


def test_format_result_with_wrong_includes_line() -> None:  # AC10 — "🎯 You scored 2/5\nWrong: apple, horse" exact
    assert fd.format_result(2, 5, ["apple", "horse"]) == "🎯 You scored 2/5\nWrong: apple, horse"


def test_format_result_zero_score_with_wrong() -> None:  # AC10 + example — "🎯 You scored 0/5\nWrong: a, b"
    assert fd.format_result(0, 5, ["a", "b"]) == "🎯 You scored 0/5\nWrong: a, b"


# -- MAX_ROUNDS = 5 (#143 — AC8) ---------------------------------------------


def test_max_rounds_constant_is_five() -> None:
    # AC8 — module-level constant MAX_ROUNDS collapses from 10 to 5.
    assert fd.MAX_ROUNDS == 5, (
        f"AC8 — focus_drill.MAX_ROUNDS must equal 5 (was 10); got {fd.MAX_ROUNDS}"
    )


def test_draw_rounds_seven_row_pool_returns_five() -> None:
    # AC8 — with a 7-row pool, draw_rounds yields exactly 5 rounds (was 7 before).
    pool = _five_translatable_rows() + [
        _row(6, "book", "книга"),
        _row(7, "tree", "дерево"),
    ]
    rounds = fd.draw_rounds(pool, rng=random.Random(0))
    assert len(rounds) == 5, (
        f"AC8 — 7-row pool must cap at MAX_ROUNDS=5 (was 7 before #143); got {len(rounds)}"
    )
