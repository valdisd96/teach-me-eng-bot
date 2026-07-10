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


def test_blank_story_numbers_first_occurrence_and_masks_duplicates() -> None:
    # The first occurrence gets the numbered blank; further occurrences are
    # masked with an unnumbered blank so they can't leak the answer.
    display, order, missing = cloze.blank_story("A frog met a frog.", ["frog"])
    assert missing == []
    assert display == "A ___(1) met a ___."
    assert order == [0]


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


def test_grade_answer_strips_surrounding_punctuation() -> None:
    # A typed answer may carry sentence punctuation — "cat." still means cat.
    assert cloze.grade_answer("cat.", "cat") is True
    assert cloze.grade_answer('"cat"', "cat") is True
    assert cloze.grade_answer("cat!?", "cat") is True


def test_grade_answer_keeps_inner_apostrophe() -> None:
    # Only *surrounding* punctuation is stripped — the apostrophe inside
    # "didn't" must survive.
    assert cloze.grade_answer("didn't!", "didn't") is True
    assert cloze.grade_answer("didn't", "didnt") is False


def test_resolve_answers_tolerates_trailing_punctuation() -> None:
    s = _session(["cat"], "A cat sat.")
    assert cloze.resolve_answers("cat.", s) == [
        cloze.Answer(blank_index=0, text="cat.", is_skip=False)
    ]


def test_resolve_answers_bare_question_mark_is_still_skip() -> None:
    # "?" is checked on the raw text — the punctuation-tolerant normalizer
    # would strip it to "" and lose the skip intent.
    s = _session(["cat"], "A cat sat.")
    assert cloze.resolve_answers("?", s) == [
        cloze.Answer(blank_index=0, text="", is_skip=True)
    ]


def test_resolve_answers_numbered_targets_specific_blank() -> None:
    s = _session(["alpha", "beta", "gamma"], "First alpha then beta then gamma.")
    for text in ("2 beta", "2: beta", "2. beta", "2) beta"):
        assert cloze.resolve_answers(text, s) == [
            cloze.Answer(blank_index=1, text="beta", is_skip=False)
        ], text


def test_resolve_answers_numbered_skip() -> None:
    s = _session(["alpha", "beta"], "First alpha then beta.")
    assert cloze.resolve_answers("2 skip", s) == [
        cloze.Answer(blank_index=1, text="", is_skip=True)
    ]


def test_resolve_answers_batch_fills_in_order() -> None:
    s = _session(["alpha", "beta", "gamma"], "First alpha then beta then gamma.")
    assert cloze.resolve_answers("alpha, gamma, beta", s) == [
        cloze.Answer(blank_index=0, text="alpha", is_skip=False),
        cloze.Answer(blank_index=1, text="gamma", is_skip=False),
        cloze.Answer(blank_index=2, text="beta", is_skip=False),
    ]


def test_resolve_answers_mixed_numbered_and_bare() -> None:
    # `2 beta` claims blank 2, so the bare answer flows to the first
    # still-free blank (1); skip works inside a batch.
    s = _session(["alpha", "beta", "gamma"], "First alpha then beta then gamma.")
    assert cloze.resolve_answers("2 beta, alpha, skip", s) == [
        cloze.Answer(blank_index=1, text="beta", is_skip=False),
        cloze.Answer(blank_index=0, text="alpha", is_skip=False),
        cloze.Answer(blank_index=2, text="", is_skip=True),
    ]


def test_resolve_answers_rejects_whole_message_on_any_bad_segment() -> None:
    s = _session(["alpha", "beta"], "First alpha then beta.")
    # One stray segment poisons the batch — nothing may be graded.
    assert cloze.resolve_answers("alpha, huh what", s) is None
    assert cloze.resolve_answers("alpha, betaz", s) is None


def test_resolve_answers_rejects_out_of_range_and_answered_targets() -> None:
    s = _session(["alpha", "beta"], "First alpha then beta.")
    assert cloze.resolve_answers("3 alpha", s) is None  # no blank (3)
    assert cloze.resolve_answers("0 alpha", s) is None
    assert cloze.resolve_answers("1 alpha, 1 beta", s) is None  # duplicate
    cloze.apply_answer(s, 0, True)
    assert cloze.resolve_answers("1 alpha", s) is None  # already answered


def test_resolve_answers_rejects_more_words_than_open_blanks() -> None:
    s = _session(["alpha", "beta"], "First alpha then beta.")
    assert cloze.resolve_answers("alpha, beta, alpha", s) is None


def test_resolve_answers_stray_text_is_none() -> None:
    s = _session(["alpha"], "First alpha here.")
    assert cloze.resolve_answers("hi, what does alpha mean??", s) is None


# --- session state ----------------------------------------------------------------


def test_apply_answer_advances_and_tracks_wrong() -> None:
    s = _session(["alpha", "beta"], "First alpha then beta.")
    assert s.done is False
    assert s.current().word == "alpha"
    cloze.apply_answer(s, 0, True)
    assert s.score == 1 and s.wrong == []
    assert s.current().word == "beta"
    cloze.apply_answer(s, 1, False)
    assert s.done is True
    assert s.score == 1
    assert s.wrong == ["beta"]


def test_apply_answer_out_of_order() -> None:
    s = _session(["alpha", "beta", "gamma"], "First alpha then beta then gamma.")
    cloze.apply_answer(s, 1, True)
    assert s.remaining == [0, 2]
    assert s.current().word == "alpha"  # lowest open blank
    cloze.apply_answer(s, 2, False)
    assert s.current().word == "alpha"
    assert s.done is False
    cloze.apply_answer(s, 0, True)
    assert s.done is True
    assert s.score == 2 and s.wrong == ["gamma"]


def test_apply_answer_rejects_double_answer() -> None:
    s = _session(["alpha", "beta"], "First alpha then beta.")
    cloze.apply_answer(s, 0, True)
    try:
        cloze.apply_answer(s, 0, False)
    except ValueError:
        pass
    else:
        raise AssertionError("answering the same blank twice must raise")


# --- formatting ------------------------------------------------------------------


def test_format_session_message_contains_blanks_bank_and_prompt() -> None:
    s = _session(["alpha", "beta"], "First alpha then beta.")
    body = cloze.format_session_message(s, rng=random.Random(0))
    assert "___(1)" in body and "___(2)" in body
    assert "Word bank:" in body
    assert "alpha" in body and "beta" in body
    assert "Blanks left: (1), (2)" in body
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
    cloze.apply_answer(s, 0, True)
    cloze.apply_answer(s, 1, False)
    out = cloze.format_result(s)
    assert "<b>alpha</b>" in out and "<b>beta</b>" in out  # completed story, bolded
    assert "1/2" in out
    assert "бета" in out  # missed word carries its translation
    assert "❌ <b>beta</b>" in out


def test_format_result_bolds_every_occurrence_with_bare_infinitive() -> None:
    # Bolding reuses blank_story's span discovery: the "to X" entry matches
    # its bare form, and *every* occurrence is bolded — including the
    # duplicate that was masked with an unnumbered blank.
    story = "She may run out of milk. He may run out of luck."
    s = _session(["to run out of"], story)
    cloze.apply_answer(s, 0, True)
    out = cloze.format_result(s)
    assert out.count("<b>run out of</b>") == 2, (
        f"both bare-form occurrences must be bolded; got {out!r}"
    )


def test_format_answer_feedback() -> None:
    assert cloze.format_answer_feedback(1, True, "alpha") == "(1) ✅ alpha"
    assert cloze.format_answer_feedback(3, False, "alpha") == "(3) ❌ it was: alpha"


def test_format_blank_prompt_lists_open_blanks_and_syntax_html_safe() -> None:
    s = _session(["alpha", "beta", "gamma"], "First alpha then beta then gamma.")
    p = cloze.format_blank_prompt(s)
    assert "(1), (2), (3)" in p
    assert "`1 &lt;word&gt;`" in p and "`1 <word>`" not in p and "skip" in p
    cloze.apply_answer(s, 1, True)  # out-of-order: blank 2 answered first
    p = cloze.format_blank_prompt(s)
    assert "Blanks left: (1), (3)." in p


def test_format_blank_prompt_single_open_blank_is_simple() -> None:
    s = _session(["alpha", "beta"], "First alpha then beta.")
    cloze.apply_answer(s, 0, True)
    assert "(2) of 2" in cloze.format_blank_prompt(s)
