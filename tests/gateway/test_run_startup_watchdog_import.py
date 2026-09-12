"""Live-confirmed (2026-09-12): ``GatewayRunner.start()`` in gateway/run.py imported
arm/disarm_startup_watchdog from ``gateway.startup_watchdog`` — a compat stub that
re-exports ``gateway.shutdown_watchdog`` (a *different* watchdog, for shutdown, not
startup). ``gateway.shutdown_watchdog`` defines neither symbol, so both imports raised
``ImportError`` at runtime. Both call sites wrap the import in a broad
``except Exception: pass`` / ``logger.debug(...)``, so the failure was silent.

Confirmed impact: the watchdog *armed* fine elsewhere (hermes_cli/main.py and
hermes_cli/gateway.py import the real ``hermes_startup_watchdog`` module directly), but
the one disarm call that matters — "the event loop is confirmed live, startup's job is
done" inside ``GatewayRunner.start()`` — always failed. A watchdog that is armed but
never disarmed eventually calls ``os._exit(SERVICE_RESTART_EXIT_CODE)`` once its lease
and CPU-progress extensions run out, so a healthy, fully-started gateway would
self-terminate. Live gateway.log showed the tell: the same
"Gateway startup exceeded 300s ... holds a progress lease" warning recurring ~296s
after *every* gateway startup (03:33, 03:55, 04:36), which is exactly the watchdog
never being told startup finished.

Fix: both call sites now import from ``hermes_startup_watchdog`` directly, matching
the already-correct pattern used by gateway/run_startup.py, hermes_cli/gateway.py,
hermes_cli/main.py, and cli.py.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_gateway_shutdown_watchdog_has_no_startup_watchdog_symbols():
    """Pin down the actual shape of the trap: shutdown_watchdog must not define
    these names, so importing them via the gateway.startup_watchdog compat stub
    is a guaranteed ImportError, not an incidental one."""
    from gateway import shutdown_watchdog

    for name in ("arm_startup_watchdog", "disarm_startup_watchdog", "report_startup_progress"):
        assert not hasattr(shutdown_watchdog, name), (
            f"gateway.shutdown_watchdog unexpectedly defines {name!r} — the "
            "ImportError this regression test guards against may no longer occur "
            "via that path; re-check gateway/run.py's import source directly."
        )


def test_gateway_startup_watchdog_alias_still_lacks_the_symbols():
    """Reproduces the exact failure mode live-confirmed in gateway.log: importing
    arm/disarm_startup_watchdog from the compat-stub alias raises ImportError."""
    import importlib

    alias = importlib.import_module("gateway.startup_watchdog")
    for name in ("arm_startup_watchdog", "disarm_startup_watchdog"):
        assert not hasattr(alias, name), (
            f"gateway.startup_watchdog now exposes {name!r} — if this stub was "
            "fixed to re-export the real hermes_startup_watchdog, that's fine, but "
            "then gateway/run.py's fix here is merely redundant, not moot; leave "
            "the direct hermes_startup_watchdog imports in place regardless."
        )


def test_gateway_run_imports_startup_watchdog_from_the_real_module():
    """Structural regression guard: gateway/run.py must import arm/disarm_startup_watchdog
    from hermes_startup_watchdog, never from the dead gateway.startup_watchdog alias."""
    source = (REPO_ROOT / "gateway" / "run.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    bad_imports = []
    good_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names = {alias.name for alias in node.names}
            if not ({"arm_startup_watchdog", "disarm_startup_watchdog"} & names):
                continue
            if node.module == "gateway.startup_watchdog":
                bad_imports.append(node.lineno)
            elif node.module == "hermes_startup_watchdog":
                good_names |= names

    assert not bad_imports, (
        f"gateway/run.py imports arm/disarm_startup_watchdog from the dead "
        f"gateway.startup_watchdog alias at line(s) {bad_imports} — this raises "
        "ImportError at runtime and is silently swallowed, leaving the watchdog "
        "permanently armed. Import from hermes_startup_watchdog instead."
    )
    assert {"arm_startup_watchdog", "disarm_startup_watchdog"} <= good_names, (
        "gateway/run.py no longer imports both arm_startup_watchdog and "
        "disarm_startup_watchdog from hermes_startup_watchdog — this test's "
        "structural check needs updating alongside whatever moved them."
    )


def test_hermes_startup_watchdog_disarm_import_succeeds():
    """The concrete repro: this is the exact import statement gateway/run.py now
    uses; it must not raise."""
    from hermes_startup_watchdog import arm_startup_watchdog, disarm_startup_watchdog

    assert callable(arm_startup_watchdog)
    assert callable(disarm_startup_watchdog)
