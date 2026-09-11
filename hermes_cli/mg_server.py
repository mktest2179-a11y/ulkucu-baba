"""``hermes mg`` — a tiny loopback web server for the Model Groups page.

Serves ONLY the ``hermes_cli.web_routers.model_groups`` router (the
``/model-groups`` / ``/mg`` page plus its ``/api/model-groups/*`` API):
a model catalog + ordered cascade groups + per-model role tags + a chat
box that runs prompts through ``cli.py --oneshot`` with the chosen group.

Unlike ``hermes dashboard`` this has no React build step and no auth gate
— it is bound to 127.0.0.1 and meant for a single trusted local operator.
Any local process can reach it while it runs; stop it with Ctrl+C.
"""

from __future__ import annotations

import sys

from hermes_cli.port_probe import (
    PORT_IN_USE_EXIT_CODE,
    is_addr_in_use_error,
    port_bind_conflict,
)


def run_server(host: str = "127.0.0.1", port: int = 9140, *, open_browser: bool = True) -> int:
    try:
        import uvicorn
        from fastapi import FastAPI

        from hermes_cli.web_routers import model_groups as _mg
    except Exception as exc:  # pragma: no cover - dependency/setup issue
        print(f"hermes mg: gerekli modüller yüklenemedi: {exc}", file=sys.stderr)
        return 1

    app = FastAPI(title="Hermes — Model Grupları")
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
