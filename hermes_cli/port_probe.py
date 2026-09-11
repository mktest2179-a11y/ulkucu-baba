"""Shared TCP bind-conflict probe for Hermes' local HTTP servers.

``serve``/``dashboard`` (``hermes_cli.web_server``) and ``mg``
(``hermes_cli.mg_server``) all hand a host/port to uvicorn, and uvicorn does
not raise on a bind failure — ``uvicorn.Config.bind_socket`` logs the OSError
and calls ``sys.exit(1)``.  A caller that wants to tell "port taken" apart
from any other startup failure therefore has to probe the bind itself.

That probe carries real platform subtlety (see ``port_bind_conflict``), so it
lives here rather than being reimplemented per server: ``web_server`` keeps
its long-standing private aliases pointing at these functions, and
``mg_server`` imports them directly instead of importing the whole
20k-line ``web_server`` module just to reach them.
"""

from __future__ import annotations

import errno
import socket
import sys

# Distinct exit code so launchers and supervisors can tell "someone else is
# already serving this port" from a generic crash (mirrors gateway/restart.py
# and kanban_db.py's quota-wall sentinel).
PORT_IN_USE_EXIT_CODE = 75

# POSIX, Linux, macOS, WinSock spellings of EADDRINUSE.
_ADDR_IN_USE_CODES = frozenset({errno.EADDRINUSE, 98, 48, 10048})


def is_addr_in_use_error(exc: OSError) -> bool:
    """True when ``exc`` is the platform's address-in-use bind failure."""
    if exc.errno in _ADDR_IN_USE_CODES:
        return True
    return getattr(exc, "winerror", None) == 10048  # WSAEADDRINUSE


def port_bind_conflict(host: str, port: int) -> bool:
    """Probe whether binding ``host:port`` would fail with EADDRINUSE.

    ``port == 0`` (ephemeral) can never conflict — the kernel picks a free
    port — so the probe is skipped and ``--port 0`` behaves exactly as
    before. Any probe error other than address-in-use returns ``False`` so
    uvicorn surfaces it with its normal diagnostics (bad host, EACCES, …).
    """
    if not port:
        return False

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        probe = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        return False
    try:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if sys.platform == "win32" and exclusive is not None:
            # Windows: SO_REUSEADDR means "bind over anyone" — a probe (or
            # uvicorn bind) with it SUCCEEDS on top of a live LISTEN socket,
            # so it can never detect a conflict. SO_EXCLUSIVEADDRUSE makes
            # the probe fail with WSAEADDRINUSE exactly when another socket
            # holds the port (the reporter's 10048 shape in #93608).
            probe.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        else:
            # POSIX: match uvicorn's bind flags (uvicorn/config.py
            # bind_socket) so the probe conflicts exactly when uvicorn's own
            # bind would: SO_REUSEADDR lets TIME_WAIT remnants pass while a
            # live LISTEN socket still fails.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))
    except OSError as exc:
        return is_addr_in_use_error(exc)
    except Exception:
        return False
    finally:
        probe.close()
    return False
