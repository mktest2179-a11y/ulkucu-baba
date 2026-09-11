"""A diff review showing code that contains "exception"/"error" must not be
misread as the task failing (Windows: reproduced against task #6c0050f6,
2026-09-11 — a write_file diff added `except Exception as e:` lines and each
one got logged as a HATA stage even though nothing failed).

``_RE_ERR`` matches the bare word "exception" (or "error:", "fatal") anywhere
in a line, which is exactly what a Python try/except block's diff line looks
like. ``agent/display.py``'s ``_emit_inline_diff()`` prints a "review diff"
marker followed by raw unified-diff lines with no closing marker, so those
lines must be excluded from the HATA scan for the duration of the diff block.
"""

import pytest

import hermes_cli.mg_run as mg_run


def _run_reader(gid: str, output_lines: list[str], returncode: int = 0):
    mg_run._TASKS[gid] = {
        "gid": gid, "durum": "calisiyor", "asamalar": [], "cikti": [],
        "hata": None, "sure": None, "mesaj_sayisi": None,
    }

    class _FakeStdout:
        def __init__(self, lines):
            self._lines = iter(l + "\n" for l in lines)

        def readline(self):
            return next(self._lines, "")

    class _FakeProc:
        stdout = None
        returncode = None

        def wait(self_inner):
            self_inner.returncode = returncode
            return returncode

    proc = _FakeProc()
    proc.stdout = _FakeStdout(output_lines)
    mg_run._reader(gid, proc)
    return mg_run._TASKS.pop(gid)


# The exact shape from the real bug: a write_file diff whose added lines
# contain "except Exception", surrounded by ordinary tool-status lines.
_DIFF_WITH_EXCEPTION_TRANSCRIPT = [
    "Initializing agent...",
    "  ┊ 💻 ls -lt logs/ + 1 command  0.5s",
    "  ┊ review diff",
    "a/probe.py → b/probe.py",
    "@@ -0,0 +1,5 @@",
    "+def get(url):",
    "+    try:",
    "+        return urlopen(url)",
    "+    except Exception as e:",
    "+        return str(e)",
    "  ┊ 💻 python3 probe.py  4.5s",
    "╭─ ⚕ Hermes ─────────────────────────────────────────╮",
    "Probe tamamlandi, sonuc iyi.",
    "╰─────────────────────────────────────────────────────╯",
    "Session:        20260911_210900_abcdef",
]


def test_diff_content_does_not_trigger_hata_stages():
    rec = _run_reader("g1", _DIFF_WITH_EXCEPTION_TRANSCRIPT, returncode=0)

    hata_stages = [s for s in rec["asamalar"] if s["ad"] == "HATA"]
    assert hata_stages == [], f"diff content misread as errors: {hata_stages}"
    assert rec["hata"] is None
    assert rec["durum"] == "teslim_bekliyor"
    assert rec["sonuc"]["cevap"] == "Probe tamamlandi, sonuc iyi."


def test_arac_stages_are_still_recorded_around_the_diff():
    rec = _run_reader("g2", _DIFF_WITH_EXCEPTION_TRANSCRIPT, returncode=0)

    arac_stages = [s["detay"] for s in rec["asamalar"] if s["ad"] == "ARAÇ"]
    assert arac_stages == ["ls -lt logs/ + 1 command", "python3 probe.py"]


def test_diff_content_is_still_captured_in_raw_output():
    """The fix must only skip HATA classification, not drop the lines."""
    rec = _run_reader("g3", _DIFF_WITH_EXCEPTION_TRANSCRIPT, returncode=0)

    assert "+    except Exception as e:" in rec["cikti"]


def test_a_real_error_after_a_diff_block_still_fires():
    """The diff state must end when normal output resumes, not stick forever."""
    transcript = _DIFF_WITH_EXCEPTION_TRANSCRIPT[:10] + [
        "  ┊ 💻 does-not-exist  0.1s",
        "❌ Error: command not found: does-not-exist",
    ]

    rec = _run_reader("g4", transcript, returncode=0)

    hata_stages = [s for s in rec["asamalar"] if s["ad"] == "HATA"]
    assert len(hata_stages) == 1
    assert "does-not-exist" in hata_stages[0]["detay"]


def test_a_real_error_with_no_diff_involved_still_fires_as_before():
    transcript = [
        "Initializing agent...",
        "❌ Error: Search failed: rg: regex parse error: unclosed group",
    ]

    rec = _run_reader("g5", transcript, returncode=0)

    assert any(s["ad"] == "HATA" for s in rec["asamalar"])
    assert rec["hata"] is not None
