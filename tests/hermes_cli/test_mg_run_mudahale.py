"""``mg_run.mudahale()`` writes the IPC file the running subprocess polls
(``mg_drain_external_steer``, see tests/agent/test_mg_external_steer.py) —
this is the server-side half of the same feature.
"""

import pytest

import hermes_cli.mg_run as mg_run


@pytest.fixture(autouse=True)
def _isolated_tasks_and_ipc(tmp_path, monkeypatch):
    monkeypatch.setattr(mg_run, "_TASKS", {})
    monkeypatch.setattr(mg_run, "_ipc_dir", lambda: tmp_path)
    return tmp_path


def _running_task(gid):
    mg_run._TASKS[gid] = {
        "gid": gid, "durum": "calisiyor", "asamalar": [],
    }


def test_writes_the_steer_file_for_a_running_task(_isolated_tasks_and_ipc):
    _running_task("g1")

    ok = mg_run.mudahale("g1", "once X'i kontrol et")

    assert ok is True
    assert (_isolated_tasks_and_ipc / "g1.steer").read_text(encoding="utf-8") == (
        "once X'i kontrol et"
    )


def test_records_a_stage_so_the_ui_shows_it_was_sent(_isolated_tasks_and_ipc):
    _running_task("g1")

    mg_run.mudahale("g1", "mesaj")

    stages = mg_run._TASKS["g1"]["asamalar"]
    assert any(s["ad"] == "MÜDAHALE" for s in stages)


@pytest.mark.parametrize("durum", ["teslim_bekliyor", "bitti", "hata", "iptal", "onay_bekliyor"])
def test_refuses_when_the_task_is_not_running(_isolated_tasks_and_ipc, durum):
    mg_run._TASKS["g1"] = {"gid": "g1", "durum": durum, "asamalar": []}

    ok = mg_run.mudahale("g1", "mesaj")

    assert ok is False
    assert not (_isolated_tasks_and_ipc / "g1.steer").exists()


def test_refuses_an_unknown_gid(_isolated_tasks_and_ipc):
    assert mg_run.mudahale("does-not-exist", "mesaj") is False


def test_refuses_blank_text(_isolated_tasks_and_ipc):
    _running_task("g1")

    assert mg_run.mudahale("g1", "   ") is False
    assert mg_run.mudahale("g1", "") is False


def test_gid_is_stripped():
    _running_task("g1")
    ok = mg_run.mudahale("  g1  ", "mesaj")
    assert ok is True
