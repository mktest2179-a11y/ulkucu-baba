"""``_scan_dashboard_processes`` must identify servers, not mentions of them.

The scan used to ask whether a raw command line *contained* one of a handful
of substrings ("hermes_cli.main dashboard", ...).  Any process that merely
quoted the command matched — in practice a shell run as
``bash -c "... hermes_cli.main dashboard --status ..."``.  ``hermes dashboard
--status`` listed those shells as dashboards, and ``--stop`` would have killed
them, because it kills exactly what the scan returns.

Identity now comes from argv structure, so quoted text inside a wrapper's
``-c`` argument can no longer line up as a subcommand.
"""

import pytest

from hermes_cli.dashboard_procs import _cmdline_is_dashboard


# Real command lines observed from `hermes dashboard --status` on Windows.
WRAPPER_SHELL = (
    '"C:\\Program Files\\Git\\bin\\bash.exe" -c "source /c/Users/u/.claude/snap.sh '
    "2>/dev/null || true && eval 'HERMES_HOME=D:/Hermes .venv/Scripts/python.exe "
    "-m hermes_cli.main dashboard --status 2>&1 | tail -15' < /dev/null\""
)
REAL_DASHBOARD = (
    '"D:\\Hermes\\hermes-agent\\.venv\\Scripts\\python.exe" '
    "-m hermes_cli.main dashboard --no-open --skip-build --port 9119"
)


@pytest.mark.parametrize("cmdline", [
    REAL_DASHBOARD,
    "python -m hermes_cli.main dashboard --no-open --port 9119",
    "python -m hermes_cli.main serve --host 127.0.0.1",
    "hermes dashboard --no-open",
    "hermes serve",
    "/usr/bin/python3 /opt/hermes/hermes_cli/main.py dashboard --port 9119",
    r"python D:\Hermes\hermes-agent\hermes_cli\main.py serve",
    "python -m hermes_cli.main --profile work dashboard --port 9120",
])
def test_real_servers_are_matched(cmdline):
    assert _cmdline_is_dashboard(cmdline) is True


@pytest.mark.parametrize("cmdline", [
    pytest.param(WRAPPER_SHELL, id="git-bash-wrapper"),
    pytest.param(
        'bash -c "hermes dashboard --status"', id="short-bash-wrapper"),
    pytest.param(
        'cmd.exe /c "python -m hermes_cli.main dashboard"', id="cmd-wrapper"),
    pytest.param(
        'powershell -Command "hermes serve"', id="powershell-wrapper"),
    pytest.param(
        'wscript.exe "C:\\x\\start.vbs" hermes dashboard', id="script-host"),
    pytest.param(
        "python -m hermes_cli.main chat --message 'fix the dashboard'",
        id="chat-about-dashboard"),
    pytest.param(
        "python -m hermes_cli.main gateway run", id="gateway-not-dashboard"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="blank"),
])
def test_non_servers_are_not_matched(cmdline):
    assert _cmdline_is_dashboard(cmdline) is False


def test_unbalanced_quotes_do_not_raise():
    assert _cmdline_is_dashboard('python -m hermes_cli.main dashboard --title "x') in (
        True, False
    )


def test_scan_skips_wrapper_shells(monkeypatch):
    """End-to-end through the ps branch: only the real server comes back."""
    import subprocess
    import sys

    from hermes_cli import dashboard_procs

    monkeypatch.setattr(sys, "platform", "linux")

    ps_output = (
        f"  101 {REAL_DASHBOARD}\n"
        f"  202 {WRAPPER_SHELL}\n"
        "  303 python -m hermes_cli.main gateway run\n"
    )

    class _Result:
        returncode = 0
        stdout = ps_output

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result())
    monkeypatch.setattr(dashboard_procs, "ledger_entries", None, raising=False)
    monkeypatch.setattr(
        "hermes_cli.process_identity.ledger_entries", lambda: [], raising=False
    )

    found = dashboard_procs._scan_dashboard_processes()

    assert [pid for pid, _ in found] == [101]


@pytest.mark.parametrize("cmdline,expected", [
    ("python -m hermes_cli.main --profile work dashboard --port 9120", True),
    ("python -m hermes_cli.main --profile=work serve", True),
    ("python -m hermes_cli.main -p work dashboard", True),
    ("python -m hermes_cli.main --yolo dashboard", True),
    # A different subcommand must stop the walk, not be skipped past.
    ("python -m hermes_cli.main gateway run --then dashboard", False),
    ("python -m hermes_cli.main chat dashboard", False),
    # Flags only, no subcommand at all.
    ("python -m hermes_cli.main --help", False),
])
def test_global_flags_before_subcommand(cmdline, expected):
    assert _cmdline_is_dashboard(cmdline) is expected


@pytest.mark.parametrize("cmdline", [
    "python -m hermes_cli.main dashboard --status",
    "python -m hermes_cli.main dashboard --stop",
    "python -m hermes_cli.main serve --help",
    "hermes dashboard -h",
])
def test_control_invocations_are_not_servers(cmdline):
    """`--stop` kills what the scan returns — it must not return itself."""
    assert _cmdline_is_dashboard(cmdline) is False


def test_server_with_similar_flag_still_matches():
    assert _cmdline_is_dashboard(
        "python -m hermes_cli.main dashboard --no-open --status-file /tmp/s"
    ) is True


@pytest.mark.parametrize("cmdline", [
    # Wrappers nobody enumerated: a positive interpreter rule rejects them all.
    'supervisord -c "python -m hermes_cli.main dashboard"',
    'nu -c "hermes dashboard"',
    'xonsh -c "python -m hermes_cli.main serve"',
    'code --run "python -m hermes_cli.main dashboard"',
    'tmux new-session "hermes dashboard"',
])
def test_unknown_wrappers_are_rejected_without_a_denylist(cmdline):
    assert _cmdline_is_dashboard(cmdline) is False


@pytest.mark.parametrize("cmdline", [
    "python3.11 -m hermes_cli.main dashboard",
    "pythonw.exe -m hermes_cli.main serve",
    "uv run -m hermes_cli.main dashboard",
    "/usr/local/bin/hermes dashboard",
])
def test_real_interpreters_are_accepted(cmdline):
    assert _cmdline_is_dashboard(cmdline) is True
