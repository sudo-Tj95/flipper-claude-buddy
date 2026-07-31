#!/usr/bin/env python3
"""What the Flipper shows for a permission prompt.

Focus: AskUserQuestion. It DOES reach the device — it goes through the
PermissionRequest hook like any other tool — but upstream's extract_detail has
no branch for it, so it arrived as a bare "AskUserQuestion" with an empty
detail line. Indistinguishable from nothing happening, while the session sat
waiting for an answer nobody knew was needed.

The device cannot answer the question (ALLOW merely lets Claude ask it), so the
goal is purely to say "there is something to answer, go look".

    python3 plugin/host-bridge/tests/test_permission_detail.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def load_hook():
    import importlib.util

    path = (
        pathlib.Path(__file__).resolve().parents[2] / "scripts" / "on-permission-request.py"
    )
    spec = importlib.util.spec_from_file_location("_perm_hook", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HOOK = load_hook()


def detail_for(tool_input: dict) -> str:
    return HOOK.extract_detail("AskUserQuestion", tool_input)


# ── Tests ──────────────────────────────────────────────────────────


def test_single_question_shows_the_question_text():
    d = detail_for(
        {"questions": [{"question": "Which library should we use for dates?",
                        "header": "Library", "options": []}]}
    )
    assert "library" in d.lower(), "question text missing from %r" % d


def test_several_questions_show_the_count_and_headers():
    """Headers identify each question in far less space than the questions do."""
    d = detail_for(
        {"questions": [
            {"question": "Where does your Claude Code view live?", "header": "Layout"},
            {"question": "Which approach should we take?", "header": "Approach"},
        ]}
    )
    assert d.startswith("2 questions"), "count missing from %r" % d
    assert "Layout" in d and "Approach" in d, "headers missing from %r" % d


def test_missing_questions_still_says_something():
    """An empty detail is the bug — it renders as a blank prompt."""
    for payload in ({}, {"questions": []}, {"questions": [{}]}):
        d = detail_for(payload)
        assert d.strip(), "empty detail for %r — prompt would render blank" % payload


def test_detail_fits_the_device_buffer():
    """`char detail[64]`, ASCII only, drawn over three wrapped lines."""
    long_q = "Should we " + "really " * 40 + "do this?"
    for payload in (
        {"questions": [{"question": long_q, "header": "Verbose"}]},
        {"questions": [{"question": long_q, "header": "H%d" % i} for i in range(8)]},
    ):
        d = detail_for(payload)
        raw = d.encode("ascii", "strict")  # raises if any non-ASCII slipped through
        assert len(raw) <= HOOK.DETAIL_MAX, (
            "%d bytes exceeds the device's %d" % (len(raw), HOOK.DETAIL_MAX)
        )


def test_long_question_truncates_on_a_word_boundary():
    """Half a word wastes characters on a screen that only has 63 of them."""
    q = "Where does your Claude Code view live in VS Code, and how many are open?"
    d = detail_for({"questions": [{"question": q, "header": "Layout"}]})

    assert d == d.strip(), "trailing whitespace in %r" % d
    assert q.startswith(d), "detail is not a prefix of the question: %r" % d
    rest = q[len(d):]
    assert rest == "" or rest[0] == " ", (
        "cut mid-word: ...%r | %r..." % (d[-14:], rest[:14])
    )


def test_unbroken_run_longer_than_the_buffer_still_truncates():
    """Word-boundary trimming must not fail when there is no boundary."""
    d = detail_for({"questions": [{"question": "x" * 200, "header": "Long"}]})
    assert 0 < len(d.encode()) <= HOOK.DETAIL_MAX, "got %d bytes" % len(d.encode())


def test_non_ascii_question_is_stripped():
    """The device draws single-byte glyphs; a stray em-dash renders as garbage."""
    d = detail_for({"questions": [{"question": "Which — dash — style?", "header": "Dash"}]})
    d.encode("ascii", "strict")


# ── Runner ─────────────────────────────────────────────────────────


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failures += 1
            print("FAIL  %s\n        %s: %s" % (t.__name__, type(e).__name__, e))
        else:
            print("ok    %s" % t.__name__)
    print("\n%d passed, %d failed" % (len(tests) - failures, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
