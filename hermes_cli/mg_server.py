"""``hermes mg`` — a tiny loopback web server for the Model Groups page.

Serves ONLY the ``hermes_cli.web_routers.model_groups`` router (the
``/model-groups`` / ``/mg`` page plus its ``/api/model-groups/*`` API):
a model catalog + ordered cascade groups + per-model role tags + a chat
box that runs prompts through ``cli.py --oneshot`` with the chosen group.

Unlike ``hermes dashboard`` this has no React build step and no token/
session auth gate — it is bound to 127.0.0.1 and meant for a single
trusted local operator. Any local process can reach it while it runs;
stop it with Ctrl+C.

Absence of a token gate does not mean absence of a boundary, though: a
Host-header check is still required (see ``_loopback_host_middleware``
below), the same defense ``hermes dashboard`` applies to its own ``/mg``
mount (``hermes_cli.web_server.host_header_middleware``). Without it, DNS
rebinding lets an attacker-controlled external page — resolved to
127.0.0.1 after its DNS TTL expires — issue same-origin-looking requests
here: unauthenticated POST /gorev starts a real agent process in a
caller-chosen working directory (optionally with ``auto_escalate``, which
skips the fallback-approval gate for that run), POST /onay approves a
pending escalation, and POST /dosya-yukle writes into
HERMES_HOME/girdiler/ — arbitrary local code execution under the
operator's own account for any caller that can reach this port at all.
Confirmed live (2026-09-11 security audit task): this port answered a
request carrying ``Host: evil.test`` with 200; ``hermes dashboard``'s own
``/mg`` mount already rejects the identical request with 400 at the Host
layer.
"""

from __future__ import annotations

import re
import sys

from hermes_cli.port_probe import (
    PORT_IN_USE_EXIT_CODE,
    is_addr_in_use_error,
    port_bind_conflict,
)

# This server is always loopback-bound (see module docstring), so — unlike
# hermes_cli.web_server's host_header_middleware, which also has to accept
# an operator-declared public hostname and an explicit 0.0.0.0 opt-in — the
# only Host values that are ever legitimately ours are the loopback aliases.
# Parsing logic is a deliberate copy of web_server._host_header_hostname
# (same hardening: reject ambiguous ports, malformed IPv6 brackets, URL
# syntax) rather than an import, so this stays a small standalone server
# with no dependency on the ~20k-line dashboard module for one helper.
_LOOPBACK_HOST_VALUES = frozenset({"localhost", "127.0.0.1", "::1"})


def _host_header_hostname(host_header: str) -> str:
    """Return a normalized hostname from a valid HTTP Host authority."""
    value = (host_header or "").strip()
    if not value:
        return ""
    if any(char in value for char in ('"', "'", "<", ">", " ", "\n", "\r", "\t")):
        return ""
    if "://" in value or any(char in value for char in ("/", "?", "#", "@")):
        return ""
    if value.startswith("["):
        close = value.find("]")
        if close == -1:
            return ""
        hostname = value[1:close]
        if ":" not in hostname:
            return ""
        suffix = value[close + 1:]
        if suffix and not re.fullmatch(r":\d+", suffix):
            return ""
        return hostname.lower()
    if value.count(":") > 1:
        return ""
    if ":" in value:
        hostname, port = value.rsplit(":", 1)
        if not hostname or not port.isdigit():
            return ""
        return hostname.lower()
    return value.lower()


async def _loopback_host_middleware(request, call_next):
    """Reject any request whose Host header isn't a loopback alias.

    See the module docstring's DNS-rebinding note. 400 (not 404/401) makes
    the rejection reason legible at the network layer rather than looking
    like a routing miss.
    """
    from starlette.responses import JSONResponse

    if _host_header_hostname(request.headers.get("host", "")) not in _LOOPBACK_HOST_VALUES:
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid Host header. hermes mg only answers loopback requests."},
        )
    return await call_next(request)


def run_server(host: str = "127.0.0.1", port: int = 9140, *, open_browser: bool = True) -> int:
    try:
        import uvicorn
        from fastapi import FastAPI

        from hermes_cli.web_routers import model_groups as _mg
    except Exception as exc:  # pragma: no cover - dependency/setup issue
        print(f"hermes mg: gerekli modüller yüklenemedi: {exc}", file=sys.stderr)
        return 1

    app = FastAPI(title="Hermes — Model Grupları")
    app.middleware("http")(_loopback_host_middleware)
    app.include_router(_mg.router)

    url = f"http://{host}:{port}/mg"

    # Preflight, because uvicorn does not raise on a bind failure: its
    # bind_socket() logs the OSError and calls sys.exit(1), so there is no
    # exception for us to classify afterwards.
    if port_bind_conflict(host, port):
        return _handle_port_taken(host, port, url)

    print(f"Hermes Model Grupları → {url}   (Ctrl+C ile durdur)")
    if open_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    except KeyboardInterrupt:  # pragma: no cover
        return 0
    except OSError as exc:
        # Narrow TOCTOU window: someone bound the port between the preflight
        # above and uvicorn's own bind.
        if not is_addr_in_use_error(exc):
            raise
        return _handle_port_taken(host, port, url)
    except SystemExit as exc:
        # uvicorn's bind_socket() exits 1 rather than raising; re-classify
        # that as a port conflict only when the port really is taken.
        if exc.code != 1 or not port_bind_conflict(host, port):
            raise
        return _handle_port_taken(host, port, url)
    return 0


def _handle_port_taken(host: str, port: int, url: str) -> int:
    """Decide whether a busy port is a lost start race or a real failure.

    Losing a start race is the normal case here, not an error.  At logon both
    Hermes_Gateway_Baslat.vbs and the HermesMG scheduled task (mg_baslat.bat)
    check whether 9140 answers and then start mg; neither has bound yet when
    the other looks, so both start and the loser used to die with a raw
    EADDRINUSE traceback.  If the port is already serving mg, the launcher got
    the outcome it wanted — say so and exit 0 rather than reporting a failure
    nobody needs to act on.  A port held by anything else is still a genuine
    conflict, reported through the same sentinel serve/dashboard use.
    """
    if _mg_already_serving(host, port):
        print(f"hermes mg: {url} zaten çalışıyor — bu kopya kapanıyor.", flush=True)
        return 0
    _report_port_conflict(host, port)
    return PORT_IN_USE_EXIT_CODE


def _mg_already_serving(host: str, port: int, timeout: float = 3.0) -> bool:
    """True when something at ``host:port`` answers as an mg server."""
    import json
    import urllib.error
    import urllib.request

    url = f"http://{host}:{port}/api/model-groups/sohbet"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return isinstance(payload, dict) and "chat" in payload


def _report_port_conflict(host: str, port: int) -> None:
    """Reuse serve/dashboard's machine sentinel + human hint when possible."""
    try:
        from hermes_cli.web_server import _report_port_in_use

        _report_port_in_use(host, port)
        return
    except Exception:
        pass
    print(
        f"hermes mg: {host}:{port} başka bir süreç tarafından kullanılıyor. "
        "O süreci durdur ya da --port <baska> ver.",
        file=sys.stderr,
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="hermes mg",
        description="Model Grupları sayfası — yerel, auth'suz (127.0.0.1).",
    )
    p.add_argument("--port", type=int, default=9140, help="Port (varsayılan 9140)")
    p.add_argument("--host", default="127.0.0.1", help="Host (varsayılan 127.0.0.1)")
    p.add_argument("--no-open", action="store_true", help="Tarayıcıyı otomatik açma")
    args = p.parse_args(argv)
    return run_server(args.host, args.port, open_browser=not args.no_open)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
