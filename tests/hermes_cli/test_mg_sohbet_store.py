"""Tests for the mg chat-history store (``mg_sohbet.json``).

The POST handler used to stage every save through one shared
``mg_sohbet.tmp``.  Two browsers saving at the same time truncated and wrote
that same path concurrently, so the file promoted into place could be a short
document followed by the tail of the longer one it replaced.  ``json.loads``
then rejected the whole file and the mg page came up with an empty history
even though the leading document was intact.

The writer now goes through ``atomic_json_write`` (unique mkstemp temp file
per save) and the reader recovers the leading document from any file damaged
by the old writer.
"""

import json

import pytest

from hermes_cli.web_routers.model_groups import _sohbet_load


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_reads_a_healthy_file(tmp_path):
    p = _write(tmp_path / "mg_sohbet.json",
               json.dumps({"chat": [{"role": "user", "text": "selam"}], "hist": []}))

    assert _sohbet_load(p) == {"chat": [{"role": "user", "text": "selam"}], "hist": []}


def test_recovers_leading_document_from_torn_file(tmp_path):
    """Exactly the shape the old shared-tmp writer left behind."""
    good = json.dumps({"chat": [{"role": "user", "text": "selam"}], "hist": []})
    p = _write(tmp_path / "mg_sohbet.json", good + '"preview": "bitti", "chat"')

    assert _sohbet_load(p) == {"chat": [{"role": "user", "text": "selam"}], "hist": []}


def test_unrecoverable_file_yields_empty_history(tmp_path):
    p = _write(tmp_path / "mg_sohbet.json", "}{ not json at all")

    assert _sohbet_load(p) == {"chat": [], "hist": []}


def test_non_object_document_yields_empty_history(tmp_path):
    p = _write(tmp_path / "mg_sohbet.json", "[1, 2, 3]")

    assert _sohbet_load(p) == {"chat": [], "hist": []}


def test_leading_whitespace_is_tolerated(tmp_path):
    p = _write(tmp_path / "mg_sohbet.json", '\n\n  {"chat": [], "hist": [1]}trailing')

    assert _sohbet_load(p) == {"chat": [], "hist": [1]}


@pytest.mark.asyncio
async def test_concurrent_saves_never_leave_a_torn_file(tmp_path, monkeypatch):
    """Ten overlapping saves must each land as a complete, parseable file."""
    import hermes_cli.web_routers.model_groups as mg
    import asyncio

    monkeypatch.setattr(mg, "get_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)

    async def save(n):
        # Bodies of very different lengths — a non-atomic writer leaves the
        # long one's tail behind when a short one lands on top of it.
        return await mg.sohbet_post(
            mg.SohbetBody(chat=[{"role": "user", "text": "x" * (n * 500)}], hist=[])
        )

    results = await asyncio.gather(*(save(n) for n in range(10)))

    assert all(r["ok"] for r in results)
    loaded = json.loads((tmp_path / "mg_sohbet.json").read_text(encoding="utf-8"))
    assert set(loaded) == {"chat", "hist"}
    assert len(loaded["chat"]) == 1
