"""Tests for games.py — pure round/answer/score helpers (issue #65).

Bot-side dispatch (cmd_games, on_games_menu, on_game_answer) is verified at
the static-surface level only (constants, COMMANDS, module-level games dict),
matching the project's existing test_help.py convention. Dispatch-level edges
listed in the spec (stale-tap, post-restart orphan, malformed callback data)
are out of reach without telegram Update/Context mocking, which the project
does not currently set up.
"""

from __future__ import annotations

import os
import random
import sqlite3

import pytest

import games

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import bot  # noqa: E402  (env var must be set first)


# -- helpers -----------------------------------------------------------------


def _translatable_pool(n: int) -> list[tuple[int, str, str]]:
    return [(i, f"word{i}", f"trans{i}") for i in range(1, n + 1)]


def _make_round(
    word_id: int = 1,
    prompt: str = "cat",
    correct: str = "кошка",
    options: list[str] | None = None,
    correct_index: int = 0,
) -> games.Round:
    if options is None:
        options = [correct, "собака", "птица", "рыба"]
    return games.Round(
        word_id=word_id,
        prompt=prompt,
        correct_answer=correct,
        options=options,
        correct_index=correct_index,
    )


# -- format_result -----------------------------------------------------------


def test_format_result_basic_example() -> None:  # AC6 — example from spec
    assert games.format_result(7, 10) == "🎯 You scored 7/10"


def test_format_result_zero_example() -> None:  # AC6 — example from spec
    assert games.format_result(0, 4) == "🎯 You scored 0/4"


def test_format_result_perfect() -> None:  # AC6 — score == n_rounds path
    assert games.format_result(4, 4) == "🎯 You scored 4/4"


# -- apply_answer ------------------------------------------------------------


def test_apply_answer_correct_increments_score_and_advances() -> None:  # AC5
    g = games.Game(chat_id=1, rounds=[_make_round(correct_index=2)])
    result = games.apply_answer(g, 2)
    assert result is True
    assert g.score == 1, f"score should be 1 after correct, got {g.score}"
    assert g.current_round == 1, (
        f"current_round should advance to 1, got {g.current_round}"
    )


def test_apply_answer_wrong_advances_only() -> None:  # AC5
    g = games.Game(chat_id=1, rounds=[_make_round(correct_index=2)])
    result = games.apply_answer(g, 0)
    assert result is False
    assert g.score == 0, f"score should remain 0 after wrong, got {g.score}"
    assert g.current_round == 1, (
        f"current_round must still advance on wrong tap, got {g.current_round}"
    )


def test_apply_answer_returns_true_on_correct() -> None:  # AC5 — bool return
    g = games.Game(chat_id=1, rounds=[_make_round(correct_index=1)])
    assert games.apply_answer(g, 1) is True


def test_apply_answer_returns_false_on_wrong() -> None:  # AC5 — bool return
    g = games.Game(chat_id=1, rounds=[_make_round(correct_index=1)])
    assert games.apply_answer(g, 3) is False


def test_apply_answer_noop_when_done() -> None:  # edge: tap on done game → no-op, returns False
    g = games.Game(
        chat_id=1, rounds=[_make_round(correct_index=0)], score=1, current_round=1
    )
    assert g.done is True
    result = games.apply_answer(g, 0)
    assert result is False, "apply_answer on a done game must return False"
    assert g.score == 1, "score must not change when game is already done"
    assert g.current_round == 1, "current_round must not advance past n_rounds"


def test_apply_answer_through_full_game() -> None:  # AC5 — sequence builds score
    rounds = [
        _make_round(word_id=1, correct_index=0),
        _make_round(word_id=2, correct_index=1),
        _make_round(word_id=3, correct_index=2),
    ]
    g = games.Game(chat_id=1, rounds=rounds)
    assert games.apply_answer(g, 0) is True   # +1 → score 1
    assert games.apply_answer(g, 0) is False  # wrong, score unchanged
    assert games.apply_answer(g, 2) is True   # +1 → score 2
    assert g.score == 2, f"expected score=2, got {g.score}"
    assert g.current_round == 3
    assert g.done is True


# -- draw_rounds: count / pool ----------------------------------------------


def test_draw_rounds_count_capped_at_max() -> None:  # AC3 — N ≥ 10 → n_rounds = MAX_ROUNDS
    rounds = games.draw_rounds(_translatable_pool(15), rng=random.Random(0))
    assert len(rounds) == games.MAX_ROUNDS, (
        f"expected {games.MAX_ROUNDS} rounds for pool of 15, got {len(rounds)}"
    )


def test_draw_rounds_count_below_max_uses_pool() -> None:  # AC3 — 4 ≤ N < 10 → n_rounds = N
    rounds = games.draw_rounds(_translatable_pool(7), rng=random.Random(0))
    assert len(rounds) == 7, f"expected 7 rounds for pool of 7, got {len(rounds)}"


def test_draw_rounds_at_min_vocab_yields_min_rounds() -> None:  # edge: exactly 4 rows → 4 rounds; distractor pool = 3
    rounds = games.draw_rounds(
        _translatable_pool(games.MIN_VOCAB), rng=random.Random(0)
    )
    assert len(rounds) == games.MIN_VOCAB, (
        f"expected {games.MIN_VOCAB} rounds, got {len(rounds)}"
    )
    # With unique translations and exactly 4 rows, the per-round distractor
    # pool is the other 3 chosen rows — and all 4 option strings are distinct.
    for r in rounds:
        assert len(r.options) == games.N_OPTIONS
        assert len(set(r.options)) == games.N_OPTIONS, (
            f"options should be distinct when pool translations are unique; got {r.options}"
        )


def test_draw_rounds_n_max_argument_caps_count() -> None:  # public API — n_max kwarg respected
    rounds = games.draw_rounds(
        _translatable_pool(15), n_max=5, rng=random.Random(0)
    )
    assert len(rounds) == 5


# -- draw_rounds: round shape (AC4) -----------------------------------------


def test_draw_rounds_each_round_has_four_options() -> None:  # AC4 — exactly N_OPTIONS buttons
    rounds = games.draw_rounds(_translatable_pool(8), rng=random.Random(1))
    for r in rounds:
        assert len(r.options) == games.N_OPTIONS, (
            f"round word_id={r.word_id}: expected {games.N_OPTIONS} options, got {len(r.options)}"
        )


def test_draw_rounds_correct_index_in_valid_range() -> None:  # AC4 — 0 ≤ correct_index < N_OPTIONS
    rounds = games.draw_rounds(_translatable_pool(6), rng=random.Random(2))
    for r in rounds:
        assert 0 <= r.correct_index < games.N_OPTIONS, (
            f"correct_index {r.correct_index} out of range [0, {games.N_OPTIONS})"
        )


def test_draw_rounds_options_at_correct_index_matches_translation() -> None:  # AC4 — invariant
    rounds = games.draw_rounds(_translatable_pool(6), rng=random.Random(3))
    for r in rounds:
        assert r.options[r.correct_index] == r.correct_answer, (
            f"options[{r.correct_index}]={r.options[r.correct_index]!r} "
            f"!= correct_answer={r.correct_answer!r}"
        )


def test_draw_rounds_distractors_drawn_from_other_round_words() -> None:  # AC4 — from chosen-pool minus self
    rounds = games.draw_rounds(_translatable_pool(8), rng=random.Random(4))
    chosen_translations = {r.correct_answer for r in rounds}
    for r in rounds:
        for opt in r.options:
            if opt == r.correct_answer:
                continue
            assert opt in chosen_translations, (
                f"distractor {opt!r} is not from the chosen-round pool {chosen_translations}"
            )


def test_draw_rounds_distractors_unique_within_round() -> None:  # AC4 — without-replacement within round
    # With unique translations across the pool, every option must be distinct.
    rounds = games.draw_rounds(_translatable_pool(8), rng=random.Random(5))
    for r in rounds:
        assert len(set(r.options)) == games.N_OPTIONS, (
            f"options not distinct within round: {r.options}"
        )


def test_draw_rounds_correct_words_unique_across_rounds() -> None:  # AC3 — sampled without replacement
    rounds = games.draw_rounds(_translatable_pool(10), rng=random.Random(6))
    word_ids = [r.word_id for r in rounds]
    assert len(word_ids) == len(set(word_ids)), (
        f"correct words must not repeat across rounds; got {word_ids}"
    )


# -- draw_rounds: filtering / errors ----------------------------------------


def test_draw_rounds_skips_none_translation() -> None:  # edge: translation IS NULL → excluded
    rows = _translatable_pool(4) + [(99, "ghost", None)]
    rounds = games.draw_rounds(rows, rng=random.Random(7))
    assert 99 not in {r.word_id for r in rounds}, (
        "row with None translation must be filtered out"
    )
    assert len(rounds) == 4


def test_draw_rounds_skips_whitespace_only_translation() -> None:  # edge: whitespace-only excluded
    rows = _translatable_pool(4) + [(99, "blank", "   ")]
    rounds = games.draw_rounds(rows, rng=random.Random(8))
    assert 99 not in {r.word_id for r in rounds}, (
        "whitespace-only translation must be filtered out"
    )


def test_draw_rounds_skips_empty_string_translation() -> None:  # edge: empty string excluded
    rows = _translatable_pool(4) + [(99, "blank", "")]
    rounds = games.draw_rounds(rows, rng=random.Random(9))
    assert 99 not in {r.word_id for r in rounds}, (
        "empty-string translation must be filtered out"
    )


def test_draw_rounds_too_few_translatable_raises() -> None:  # error: pool < MIN_VOCAB → ValueError
    with pytest.raises(ValueError):
        games.draw_rounds(_translatable_pool(3), rng=random.Random(0))


def test_draw_rounds_zero_translatable_raises() -> None:  # error: empty pool → ValueError
    with pytest.raises(ValueError):
        games.draw_rounds([], rng=random.Random(0))


def test_draw_rounds_filter_then_too_few_raises() -> None:  # error: many input rows but post-filter < MIN_VOCAB
    rows = [
        (1, "a", "tr1"),
        (2, "b", "tr2"),
        (3, "c", None),
        (4, "d", "   "),
        (5, "e", ""),
    ]
    with pytest.raises(ValueError):
        games.draw_rounds(rows, rng=random.Random(0))


# -- draw_rounds: input shape ------------------------------------------------


def test_draw_rounds_accepts_id_text_translation_tuples() -> None:  # public API — tuple input
    rounds = games.draw_rounds(_translatable_pool(4), rng=random.Random(10))
    assert len(rounds) == 4


def test_draw_rounds_accepts_sqlite_row(conn: sqlite3.Connection) -> None:  # public API — sqlite3.Row input
    conn.execute(
        "CREATE TABLE _g (id INTEGER PRIMARY KEY, text TEXT, translation TEXT)"
    )
    for i in range(1, 6):
        conn.execute(
            "INSERT INTO _g(id, text, translation) VALUES (?, ?, ?)",
            (i, f"word{i}", f"trans{i}"),
        )
    rows = conn.execute("SELECT id, text, translation FROM _g").fetchall()
    assert isinstance(rows[0], sqlite3.Row), "expected sqlite3.Row from fetchall"

    rounds = games.draw_rounds(rows, rng=random.Random(11))
    assert len(rounds) == 5
    for r in rounds:
        assert r.options[r.correct_index] == r.correct_answer


def test_draw_rounds_sqlite_row_skips_none_translation(
    conn: sqlite3.Connection,
) -> None:  # edge: None via sqlite path
    conn.execute(
        "CREATE TABLE _g (id INTEGER PRIMARY KEY, text TEXT, translation TEXT)"
    )
    rows_in = list(_translatable_pool(4)) + [(99, "ghost", None)]
    for rid, text, translation in rows_in:
        conn.execute(
            "INSERT INTO _g(id, text, translation) VALUES (?, ?, ?)",
            (rid, text, translation),
        )
    rows = conn.execute("SELECT id, text, translation FROM _g").fetchall()
    rounds = games.draw_rounds(rows, rng=random.Random(12))
    assert 99 not in {r.word_id for r in rounds}


# -- draw_rounds: duplicates / determinism -----------------------------------


def test_draw_rounds_duplicate_translations_dont_break_correct_index() -> None:  # edge: duplicate translation strings
    # couch and sofa share "диван"; correct_index must still point at the
    # actual correct row's slot, even if "диван" surfaces as a distractor too.
    rows = [
        (1, "couch", "диван"),
        (2, "sofa", "диван"),
        (3, "cat", "кошка"),
        (4, "dog", "собака"),
        (5, "bird", "птица"),
    ]
    for seed in range(20):
        rounds = games.draw_rounds(rows, rng=random.Random(seed))
        for r in rounds:
            assert r.options[r.correct_index] == r.correct_answer, (
                f"seed={seed}, word_id={r.word_id}: "
                f"options[{r.correct_index}]={r.options[r.correct_index]!r} "
                f"!= correct={r.correct_answer!r}; options={r.options}"
            )
            assert len(r.options) == games.N_OPTIONS


def test_draw_rounds_uses_provided_rng_deterministically() -> None:  # public API — rng arg is the only randomness source
    pool = _translatable_pool(8)
    a = games.draw_rounds(pool, rng=random.Random(123))
    b = games.draw_rounds(pool, rng=random.Random(123))
    assert [r.word_id for r in a] == [r.word_id for r in b]
    assert [r.options for r in a] == [r.options for r in b]
    assert [r.correct_index for r in a] == [r.correct_index for r in b]


def test_draw_rounds_default_rng_produces_valid_rounds() -> None:  # public API — rng=None default
    rounds = games.draw_rounds(_translatable_pool(6))
    assert len(rounds) == 6
    for r in rounds:
        assert len(r.options) == games.N_OPTIONS
        assert r.options[r.correct_index] == r.correct_answer


# -- Round / Game dataclass invariants (AC7) ---------------------------------


def test_round_dataclass_has_required_fields() -> None:  # AC7 — Round shape
    r = games.Round(
        word_id=1,
        prompt="hello",
        correct_answer="привет",
        options=["привет", "пока", "да", "нет"],
        correct_index=0,
    )
    assert r.word_id == 1
    assert r.prompt == "hello"
    assert r.correct_answer == "привет"
    assert r.options == ["привет", "пока", "да", "нет"]
    assert r.correct_index == 0


def test_game_dataclass_has_required_fields_and_defaults() -> None:  # AC7 — Game shape
    r = _make_round()
    g = games.Game(chat_id=42, rounds=[r])
    assert g.chat_id == 42
    assert g.rounds == [r]
    assert g.score == 0, "score default must be 0"
    assert g.current_round == 0, "current_round default must be 0"


def test_game_n_rounds_property() -> None:  # AC7 — n_rounds property
    rounds = [_make_round(word_id=i) for i in (1, 2, 3)]
    g = games.Game(chat_id=1, rounds=rounds)
    assert g.n_rounds == 3


def test_game_done_property_false_before_completion() -> None:  # AC7 — done property
    g = games.Game(
        chat_id=1, rounds=[_make_round(), _make_round()], current_round=0
    )
    assert g.done is False
    g.current_round = 1
    assert g.done is False


def test_game_done_property_true_at_completion() -> None:  # AC7 — done property
    g = games.Game(
        chat_id=1, rounds=[_make_round(), _make_round()], current_round=2
    )
    assert g.done is True


def test_game_current_returns_round_at_index() -> None:  # AC7 — current() method
    r0 = _make_round(word_id=10)
    r1 = _make_round(word_id=20)
    g = games.Game(chat_id=1, rounds=[r0, r1], current_round=1)
    assert g.current() is r1


# -- module surface (AC7) ----------------------------------------------------


def test_module_constants_match_spec() -> None:  # AC7 — MIN_VOCAB / N_OPTIONS / MAX_ROUNDS
    assert games.MIN_VOCAB == 4
    assert games.N_OPTIONS == 4
    assert games.MAX_ROUNDS == 10


def test_module_exposes_public_symbols() -> None:  # AC7 — Round / Game / draw_rounds / apply_answer / format_result
    for sym in ("Round", "Game", "draw_rounds", "apply_answer", "format_result"):
        assert hasattr(games, sym), f"games.py must expose {sym!r}"


def test_module_has_no_telegram_imports() -> None:  # AC7 — pure module, no telegram coupling
    with open(games.__file__, encoding="utf-8") as f:
        src = f.read()
    assert "import telegram" not in src, (
        "games.py must not `import telegram` (must remain a pure module)"
    )
    assert "from telegram" not in src, (
        "games.py must not `from telegram import …` (must remain a pure module)"
    )


# -- bot wiring (constants / COMMANDS — AC1, AC2, AC6, AC8) -------------------


def test_bot_games_need_vocab_text() -> None:  # AC1 — refusal text constant
    assert bot.GAMES_NEED_VOCAB == "add at least 4 words to your vocab first"


def test_bot_games_in_progress_text() -> None:  # AC8 — already-in-progress text
    assert bot.GAMES_IN_PROGRESS == "you have a game in progress"


def test_bot_commands_includes_games() -> None:  # AC1 — /games is a registered slash command
    names = {name for name, _ in bot.COMMANDS}
    assert "games" in names, f"COMMANDS missing 'games'; got {sorted(names)}"


def test_bot_exposes_games_in_flight_dict() -> None:  # AC6 + AC8 — module-level per-chat dict
    assert hasattr(bot, "games"), "bot must expose a module-level `games` mapping"
    assert isinstance(bot.games, dict), (
        f"bot.games must be a dict, got {type(bot.games).__name__}"
    )
