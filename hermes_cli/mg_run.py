"""In-process task runner for the Model Groups page's live flow.

``start()`` spawns ``cli.py -q <prompt> --oneshot`` as a subprocess and a
reader thread that turns its stdout into a live list of *stages*
(``asamalar``) the UI polls every ~1.5 s — mirroring an async task API:
POST returns a ``gid``, GET returns ``{durum, asamalar, cikti, sonuc}``,
and there is an ``iptal`` (cancel / kill) endpoint.

Stage parsing is heuristic (Hermes has no structured stdout): shell tool
lines, the answer box, the trailing ``Session:`` summary, error markers.
Fully local, single trusted operator — same trust model as ``hermes mg``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

_LOCK = threading.Lock()
_TASKS: Dict[str, Dict[str, Any]] = {}
_MAX_KEEP = 40

_RE_TOOL = re.compile(r"(?:┊\s*)?💻\s*\$?\s*(.+?)\s*(?:\b\d+(?:\.\d+)?s)?\s*$")
_RE_BOXTOP = re.compile(r"╭[─-].*Hermes|╭[─-]{4,}")
_RE_BOXBOT = re.compile(r"^\s*╰[─-]{4,}")
_RE_SESSION = re.compile(r"^\s*Session:\s*(\S+)")
_RE_DURATION = re.compile(r"^\s*Duration:\s*(.+)$")
_RE_MSGS = re.compile(r"^\s*Messages:\s*(.+)$")
_RE_ERR = re.compile(r"(traceback \(most recent call last\)|error:|exception|fatal)", re.I)
_RE_FALLBACK_HOLD = re.compile(r"Kademe yukseltme izni bekliyor|fallback_approval", re.I)


def _cli_py() -> Path:
    import hermes_cli

    return Path(hermes_cli.__file__).parent.parent / "cli.py"


def _ipc_dir() -> Path:
    from hermes_cli.config import get_config_path

    d = Path(get_config_path()).parent / "mg_ipc"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _add_stage(rec: Dict[str, Any], no: str, ad: str, durum: str, detay: str = "") -> None:
    rec["asamalar"].append({"no": no, "ad": ad, "durum": durum, "detay": detay[:200]})
    # keep the previous "calisiyor" row from staying live forever
    for s in rec["asamalar"][:-1]:
        if s["durum"] == "calisiyor":
            s["durum"] = "ok"


def _reader(gid: str, proc: subprocess.Popen) -> None:
    rec = _TASKS[gid]
    n = 1
    box_seen = False
    answer: List[str] = []
    in_box = False
    try:
        for raw in iter(proc.stdout.readline, ""):
            line = raw.rstrip("\n")
            with _LOCK:
                rec["cikti"].append(line)
                if len(rec["cikti"]) > 4000:
                    rec["cikti"] = rec["cikti"][-3000:]

                s = line.strip()
                if _RE_FALLBACK_HOLD.search(line):
                    rec["durum"] = "onay_bekliyor"
                    rec["onay_tipi"] = "kademe_gecis"
                    _add_stage(rec, str(n), "KADEME İZNİ", "beklemede", s); n += 1
                elif _RE_BOXBOT.match(line):
                    in_box = False
                elif _RE_BOXTOP.search(line):
                    box_seen = True
                    in_box = True
                    _add_stage(rec, str(n), "TESLİM", "calisiyor", "cevap yazılıyor"); n += 1
                elif _RE_SESSION.search(line):
                    in_box = False
                    m = _RE_SESSION.search(line)
                    rec["session_id"] = m.group(1)
                    _add_stage(rec, str(n), "BİTTİ", "ok", "oturum " + m.group(1)); n += 1
                elif _RE_DURATION.search(line):
                    rec["sure"] = _RE_DURATION.search(line).group(1).strip()
                elif _RE_MSGS.search(line):
                    rec["mesaj_sayisi"] = _RE_MSGS.search(line).group(1).strip()
                elif ("💻" in line or "┊" in line) and "preparing terminal" not in line:
                    mt = _RE_TOOL.search(line)
                    if mt:
                        _add_stage(rec, str(n), "ARAÇ", "ok", mt.group(1)); n += 1
                elif s.startswith("Initializing agent"):
                    _add_stage(rec, str(n), "MODEL", "calisiyor", "ajan başlatıldı"); n += 1
                elif in_box and s and not s.startswith(("╭", "╰", "─", "Resume this")):
                    answer.append(s.strip("│ ").strip())
                elif _RE_ERR.search(line):
                    _add_stage(rec, str(n), "HATA", "hata", s[:180]); n += 1
                    rec["hata"] = s[:300]
    except Exception as exc:  # pragma: no cover
        with _LOCK:
            rec["hata"] = f"reader: {exc}"
    finally:
        proc.wait()
        with _LOCK:
            for s in rec["asamalar"]:
                if s["durum"] == "calisiyor":
                    s["durum"] = "ok"
            rec["returncode"] = proc.returncode
            ans = "\n".join([a for a in answer if a]).strip()
            tam = "\n".join(rec["cikti"]).strip()
            if not ans:
                # fallback: text between last box-top and the Resume/Session tail
                lines = rec["cikti"]
                top = max((i for i, l in enumerate(lines) if _RE_BOXTOP.search(l)), default=-1)
                if top >= 0:
                    buf = []
                    for l in lines[top + 1:]:
                        if _RE_SESSION.search(l) or l.strip().startswith("Resume this"):
                            break
                        t = l.strip().strip("│╭╰─ ").strip()
                        if t:
                            buf.append(t)
                    ans = "\n".join(buf).strip()
            rec["sonuc"] = {"cevap": ans or "(cevap ayrıştırılamadı — ham çıktıya bak)", "ham": tam[-8000:]}
            if rec["durum"] == "onay_bekliyor":
                pass  # keep awaiting
            elif rec.get("hata") or (proc.returncode not in (0, None)):
                rec["durum"] = "hata"
            elif rec["durum"] != "iptal":
                # Teslim onay kapisı: görev başarılı bittiğinde otomatik
                # "bitti" yazılmaz — kullanıcı mg sayfasında sonucu inceleyip
                # [TESLIM ET]/[REDDET] ile kararı verir (bkz. teslim()).
                rec["durum"] = "teslim_bekliyor"
            rec["bitti_at"] = time.time()


def start(prompt: str, *, group: Optional[str] = None, profile: str = "current",
          auto_escalate: bool = False, timeout: int = 900) -> str:
    gid = uuid.uuid4().hex[:8]
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    ipc = _ipc_dir()
    env["HERMES_MG_GID"] = gid
    env["HERMES_MG_IPC"] = str(ipc)
    for suf in (".pending", ".resp"):
        try:
            (ipc / (gid + suf)).unlink()
        except Exception:
            pass
    if auto_escalate:
        env["HERMES_FALLBACK_APPROVE"] = "1"
    if profile and profile != "current":
        try:
            from hermes_cli import profiles as _pm

            env["HERMES_HOME"] = str(_pm.get_profile_dir(profile))
        except Exception:
            pass
    cli = _cli_py()
    proc = subprocess.Popen(
        [sys.executable, str(cli), "-q", prompt, "--oneshot"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
        env=env, cwd=env.get("HERMES_HOME") or None, bufsize=1,
    )
    rec = {
        "gid": gid, "prompt": prompt, "group": group, "profile": profile,
        "durum": "calisiyor", "asamalar": [
            {"no": "0", "ad": "CLIENT", "durum": "ok", "detay": "komut alındı"}
        ],
        "cikti": [], "sonuc": None, "hata": None, "baslangic": time.time(),
        "_proc": proc,
    }
    with _LOCK:
        _TASKS[gid] = rec
        if len(_TASKS) > _MAX_KEEP:
            for old in sorted(_TASKS, key=lambda k: _TASKS[k]["baslangic"])[:len(_TASKS) - _MAX_KEEP]:
                _TASKS.pop(old, None)
    threading.Thread(target=_reader, args=(gid, proc), daemon=True).start()

    def _guard() -> None:
        time.sleep(timeout)
        with _LOCK:
            r = _TASKS.get(gid)
        if r and r["durum"] == "calisiyor":
            try:
                proc.kill()
            except Exception:
                pass
            with _LOCK:
                r["durum"] = "hata"
                r["hata"] = f"zaman aşımı ({timeout}s)"
    threading.Thread(target=_guard, daemon=True).start()
    return gid


def get(gid: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        rec = _TASKS.get(gid)
        if not rec:
            return None
        return {k: v for k, v in rec.items() if not k.startswith("_")}


def respond(gid: str, karar: str) -> bool:
    """UI's answer to a pending approval: 'approve' | 'deny'."""
    gid = gid.strip()
    with _LOCK:
        rec = _TASKS.get(gid)
    if rec is None:
        return False
    karar = "approve" if str(karar).strip().lower() in {"approve", "onay", "evet", "yes", "1"} else "deny"
    try:
        (_ipc_dir() / (gid + ".resp")).write_text(karar, encoding="utf-8")
    except Exception:
        return False
    with _LOCK:
        _add_stage(rec, "!", "KULLANICI", "ok" if karar == "approve" else "hata",
                   "onayladı — devam" if karar == "approve" else "reddetti — dur")
        rec["durum"] = "calisiyor" if karar == "approve" else rec["durum"]
        rec.pop("onay_tipi", None)
    if karar == "deny":
        # denied escalation → the gate returns False and the run ends; also
        # nudge the process if it lingers
        threading.Thread(target=lambda: (time.sleep(20), cancel(gid)), daemon=True).start()
    return True


def teslim(gid: str, karar: str) -> bool:
    """UI's delivery decision on a 'teslim_bekliyor' task:
    'approve' -> durum 'bitti'; 'reject' -> durum 'iptal' (Kaldigi Yerden
    Devam akışı yeniden koşturabilir)."""
    gid = gid.strip()
    with _LOCK:
        rec = _TASKS.get(gid)
        if rec is None or rec.get("durum") != "teslim_bekliyor":
            return False
        ok = karar.strip().lower() in {"approve", "onay", "evet", "yes", "1"}
        rec["durum"] = "bitti" if ok else "iptal"
        _add_stage(rec, "!", "TESLİM", "ok" if ok else "hata",
                   "kullanıcı teslimi onayladı" if ok else "kullanıcı teslimi reddetti")
        rec.pop("onay_tipi", None)
    return True


def cancel(gid: str) -> bool:
    with _LOCK:
        rec = _TASKS.get(gid)
        if not rec:
            return False
        proc = rec.get("_proc")
    try:
        (_ipc_dir() / (gid + ".resp")).write_text("deny", encoding="utf-8")
    except Exception:
        pass
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            time.sleep(0.4)
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
    with _LOCK:
        rec["durum"] = "iptal"
        _add_stage(rec, "!", "İPTAL", "hata", "kullanıcı durdurdu")
    return True
