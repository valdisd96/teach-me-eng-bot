"""Tests for repeat_game.py — typed-answer Repeat mini-game (issue #127).

Spec-driven: every assertion ties back to a numbered AC, an enumerated edge
case, or an error condition from the latest <!-- agent-plan v1 --> comment on
#127. The Behavioral spec only enumerates ACs for the repeat_game module;
bot-side wiring (gm:repeat picker button, in-flight session routing,
under-5-pool reply) is out of scope here.
"""

from __future__ import annotations

import random

import pytest

import repeat_game as rg


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


def _round(
    *,
    word_id: int = 1,
    prompt: str = "cat",
    expected: str = "кошка",
    direction: str = "en2ru",
) -> rg.Round:
    # `direction` is informational per the spec; grading is direction-agnostic.
    return rg.Round(word_id=word_id, prompt=prompt, expected=expected, direction=direction)


# -- draw_rounds: pool sizing (AC1, AC2, edge & error cases) -----------------


def test_draw_rounds_raises_when_pool_below_n_rounds() -> None:  # AC1 — 4 rows, default n=5 → ValueError
    pool = _five_translatable_rows()[:4]
    with pytest.raises(ValueError):
        rg.draw_rounds(pool, rng=random.Random(0))


def test_draw_rounds_returns_n_unique_word_ids() -> None:  # AC2 — seeded rng, sample-without-replacement
    pool = _five_translatable_rows() + [_row(6, "book", "книга"), _row(7, "sun", "солнце")]
    rounds = rg.draw_rounds(pool, n_rounds=5, rng=random.Random(42))
    assert len(rounds) == 5, f"expected 5 rounds, got {len(rounds)}"
    wids = [r.word_id for r in rounds]
    assert len(set(wids)) == 5, f"word_ids must be unique (without-replacement), got {wids}"
    pool_ids = {r[0] for r in pool}
    assert set(wids) <= pool_ids, f"sampled word_ids {wids} must come from pool {pool_ids}"


def test_draw_rounds_pool_exactly_n_rounds_succeeds() -> None:  # edge: pool size == n_rounds
    pool = _five_translatable_rows()
    rounds = rg.draw_rounds(pool, n_rounds=5, rng=random.Random(0))
    assert len(rounds) == 5
    assert {r.word_id for r in rounds} == {1, 2, 3, 4, 5}


def test_draw_rounds_empty_pool_raises() -> None:  # edge: empty pool → ValueError
    with pytest.raises(ValueError):
        rg.draw_rounds([], n_rounds=5, rng=random.Random(0))


def test_draw_rounds_n_rounds_zero_raises() -> None:  # error: n_rounds == 0 → ValueError
    with pytest.raises(ValueError):
        rg.draw_rounds(_five_translatable_rows(), n_rounds=0, rng=random.Random(0))


def test_draw_rounds_n_rounds_negative_raises() -> None:  # error: n_rounds < 0 → ValueError
    with pytest.raises(ValueError):
        rg.draw_rounds(_five_translatable_rows(), n_rounds=-1, rng=random.Random(0))


# -- draw_rounds: filtering (AC7) --------------------------------------------


def test_draw_rounds_filters_empty_translation_rows() -> None:  # AC7 — None/empty/whitespace filtered before size check
    # 5 translatable rows + 3 non-translatable; spec says non-translatable are
    # dropped *before* the size check, so n=5 should succeed despite "8 rows".
    pool = _five_translatable_rows() + [
        _row(101, "none_trans", None),
        _row(102, "empty_trans", ""),
        _row(103, "ws_trans", "   "),
    ]
    rounds = rg.draw_rounds(pool, n_rounds=5, rng=random.Random(0))
    assert len(rounds) == 5
    sampled_ids = {r.word_id for r in rounds}
    forbidden = {101, 102, 103}
    assert sampled_ids.isdisjoint(forbidden), (
        f"non-translatable rows {forbidden & sampled_ids} leaked into sample"
    )


def test_draw_rounds_filters_then_pool_too_small_raises() -> None:  # AC7 + AC1 — filter happens before size check
    # 4 translatable + 2 non-translatable = 6 rows but only 4 usable → ValueError for default n=5
    pool = _five_translatable_rows()[:4] + [
        _row(101, "none_trans", None),
        _row(102, "empty_trans", "   "),
    ]
    with pytest.raises(ValueError):
        rg.draw_rounds(pool, rng=random.Random(0))


# -- draw_rounds: direction (AC3) --------------------------------------------


def test_draw_rounds_direction_deterministic_with_seed() -> None:  # AC3 — independent per round, deterministic with seed
    pool = _five_translatable_rows()
    a = rg.draw_rounds(pool, n_rounds=5, rng=random.Random(7))
    b = rg.draw_rounds(pool, n_rounds=5, rng=random.Random(7))
    dirs_a = [r.direction for r in a]
    dirs_b = [r.direction for r in b]
    assert dirs_a == dirs_b, f"same seed must produce identical directions; got {dirs_a} vs {dirs_b}"
    for r in a:
        assert r.direction in ("en2ru", "ru2en"), f"unexpected direction {r.direction!r}"


def test_draw_rounds_prompt_expected_match_direction() -> None:  # AC3 — en2ru → prompt=text, ru2en → prompt=translation
    pool = _five_translatable_rows()
    # Build a lookup of (word_id) → (text, translation) so we can check each round
    by_id = {wid: (text, translation) for wid, text, translation in pool}
    rounds = rg.draw_rounds(pool, n_rounds=5, rng=random.Random(123))
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


def test_draw_rounds_can_produce_both_directions() -> None:  # AC3 — over many seeds, both en2ru and ru2en appear
    pool = _five_translatable_rows()
    seen: set[str] = set()
    for seed in range(50):
        rounds = rg.draw_rounds(pool, n_rounds=5, rng=random.Random(seed))
        seen.update(r.direction for r in rounds)
        if seen >= {"en2ru", "ru2en"}:
            break
    assert seen == {"en2ru", "ru2en"}, (
        f"both directions must be reachable across seeds, only saw {seen}"
    )


# -- grade_answer (AC4) ------------------------------------------------------


def test_grade_answer_case_insensitive_cat() -> None:  # AC4 — "Cat" vs "cat" → True
    assert rg.grade_answer("Cat", _round(expected="cat")) is True


def test_grade_answer_strips_user_whitespace() -> None:  # AC4 — "  cat  " vs "cat" → True
    assert rg.grade_answer("  cat  ", _round(expected="cat")) is True


def test_grade_answer_unicode_cyrillic() -> None:  # AC4 — "кошка" vs "Кошка" → True
    assert rg.grade_answer("кошка", _round(expected="Кошка")) is True


def test_grade_answer_mismatch_returns_false() -> None:  # AC4 — "dog" vs "cat" → False
    assert rg.grade_answer("dog", _round(expected="cat")) is False


def test_grade_answer_multi_word_phrase() -> None:  # AC4 — multi-word phrases use same rule end-to-end
    assert rg.grade_answer("go home ", _round(expected="go home")) is True
    assert rg.grade_answer("Go Home", _round(expected="go home")) is True


def test_grade_answer_empty_user_input_against_nonempty() -> None:  # AC4 — empty input ≠ non-empty expected
    assert rg.grade_answer("", _round(expected="cat")) is False


def test_grade_answer_expected_with_whitespace_is_trimmed() -> None:  # AC4 — strip applies to both sides
    # Spec rule: user_input.strip().casefold() == expected.strip().casefold()
    assert rg.grade_answer("cat", _round(expected="  cat  ")) is True


# -- apply_answer (AC5) ------------------------------------------------------


def _fresh_game(n: int = 3) -> rg.Game:
    rounds = [
        _round(word_id=i, prompt=f"w{i}", expected=f"t{i}") for i in range(1, n + 1)
    ]
    return rg.Game(chat_id=42, rounds=rounds)


def test_apply_answer_correct_increments_and_advances() -> None:  # AC5 — correct path
    g = _fresh_game()
    rg.apply_answer(g, True, source_word="cat")
    assert g.score == 1, f"score should be 1 after one correct, got {g.score}"
    assert g.wrong == [], f"wrong list must remain empty on correct, got {g.wrong}"
    assert g.current_round == 1, f"current_round should advance to 1, got {g.current_round}"


def test_apply_answer_wrong_appends_source_word_and_advances() -> None:  # AC5 — wrong path
    g = _fresh_game()
    rg.apply_answer(g, False, source_word="cat")
    assert g.score == 0, f"score must remain 0 on wrong, got {g.score}"
    assert g.wrong == ["cat"], f"wrong must contain ['cat'], got {g.wrong}"
    assert g.current_round == 1, f"current_round must advance on wrong too, got {g.current_round}"


def test_apply_answer_done_game_is_noop() -> None:  # AC5 — done game → no-op
    # n_rounds=1, already at current_round=1 → game.done
    rounds = [_round()]
    g = rg.Game(chat_id=1, rounds=rounds, score=1, current_round=1, wrong=[])
    assert g.done is True, "fixture precondition: game must be done"
    rg.apply_answer(g, True, source_word="cat")
    assert g.score == 1, "score must not change once game is done"
    assert g.current_round == 1, "current_round must not advance past n_rounds"
    assert g.wrong == [], "wrong must not be mutated on done game"
    rg.apply_answer(g, False, source_word="dog")
    assert g.score == 1
    assert g.current_round == 1
    assert g.wrong == [], f"wrong must stay empty after no-op call, got {g.wrong}"


def test_apply_answer_wrong_preserves_insertion_order() -> None:  # edge: wrong list preserves order
    g = _fresh_game(n=4)
    rg.apply_answer(g, True, source_word="cat")
    rg.apply_answer(g, False, source_word="dog")
    rg.apply_answer(g, False, source_word="tree")
    rg.apply_answer(g, False, source_word="apple")
    assert g.wrong == ["dog", "tree", "apple"], (
        f"wrong must preserve insertion order, got {g.wrong}"
    )
    assert g.score == 1
    assert g.current_round == 4


# -- format_result (AC6) -----------------------------------------------------


def test_format_result_with_wrong_lists_words() -> None:  # AC6 — "4/5" and wrong word appear
    out = rg.format_result(4, 5, ["dog"])
    assert "4/5" in out, f"score '4/5' must appear in {out!r}"
    assert "dog" in out, f"wrong word 'dog' must appear in {out!r}"


def test_format_result_no_wrong_omits_wrong_line() -> None:  # AC6 — empty wrong → no "Wrong" mention
    out = rg.format_result(5, 5, [])
    assert "Wrong" not in out, f"output must not mention 'Wrong' when wrong=[], got {out!r}"
    assert "5/5" in out, f"score '5/5' must appear in {out!r}"


def test_format_result_exact_strings_from_spec_examples() -> None:  # AC6 — exact strings from spec
    assert rg.format_result(3, 5, ["apple", "tree"]) == "🎯 You scored 3/5\nWrong: apple, tree"
    assert rg.format_result(5, 5, []) == "🎯 You scored 5/5"
