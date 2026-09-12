"""Live-confirmed (2026-09-12, dashboard chat crash): tui_gateway/server.py rebinds each split
handler module's functions onto its own globals via ``method_ctx.bind_module`` (called from each
module's own ``register(server)``) — but ``session_lifecycle.py`` was never added to the list of
modules ``server.py`` imports and registers, even though ``methods_session.py`` and
``methods_prompt.py`` both call its ``_reattach_refusal`` as a bare name, relying on it being
published onto server.py's globals by that same mechanism.

Confirmed live: the dashboard's `/chat` page crashed on a plain prompt.submit ("hangi modelsin")
with ``NameError: name '_reattach_refusal' is not defined`` at methods_prompt.py:591. The same
call exists in methods_session.py at three more sites (session reattach paths) that would fail
identically once exercised — this wasn't a methods_prompt-only bug, just the first path hit.

Fix: added session_lifecycle to server.py's split-module import block and registration loop, the
same way every other split-module (methods_session, methods_prompt, ...) is wired in.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_reattach_refusal_is_published_on_the_live_server_module():
    """The exact symbol that crashed live must resolve on the server module server.py's
    handler bodies actually run against."""
    import tui_gateway.server as server

    assert hasattr(server, "_reattach_refusal")
    assert callable(server._reattach_refusal)


def test_server_py_registers_session_lifecycle_alongside_its_sibling_method_modules():
    """Structural regression guard: session_lifecycle must be imported and passed to
    a module's register(server) call in tui_gateway/server.py, like every other split
    handler module already is."""
    source = (REPO_ROOT / "tui_gateway" / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported_as = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in (None, "tui_gateway", "."):
            for alias in node.names:
                if alias.name == "session_lifecycle":
                    imported_as = alias.asname or alias.name
    assert imported_as, (
        "tui_gateway/server.py no longer imports session_lifecycle — the module that defines "
        "_reattach_refusal (used bare by methods_session.py and methods_prompt.py) must be "
        "imported here so its register() call publishes its functions onto server.py's globals."
    )
    assert f"{imported_as}.register(" in source or f"_m.register(" in source, (
        f"session_lifecycle imported as {imported_as!r} but never registered — its functions "
        "won't be published onto server.py's globals without a register(server) call."
    )
