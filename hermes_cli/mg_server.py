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
    return 0


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
