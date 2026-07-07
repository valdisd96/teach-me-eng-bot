"""Tests for cloze.py — blanking, grading, session state, formatting."""

from __future__ import annotations

import random

import cloze


def _session(words: list[str], story: str, *, intro: set[str] = frozenset()) -> cloze.Session:
    display, order, missing = cloze.blank_story(story, words)
    assert not missing, f"test story must contain every word; missing {missing}"
    blanks = [
        cloze.Blank(word_id=i + 1, word=words[i], is_intro=words[i] in intro)
        for i in order
    ]
    return cloze.Session(chat_id=1, story=story, display=display, blanks=blanks)


# --- blank_story --------------------------------------------------------------


def test_blank_story_numbers_blanks_in_order_of_appearance() -> None:
    display, order, missing = cloze.blank_story(
        "The placid lake hid an ephemeral mist.", ["ephemeral", "placid"]
    )
    assert missing == []
    assert display == "The ___(1) lake hid an ___(2) mist."
    # order maps blank number → index into the input words list.
    assert order == [1, 0]


def test_blank_story_is_case_insensitive() -> None:
    display, order, missing = cloze.blank_story("Ephemeral things fade.", ["ephemeral"])
    assert missing == []
    assert display == "___(1) things fade."


def test_blank_story_reports_missing_words() -> None:
    display, order, missing = cloze.blank_story("Nothing here.", ["ephemeral", "placid"])
    assert missing == ["ephemeral", "placid"]
    assert order == []
    assert display == "Nothing here."


def test_blank_story_handles_phrases() -> None:
    display, order, missing = cloze.blank_story(
        "I ran out of milk today.", ["ran out of"]
    )
    assert missing == []
    assert display == "I ___(1) milk today."


def test_blank_story_word_boundary_no_substring_match() -> None:
    # "cat" must not match inside "catch".
    display, order, missing = cloze.blank_story("I catch the ball.", ["cat"])
    assert missing == ["cat"]


def test_blank_story_phrase_not_shadowed_by_subword() -> None:
    # Longer words match first, so "run out of" isn't broken by "run".
    display, order, missing = cloze.blank_story(
        "They run out of time when they run.", ["run", "run out of"]
    )
    assert missing == []
    assert display == "They ___(1) time when they ___(2)."
    assert order == [1, 0]


def test_blank_story_accepts_bare_form_of_to_phrase() -> None:
    # "to run out of" stored, story uses the bare "ran"-less form without "to".
    display, order, missing = cloze.blank_story(
        "We run out of milk.", ["to run out of"]
    )
    assert missing == []
    assert display == "We ___(1) milk."


def test_blank_story_blanks_only_first_occurrence() -> None:
    display, _, missing = cloze.blank_story("A frog met a frog.", ["frog"])
    assert missing == []
    assert display == "A ___(1) met a frog."


# --- grade_answer ---------------------------------------------------------------


def test_grade_answer_exact_and_case_insensitive() -> None:
    assert cloze.grade_answer("Ephemeral", "ephemeral") is True
    assert cloze.grade_answer("  ephemeral  ", "ephemeral") is True
    assert cloze.grade_answer("placid", "ephemeral") is False


def test_grade_answer_optional_leading_to() -> None:
    assert cloze.grade_answer("run out of", "to run out of") is True
    assert cloze.grade_answer("to run out of", "run out of") is True


def test_grade_answer_collapses_inner_whitespace() -> None:
    assert cloze.grade_answer("run  out   of", "to run out of") is True


# --- session state ----------------------------------------------------------------


def test_apply_answer_advances_and_tracks_wrong() -> None:
    s = _session(["alpha", "beta"], "First alpha then beta.")
    assert s.done is False
    assert s.current().word == "alpha"
    cloze.apply_answer(s, True)
    assert s.score == 1 and s.wrong == []
    assert s.current().word == "beta"
    cloze.apply_answer(s, False)
    assert s.done is True
    assert s.score == 1
    assert s.wrong == ["beta"]


# --- formatting ------------------------------------------------------------------


def test_format_session_message_contains_blanks_bank_and_prompt() -> None:
    s = _session(["alpha", "beta"], "First alpha then beta.")
    body = cloze.format_session_message(s, rng=random.Random(0))
    assert "___(1)" in body and "___(2)" in body
    assert "Word bank:" in body
    assert "alpha" in body and "beta" in body
    assert "blank (1)" in body
    # No intro words → no 🆕 section.
    assert "🆕" not in body


def test_format_session_message_lists_intro_words_with_translation() -> None:
    s = _session(["alpha", "beta"], "First alpha then beta.", intro={"alpha"})
    s.blanks[0].translation = "альфа"
    body = cloze.format_session_message(s, rng=random.Random(0))
    assert "🆕" in body
    assert "альфа" in body


def test_format_session_message_escapes_html() -> None:
    s = _session(["a<b"], "Compare a<b now.")
    body = cloze.format_session_message(s, rng=random.Random(0))
    assert "a&lt;b" in body
    assert "a<b" not in body


def test_format_result_shows_story_score_and_missed_translations() -> None:
    s = _session(["alpha", "beta"], "First alpha then beta.")
    s.blanks[1].translation = "бета"
    cloze.apply_answer(s, True)
    cloze.apply_answer(s, False)
    out = cloze.format_result(s)
    assert "<b>alpha</b>" in out and "<b>beta</b>" in out  # completed story, bolded
    assert "1/2" in out
    assert "бета" in out  # missed word carries its translation
    assert "❌ <b>beta</b>" in out


def test_format_answer_feedback() -> None:
    assert cloze.format_answer_feedback(True, "alpha") == "✅ alpha"
    assert cloze.format_answer_feedback(False, "alpha") == "❌ it was: alpha"


def test_format_blank_prompt_counts_from_one() -> None:
    s = _session(["alpha", "beta"], "First alpha then beta.")
    assert "(1) of 2" in cloze.format_blank_prompt(s)
    cloze.apply_answer(s, True)
    assert "(2) of 2" in cloze.format_blank_prompt(s)
