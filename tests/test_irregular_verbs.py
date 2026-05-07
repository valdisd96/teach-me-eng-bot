"""Tests for irregular_verbs.py — pure verb-trio mini-game (issue #80).

Spec-driven: every assertion ties back to a numbered AC, an enumerated edge case,
or an error condition from the latest <!-- agent-plan v1 --> comment on #80.
Bot-side wiring (cmd_irregulars, plain-text routing, irregulars dict) is out of
scope here — Behavioral spec only enumerates ACs for the irregular_verbs module.
"""

from __future__ import annotations

import random

import pytest

import irregular_verbs as iv


# -- helpers -----------------------------------------------------------------


def _round(
    base: str = "go",
    past_simple_alts: list[str] | None = None,
    past_participle_alts: list[str] | None = None,
) -> iv.Round:
    return iv.Round(
        base=base,
        past_simple_alts=past_simple_alts if past_simple_alts is not None else ["went"],
        past_participle_alts=(
            past_participle_alts if past_participle_alts is not None else ["gone"]
        ),
    )


def _verb_with_multi_alts() -> tuple[str, list[str], list[str]]:
    """Pick any table entry where one of the alt lists has length >= 2.

    Spec AC6 is intentionally written to allow falling back to "another with
    documented alts" if "learn" is absent. Discovering programmatically keeps
    the test robust to table edits.
    """
    for entry in iv.IRREGULAR_VERBS:
        _base, ps_alts, pp_alts = entry
        if len(ps_alts) >= 2 or len(pp_alts) >= 2:
            return entry
    pytest.skip("table has no verbs with >=2 alternates; AC6 not exercisable")


# -- IRREGULAR_VERBS table (AC1) ---------------------------------------------


def test_table_has_min_30_unique_lowercase_entries() -> None:  # AC1 — well-formed static table
    assert len(iv.IRREGULAR_VERBS) >= 30, (
        f"expected >=30 entries, got {len(iv.IRREGULAR_VERBS)}"
    )
    bases = [base for base, _ps, _pp in iv.IRREGULAR_VERBS]
    assert len(set(bases)) == len(bases), (
        f"bases must be unique; duplicates: "
        f"{[b for b in bases if bases.count(b) > 1]}"
    )
    for base, ps_alts, pp_alts in iv.IRREGULAR_VERBS:
        assert base and base == base.lower(), f"base not lowercase/non-empty: {base!r}"
        assert ps_alts, f"past_simple_alts empty for {base!r}"
        assert pp_alts, f"past_participle_alts empty for {base!r}"
        for form in (*ps_alts, *pp_alts):
            assert form and form == form.lower(), (
                f"form not lowercase/non-empty for base={base!r}: {form!r}"
            )


# -- draw_rounds (AC2, AC3) --------------------------------------------------


def test_draw_rounds_is_deterministic_with_seeded_rng() -> None:  # AC2
    a = iv.draw_rounds(iv.IRREGULAR_VERBS, n_max=10, rng=random.Random(42))
    b = iv.draw_rounds(iv.IRREGULAR_VERBS, n_max=10, rng=random.Random(42))
    assert a == b, "same seed must produce identical samples"
    expected_n = min(10, len(iv.IRREGULAR_VERBS))
    assert len(a) == expected_n, (
        f"expected {expected_n} rounds for n_max=10, got {len(a)}"
    )
    bases = [r.base for r in a]
    assert len(set(bases)) == len(bases), (
        f"sampled bases must be unique (sampled without replacement); got {bases}"
    )


def test_draw_rounds_n_max_greater_than_pool() -> None:  # AC3 — n_max > len(verbs) caps at len(verbs)
    pool = [
        ("go", ["went"], ["gone"]),
        ("see", ["saw"], ["seen"]),
        ("be", ["was", "were"], ["been"]),
    ]
    rounds = iv.draw_rounds(pool, n_max=99, rng=random.Random(0))
    assert len(rounds) == len(pool), (
        f"expected {len(pool)} rounds when n_max>pool, got {len(rounds)}"
    )


def test_draw_rounds_n_max_equals_pool() -> None:  # edge: n_max == len(verbs)
    pool = [
        ("go", ["went"], ["gone"]),
        ("see", ["saw"], ["seen"]),
    ]
    rounds = iv.draw_rounds(pool, n_max=2, rng=random.Random(0))
    assert len(rounds) == 2


def test_draw_rounds_n_max_one() -> None:  # edge: n_max == 1
    rounds = iv.draw_rounds(iv.IRREGULAR_VERBS, n_max=1, rng=random.Random(0))
    assert len(rounds) == 1


def test_draw_rounds_n_max_zero_raises() -> None:  # AC3, error: n_max == 0 → ValueError
    with pytest.raises(ValueError):
        iv.draw_rounds(iv.IRREGULAR_VERBS, n_max=0, rng=random.Random(0))


def test_draw_rounds_n_max_negative_raises() -> None:  # AC3, error: n_max < 0 → ValueError
    with pytest.raises(ValueError):
        iv.draw_rounds(iv.IRREGULAR_VERBS, n_max=-1, rng=random.Random(0))


def test_draw_rounds_empty_pool_raises() -> None:  # AC3, error: empty pool → ValueError
    with pytest.raises(ValueError):
        iv.draw_rounds([], n_max=10, rng=random.Random(0))


# -- grade_answer correct paths (AC4) ----------------------------------------


def test_grade_answer_canonical_correct() -> None:  # AC4 — slash-with-spaces canonical input
    ok, display = iv.grade_answer("went / gone", _round())
    assert ok is True, "canonical 'went / gone' against go(went,gone) must be correct"
    assert display == "went / gone", f"expected display 'went / gone', got {display!r}"


def test_grade_answer_uppercase_no_spaces() -> None:  # AC4, edge: mixed case 'WENT/GONE'
    ok, display = iv.grade_answer("WENT/GONE", _round())
    assert ok is True
    assert display == "went / gone", (
        f"display must be lowercased canonical form, got {display!r}"
    )


def test_grade_answer_comma_with_padding() -> None:  # AC4, edge: '  went,gone '
    ok, display = iv.grade_answer("  went,gone ", _round())
    assert ok is True
    assert display == "went / gone"


def test_grade_answer_whitespace_only_separator() -> None:  # AC4, edge: 'went   gone'
    ok, display = iv.grade_answer("went   gone", _round())
    assert ok is True
    assert display == "went / gone"


# -- grade_answer non-2-token paths (AC5) ------------------------------------


def test_grade_answer_empty_string_returns_none() -> None:  # AC5, edge: empty input
    assert iv.grade_answer("", _round()) == (False, None)


def test_grade_answer_single_token_returns_none() -> None:  # AC5, edge: single token
    assert iv.grade_answer("went", _round()) == (False, None)


def test_grade_answer_three_tokens_returns_none() -> None:  # AC5, edge: three+ tokens
    assert iv.grade_answer("went / gone / extra", _round()) == (False, None)


def test_grade_answer_slash_only_returns_none() -> None:  # AC5, edge: '/'
    assert iv.grade_answer("/", _round()) == (False, None)


def test_grade_answer_comma_only_returns_none() -> None:  # AC5, edge: ','
    assert iv.grade_answer(",", _round()) == (False, None)


# -- grade_answer alternates (AC6) -------------------------------------------


def test_grade_answer_accepts_either_alternate() -> None:  # AC6 — both alts of multi-alt verb accepted
    base, ps_alts, pp_alts = _verb_with_multi_alts()
    rd = iv.Round(base=base, past_simple_alts=ps_alts, past_participle_alts=pp_alts)
    # try every (ps, pp) combo built from the alt lists; all must grade correct
    for ps in ps_alts:
        for pp in pp_alts:
            ok, _ = iv.grade_answer(f"{ps} / {pp}", rd)
            assert ok is True, (
                f"alternate ({ps!r}, {pp!r}) for base={base!r} must grade correct "
                f"(ps_alts={ps_alts}, pp_alts={pp_alts})"
            )


def test_grade_answer_alts_length_one() -> None:  # edge: alt list of length 1
    rd = _round("go", ["went"], ["gone"])
    ok, display = iv.grade_answer("went / gone", rd)
    assert ok is True
    assert display == "went / gone"


# -- grade_answer parsed-but-wrong (AC7) -------------------------------------


def test_grade_answer_parsed_but_wrong() -> None:  # AC7 — two tokens, neither matches alts
    ok, display = iv.grade_answer("goed / goned", _round("go", ["went"], ["gone"]))
    assert ok is False, "non-matching forms must grade False"
    assert display == "goed / goned", (
        f"parsed display must echo (lowercased) what the user typed, got {display!r}"
    )


# -- apply_answer (AC8) ------------------------------------------------------


def _three_round_game() -> iv.Game:
    rounds = [
        _round("go", ["went"], ["gone"]),
        _round("see", ["saw"], ["seen"]),
        _round("be", ["was", "were"], ["been"]),
    ]
    return iv.Game(chat_id=1, rounds=rounds)


def test_apply_answer_correct_increments_and_advances() -> None:  # AC8 — correct path
    g = _three_round_game()
    iv.apply_answer(g, True)
    assert g.score == 1, f"score should be 1 after one correct, got {g.score}"
    assert g.current_round == 1, (
        f"current_round should advance to 1, got {g.current_round}"
    )


def test_apply_answer_wrong_advances_only() -> None:  # AC8 — wrong path
    g = _three_round_game()
    iv.apply_answer(g, False)
    assert g.score == 0, f"score must remain 0 on wrong, got {g.score}"
    assert g.current_round == 1, (
        f"current_round must advance even on wrong, got {g.current_round}"
    )


def test_apply_answer_score_bounded() -> None:  # AC8 — score in [0, n_rounds]
    g = _three_round_game()
    iv.apply_answer(g, True)
    iv.apply_answer(g, False)
    iv.apply_answer(g, True)
    assert 0 <= g.score <= 3, f"score {g.score} out of [0, 3]"
    assert g.score == 2, f"expected score=2 after T,F,T sequence, got {g.score}"


def test_apply_answer_on_done_is_noop() -> None:  # AC8, edge: done game → no field change
    g = iv.Game(
        chat_id=1,
        rounds=[_round("go", ["went"], ["gone"])],
        score=1,
        current_round=1,
    )
    assert g.done is True, "fixture precondition: game must be done"
    iv.apply_answer(g, True)
    assert g.score == 1, "score must not change when game is already done"
    assert g.current_round == 1, "current_round must not advance past n_rounds"
    iv.apply_answer(g, False)
    assert g.score == 1
    assert g.current_round == 1


# -- format_result (AC9) -----------------------------------------------------


def test_format_result_exact_string() -> None:  # AC9 — exact byte string
    assert iv.format_result(7, 10) == "🎯 You scored 7/10"


def test_format_result_zero_score() -> None:  # AC9 — example: 0/10
    assert iv.format_result(0, 10) == "🎯 You scored 0/10"
