"""Mid-task steering for `hermes mg`: a message sent from the mg page must
reach the running agent's turn loop without cancelling the task.

A mg task runs as a headless ``cli.py --oneshot`` subprocess with no
keyboard for the interactive ``/steer`` command to reach, and the process
running the turn is not the web server process the mg page talks to.
``mg_drain_external_steer`` bridges that gap via the same IPC-file pattern
the fallback-approval gate already uses (``<ipc>/<gid>.pending`` /
``.resp``): the server writes ``<gid>.steer``, this function picks it up
inside the running subprocess and hands it to the real, already-tested
``agent.steer()``.
"""

import os

import pytest

from agent.chat_completion_helpers import mg_drain_external_steer


class _FakeAgent:
    def __init__(self):
        self.steered = []

    def steer(self, text):
        self.steered.append(text)
        return True


@pytest.fixture
def ipc(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_MG_GID", "gid123")
    monkeypatch.setenv("HERMES_MG_IPC", str(tmp_path))
    return tmp_path


def test_picks_up_a_pending_steer_file(ipc):
    (ipc / "gid123.steer").write_text("durdurma, once X'i kontrol et", encoding="utf-8")
    agent = _FakeAgent()

    mg_drain_external_steer(agent)

    assert agent.steered == ["durdurma, once X'i kontrol et"]
    assert not (ipc / "gid123.steer").exists(), "must consume the file, not leave it to re-fire"


def test_no_file_is_a_silent_noop(ipc):
    agent = _FakeAgent()

    mg_drain_external_steer(agent)  # must not raise

    assert agent.steered == []


def test_no_mg_session_is_a_silent_noop(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_MG_GID", raising=False)
    monkeypatch.delenv("HERMES_MG_IPC", raising=False)
    agent = _FakeAgent()

    mg_drain_external_steer(agent)

    assert agent.steered == []


def test_empty_file_is_not_delivered_as_a_blank_steer(ipc):
    (ipc / "gid123.steer").write_text("   \n", encoding="utf-8")
    agent = _FakeAgent()

    mg_drain_external_steer(agent)

    assert agent.steered == []


def test_a_second_task_never_sees_another_gids_steer(ipc):
    (ipc / "gid999.steer").write_text("baska goreve ait", encoding="utf-8")
    agent = _FakeAgent()

    mg_drain_external_steer(agent)  # HERMES_MG_GID is "gid123"

    assert agent.steered == []
    assert (ipc / "gid999.steer").exists(), "must not touch a different task's file"


def test_fails_open_when_agent_steer_raises(ipc):
    (ipc / "gid123.steer").write_text("mesaj", encoding="utf-8")

    class _Boom:
        def steer(self, text):
            raise RuntimeError("boom")

    mg_drain_external_steer(_Boom())  # must not raise
