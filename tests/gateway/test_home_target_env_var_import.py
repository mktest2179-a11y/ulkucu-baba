"""Live-confirmed (2026-09-12, real WhatsApp round-trip): ``gateway.run._home_target_env_var``
imported ``_resolve_home_env_var`` from ``cron.scheduler`` — but ``cron/scheduler.py``'s own
split-module re-export block only pulls in the five names it calls itself
(``_deliver_result``, ``_delivery_lane_value``, ``_normalize_deliver_value``,
``_resolve_delivery_target``, ``_resolve_delivery_targets``); ``_resolve_home_env_var`` was
never among them even though it lives in the same ``cron.scheduler_delivery`` split module.
The import is unconditional inside the function body, so *every* call — any platform,
including the built-in matrix/email cases — raised ``ImportError`` at runtime.

Confirmed live: a real inbound WhatsApp message triggered this exact ImportError and the
agent's error handler surfaced it verbatim back to the user instead of a reply.

Fix: import ``_resolve_home_env_var`` from ``cron.scheduler_delivery`` directly, matching
where the function is actually defined.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_resolve_home_env_var_lives_in_scheduler_delivery_not_scheduler():
    """Pin down the trap: cron.scheduler does not re-export this name, so importing
    it from there is a guaranteed ImportError."""
    from cron import scheduler, scheduler_delivery

    assert hasattr(scheduler_delivery, "_resolve_home_env_var")
    assert "_resolve_home_env_var" not in vars(scheduler), (
        "cron.scheduler now re-exports _resolve_home_env_var — if this was fixed at the "
        "source, gateway/run.py's direct import here is merely redundant, not moot; leave "
        "it importing from cron.scheduler_delivery regardless."
    )


def test_gateway_run_imports_resolve_home_env_var_from_the_real_module():
    """Structural regression guard: gateway/run.py must import _resolve_home_env_var
    from cron.scheduler_delivery, never from cron.scheduler."""
    source = (REPO_ROOT / "gateway" / "run.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    bad_imports = []
    good_found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names = {alias.name for alias in node.names}
            if "_resolve_home_env_var" not in names:
                continue
            if node.module == "cron.scheduler":
                bad_imports.append(node.lineno)
            elif node.module == "cron.scheduler_delivery":
                good_found = True

    assert not bad_imports, (
        f"gateway/run.py imports _resolve_home_env_var from cron.scheduler at line(s) "
        f"{bad_imports} — that module does not re-export it, so this raises ImportError "
        "on every call. Import from cron.scheduler_delivery instead."
    )
    assert good_found, (
        "gateway/run.py no longer imports _resolve_home_env_var from "
        "cron.scheduler_delivery — this test's structural check needs updating "
        "alongside whatever moved it."
    )


def test_home_target_env_var_resolves_a_platform_without_a_builtin_entry():
    """The concrete repro: whatsapp has no _HOME_TARGET_ENV_VARS entry, so this call falls
    through to _resolve_home_env_var — exactly the line that crashed live."""
    from gateway.run import _home_target_env_var

    assert _home_target_env_var("whatsapp") == "WHATSAPP_HOME_CHANNEL"
