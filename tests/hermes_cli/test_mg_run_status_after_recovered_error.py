"""A run that recovers from a tool error and delivers a real answer must not
be marked 'hata' (Windows: verified against #a4867117, 2026-09-11).

``rec["hata"]`` only means "some tool call printed an error line somewhere
during the run" — every ``_RE_ERR`` match overwrites it, with no memory of
whether the agent went on to finish. The old final check,
``rec.get("hata") or returncode != 0``, treated that alone as "the whole task
failed": an agent that hit one recoverable mistake (a bad regex, a blocked
command) and then completed normally — coherent parsed answer and all — still
got shown to the user as HATA, with the real answer buried in raw output
instead of surfaced for review.
"""

import subprocess
import threading
import time

import pytest

import hermes_cli.mg_run as mg_run


def _run_reader(gid: str, output_lines: list[str], returncode: int = 0):
    """Feed synthetic cli.py stdout through the real ``_reader`` state machine."""
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


# The exact shape of the real bridge output (#a4867117): a recoverable tool
# error mid-run, followed by a normal answer box and session footer.
_RECOVERED_ERROR_TRANSCRIPT = [
    "Initializing agent...",
    "  ┊ 🔍 search_files pattern1  1.8s",
    "❌ Error: Search failed: rg: regex parse error: unclosed group",
    "  ┊ 💻 netstat -ano  0.4s",
    "╭─ ⚕ Hermes ─────────────────────────────────────────╮",
    "Tarama bitti. Servis durumu: her şey çalışıyor.",
    "╰─────────────────────────────────────────────────────╯",
    "Session:        20260911_180302_4dfd9b",
]


def test_recovered_error_does_not_mark_the_task_failed():
    rec = _run_reader("gid1", _RECOVERED_ERROR_TRANSCRIPT, returncode=0)

    assert rec["hata"] is not None, "sanity: the error line was seen"
    assert rec["sonuc"]["cevap"] == "Tarama bitti. Servis durumu: her şey çalışıyor."
    assert rec["durum"] == "teslim_bekliyor", (
        "a coherent delivered answer must reach the user, not a HATA badge"
    )


def test_error_with_no_recovered_answer_still_fails():
    """The safety property must survive: no answer + an error IS a real failure."""
    transcript = [
        "Initializing agent...",
        "❌ Error: Search failed: rg: regex parse error: unclosed group",
    ]

    rec = _run_reader("gid2", transcript, returncode=0)

    assert rec["durum"] == "hata"
    assert rec["sonuc"]["cevap"] == "(cevap ayrıştırılamadı — ham çıktıya bak)"


def test_nonzero_returncode_always_fails_even_with_a_parsed_answer():
    """A crashed/killed process is a real failure regardless of stdout content."""
    transcript = [
        "Initializing agent...",
        "╭─ ⚕ Hermes ─────────────────────────────────────────╮",
        "kismi cevap",
        "╰─────────────────────────────────────────────────────╯",
    ]

    rec = _run_reader("gid3", transcript, returncode=1)

    assert rec["durum"] == "hata"


def test_clean_run_with_no_errors_still_delivers_normally():
    """Unaffected path: no error line at all -> unchanged behavior."""
    transcript = [
        "Initializing agent...",
        "╭─ ⚕ Hermes ─────────────────────────────────────────╮",
        "2+2 = 4",
        "╰─────────────────────────────────────────────────────╯",
        "Session:        20260911_000000_aaaaaa",
    ]

    rec = _run_reader("gid4", transcript, returncode=0)

    assert rec["durum"] == "teslim_bekliyor"
    assert rec["sonuc"]["cevap"] == "2+2 = 4"
