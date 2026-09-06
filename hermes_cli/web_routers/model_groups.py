"""Model group (agent architecture) manager — a dashboard panel.

A *group* is a named, ordered list of models the agent walks in sequence:
``group[0]`` is the primary (written to ``config.model``), and
``group[1:]`` become ``config.fallback_model`` so the engine's existing
failover chain (``agent._fallback_chain``) tries the next model on
error / rate-limit / classified failure / empty response. A single-model
group = one agent, no fallback. Each model may carry a free-text ``role``
tag (e.g. ``öğrenci`` / ``hakem``).

Groups are shared and defined once; **which group is active is per
profile**, so every project (Hermes profile) picks its own architecture.
Activating writes into that profile's ``config.yaml`` through the same
``read_raw_config`` + ``save_config`` path the dashboard config form uses
(scoped via ``_config_profile_scope``), preserving line endings and
unrelated sections. Re-activating any time re-writes it — live, updatable.

Store: ``<config dir>/model_groups.json`` ::

    {"groups": {"<name>": [{provider, model, role?}, ...]},
     "active": {"<profile>": "<group name>", ...}}
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

router = APIRouter()

_LOCK = threading.Lock()

# Sentinel profile key for "the home this dashboard process runs as".
_CURRENT = "current"


# ── profile helpers ──────────────────────────────────────────────────────

def _norm_profile(profile: Optional[str]) -> str:
    p = (profile or "").strip()
    if not p or p.lower() == _CURRENT:
        return _CURRENT
    try:
        from hermes_cli import profiles as _pm

        return _pm.normalize_profile_name(p) or _CURRENT
    except Exception:
        return p


def _profile_config_scope(profile: str):
    """Context manager: retarget config reads/writes to ``profile``'s home.
    Falls back to a no-op when the helper or profile is unavailable."""
    import contextlib

    if profile == _CURRENT:
        return contextlib.nullcontext()
    try:
        from hermes_cli.web_server import _config_profile_scope

        return _config_profile_scope(profile)
    except Exception:
        return contextlib.nullcontext()


# ── persistence ──────────────────────────────────────────────────────────

def _store_path() -> Path:
    from hermes_cli.config import get_config_path

    return Path(get_config_path()).parent / "model_groups.json"


def _empty() -> Dict[str, Any]:
    return {"groups": {}, "active": {}}


def _load() -> Dict[str, Any]:
    try:
        raw = json.loads(_store_path().read_text(encoding="utf-8"))
    except Exception:
        return _empty()
    if not isinstance(raw, dict):
        return _empty()
    groups = raw.get("groups")
    groups = groups if isinstance(groups, dict) else {}
    active = raw.get("active")
    if isinstance(active, str):  # migrate old flat {active: "x", groups: {}}
        active = {_CURRENT: active} if active else {}
    elif not isinstance(active, dict):
        active = {}
    return {"groups": groups, "active": active}


def _save(data: Dict[str, Any]) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _clean_models(models: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen = set()
    for m in models or []:
        if not isinstance(m, dict):
            continue
        prov = str(m.get("provider") or "").strip()
        mid = str(m.get("model") or "").strip()
        if not prov or not mid:
            continue
        key = (prov, mid)
        if key in seen:
            continue
        seen.add(key)
        row = {"provider": prov, "model": mid}
        role = str(m.get("role") or "").strip()[:32]
        if role:
            row["role"] = role
        out.append(row)
    return out


# ── config wiring ────────────────────────────────────────────────────────

def _norm_provider(provider: str) -> str:
    """Catalog slugs for user-defined endpoints are ``custom:<host>``; the
    config schema wants the base type ``custom`` (the specific endpoint is
    resolved via base_url / custom_providers). Built-in slugs pass through."""
    p = (provider or "").strip()
    return "custom" if p.startswith("custom:") else p


def _apply_to_config(models: List[Dict[str, str]], profile: str) -> None:
    """group[0] -> config.model, group[1:] -> config.fallback_model, in the
    given profile's scope."""
    if not models:
        raise HTTPException(status_code=400, detail="Grup boş — en az bir model gerekli.")
    from hermes_cli.config import read_raw_config, save_config

    g0, rest = models[0], models[1:]
    with _profile_config_scope(profile):
        cfg = read_raw_config() or {}
        model_block = cfg.get("model")
        if not isinstance(model_block, dict):
            model_block = {}
        model_block["provider"] = _norm_provider(g0["provider"])
        model_block["default"] = g0["model"]
        cfg["model"] = model_block

        # base_url for `custom` fallback entries: prefer the primary model
        # block's, else the first configured custom_provider's — so a custom
        # fallback still resolves even when the primary is a built-in
        # provider (which carries no base_url).
        _base_url = model_block.get("base_url")
        if not _base_url:
            for cp in (cfg.get("custom_providers") or []):
                if isinstance(cp, dict) and cp.get("base_url"):
                    _base_url = cp["base_url"]
                    break
        fb: List[Dict[str, str]] = []
        for m in rest:
            prov = _norm_provider(m["provider"])
            entry = {"provider": prov, "model": m["model"]}
            if prov == "custom" and _base_url:
                entry["base_url"] = _base_url
            fb.append(entry)
        cfg["fallback_model"] = fb
        save_config(cfg)


# ── models ───────────────────────────────────────────────────────────────

class GroupBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    models: List[Dict[str, str]] = Field(default_factory=list)


class ActiveBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    profile: Optional[str] = None


class SettingsBody(BaseModel):
    fallback_approval: bool
    profile: Optional[str] = None


# ── API ──────────────────────────────────────────────────────────────────

@router.get("/api/model-groups/profiles")
async def list_arch_profiles() -> Dict[str, Any]:
    def _run() -> Dict[str, Any]:
        names: List[str] = []
        current = _CURRENT
        try:
            from hermes_cli import profiles as _pm

            names = list(_pm.list_profile_names() or [])
            current = _pm.get_active_profile_name() or _CURRENT
        except Exception:
            pass
        return {"profiles": names, "current": current}

    return await run_in_threadpool(_run)


@router.get("/api/model-groups")
async def list_groups(profile: Optional[str] = None) -> Dict[str, Any]:
    pf = _norm_profile(profile)
    with _LOCK:
        data = _load()
    active = data["active"].get(pf, "")
    return {
        "profile": pf,
        "groups": data["groups"],
        "active": active,
        "active_models": data["groups"].get(active, []),
        "active_by_profile": data["active"],
    }


@router.post("/api/model-groups")
async def upsert_group(body: GroupBody) -> Dict[str, Any]:
    name = body.name.strip()
    models = _clean_models(body.models)
    with _LOCK:
        data = _load()
        data["groups"][name] = models
        _save(data)
    return {"ok": True, "name": name, "count": len(models)}


@router.delete("/api/model-groups/{name}")
async def delete_group(name: str) -> Dict[str, Any]:
    name = name.strip()
    with _LOCK:
        data = _load()
        if name not in data["groups"]:
            raise HTTPException(status_code=404, detail=f"Grup yok: {name}")
        del data["groups"][name]
        data["active"] = {k: v for k, v in data["active"].items() if v != name}
        _save(data)
    return {"ok": True}


@router.post("/api/model-groups/active")
async def set_active(body: ActiveBody) -> Dict[str, Any]:
    name = body.name.strip()
    pf = _norm_profile(body.profile)

    def _run() -> Dict[str, Any]:
        with _LOCK:
            data = _load()
            if name not in data["groups"]:
                raise HTTPException(status_code=404, detail=f"Grup yok: {name}")
            models = _clean_models(data["groups"][name])
            _apply_to_config(models, pf)
            data["active"][pf] = name
            data["groups"][name] = models
            _save(data)
        return {
            "ok": True,
            "profile": pf,
            "active": name,
            "primary": models[0],
            "fallback": models[1:],
        }

    return await run_in_threadpool(_run)


@router.get("/api/model-groups/settings")
async def get_settings(profile: Optional[str] = None) -> Dict[str, Any]:
    pf = _norm_profile(profile)

    def _run() -> Dict[str, Any]:
        from hermes_cli.config import read_raw_config

        with _profile_config_scope(pf):
            cfg = read_raw_config() or {}
        agent = cfg.get("agent") if isinstance(cfg.get("agent"), dict) else {}
        return {"profile": pf, "fallback_approval": bool(agent.get("fallback_approval", False))}

    return await run_in_threadpool(_run)


@router.post("/api/model-groups/settings")
async def set_settings(body: SettingsBody) -> Dict[str, Any]:
    pf = _norm_profile(body.profile)

    def _run() -> Dict[str, Any]:
        from hermes_cli.config import read_raw_config, save_config

        with _profile_config_scope(pf):
            cfg = read_raw_config() or {}
            agent = cfg.get("agent")
            if not isinstance(agent, dict):
                agent = {}
            agent["fallback_approval"] = bool(body.fallback_approval)
            cfg["agent"] = agent
            save_config(cfg)
        return {"ok": True, "profile": pf, "fallback_approval": bool(body.fallback_approval)}

    return await run_in_threadpool(_run)


# ── canlı kullanım (token / tahmini maliyet) ─────────────────────────────
# state.db'yi read-only okur; hiçbir yazma yapmaz. state.db aynı anda başka
# süreçler tarafından kullanılabileceğinden her hata fail-open boş veriye
# döner — sayfa maliyet kartı olmadan da çalışmaya devam eder.

@router.get("/api/model-groups/kullanim")
async def kullanim(profile: Optional[str] = None) -> Dict[str, Any]:
    pf = _norm_profile(profile)

    def _run() -> Dict[str, Any]:
        try:
            import sqlite3
            import time as _t

            from hermes_constants import get_hermes_home

            db_path = Path(get_hermes_home()) / "state.db"
            if not db_path.exists():
                return {"ok": False, "reason": "state.db yok"}
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
            try:
                midnight = float(_t.mktime(_t.strptime(_t.strftime("%Y-%m-%d"), "%Y-%m-%d")))
                tot = con.execute(
                    "SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),"
                    " COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(estimated_cost_usd),0)"
                    " FROM sessions WHERE started_at >= ?",
                    (midnight,),
                ).fetchone()
                by_model = con.execute(
                    "SELECT COALESCE(model,'?'), COUNT(*), COALESCE(SUM(input_tokens),0),"
                    " COALESCE(SUM(output_tokens),0), COALESCE(SUM(estimated_cost_usd),0)"
                    " FROM sessions WHERE started_at >= ? GROUP BY model ORDER BY 5 DESC LIMIT 12",
                    (midnight,),
                ).fetchall()
                recent = con.execute(
                    "SELECT substr(COALESCE(title,''),1,40), COALESCE(model,'?'),"
                    " COALESCE(estimated_cost_usd,0), COALESCE(api_call_count,0), started_at"
                    " FROM sessions ORDER BY started_at DESC LIMIT 8"
                ).fetchall()
                return {
                    "ok": True,
                    "profile": pf,
                    "bugun": {
                        "oturum": tot[0], "giris": tot[1], "cikis": tot[2],
                        "cache_okuma": tot[3], "tahmini_maliyet_usd": round(tot[4] or 0.0, 4),
                    },
                    "modeller": [
                        {"model": m[0], "oturum": m[1], "giris": m[2], "cikis": m[3],
                         "tahmini_maliyet_usd": round(m[4] or 0.0, 4)}
                        for m in by_model
                    ],
                    "son_oturumlar": [
                        {"baslik": r[0], "model": r[1], "tahmini_maliyet_usd": round(r[2] or 0.0, 4),
                         "api": r[3], "zaman": r[4]}
                        for r in recent
                    ],
                }
            finally:
                con.close()
        except Exception as exc:
            return {"ok": False, "reason": str(exc)[:120]}

    return await run_in_threadpool(_run)


# ── sunucu taraflı sohbet geçmişi ────────────────────────────────────────
# mg sayfası çok tarayıcılı kullanım için: CHAT/HIST localStorage yerine
# (ve onunla birlikte) HERMES_HOME/mg_sohbet.json'a kaydedilir; açılışta
# sunucudaki kopya yüklenir — hangi tarayıcıdan açılırsa açılsın geçmiş gelir.

@router.get("/api/model-groups/sohbet")
async def sohbet_get() -> Dict[str, Any]:
    def _run() -> Dict[str, Any]:
        try:
            from hermes_constants import get_hermes_home
            p = Path(get_hermes_home()) / "mg_sohbet.json"
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                return {"ok": True, **(data if isinstance(data, dict) else {})}
            return {"ok": True, "chat": [], "hist": []}
        except Exception as exc:
            return {"ok": False, "reason": str(exc)[:120], "chat": [], "hist": []}
    return await run_in_threadpool(_run)


class SohbetBody(BaseModel):
    chat: List[Any] = Field(default_factory=list)
    hist: List[Any] = Field(default_factory=list)


@router.post("/api/model-groups/sohbet")
async def sohbet_post(body: SohbetBody) -> Dict[str, Any]:
    def _run() -> Dict[str, Any]:
        try:
            from hermes_constants import get_hermes_home
            p = Path(get_hermes_home()) / "mg_sohbet.json"
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps({"chat": body.chat, "hist": body.hist}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "reason": str(exc)[:120]}
    return await run_in_threadpool(_run)


@router.get("/api/model-groups/catalog")
async def catalog(refresh: bool = False, profile: Optional[str] = None, all: bool = False) -> Dict[str, Any]:
    pf = _norm_profile(profile)

    def _build() -> Dict[str, Any]:
        from hermes_cli.inventory import (
            build_model_options_payload,
            load_picker_context,
        )

        with _profile_config_scope(pf):
            payload = build_model_options_payload(
                load_picker_context(), refresh=bool(refresh)
            )
        rows: List[Dict[str, str]] = []
        for prov in payload.get("providers", []):
            slug = str(prov.get("slug") or "")
            if not slug:
                continue
            # Varsayilan: yalnizca operatorun kendi saglayicisi (custom/haimaker)
            # gosterilir — openrouter vb. gurultu katalogda yer kaplamaz.
            # Istisna: ?all=1 ile tum saglayicilar listelenebilir.
            if not all and not slug.startswith("custom"):
                continue
            for mid in prov.get("models", []):
                rows.append({"provider": slug, "model": str(mid), "id": f"{slug}/{mid}"})
        return {"count": len(rows), "models": rows}

    return await run_in_threadpool(_build)


# ── live task run (async: POST -> gid, GET -> stages, iptal) ─────────────

class GorevBody(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=16000)
    group: Optional[str] = None
    profile: Optional[str] = None
    # "sorma, üst kademeye kendin geç" — fallback_approval kapısını bu koşu
    # için önceden geçer (HERMES_FALLBACK_APPROVE=1). Kapıyı KURMAK için değil,
    # bu koşuda BYPASS etmek için. Sayfa göndermez; canlı onay /onay ucuyla.
    auto_escalate: bool = False


@router.post("/api/model-groups/gorev")
async def gorev_baslat(body: GorevBody) -> Dict[str, Any]:
    from hermes_cli import mg_run

    pf = _norm_profile(body.profile)
    prompt = body.prompt.strip()
    grp = (body.group or "").strip()

    def _run() -> Dict[str, Any]:
        activated = None
        if grp:
            with _LOCK:
                data = _load()
                if grp not in data["groups"]:
                    raise HTTPException(status_code=404, detail=f"Grup yok: {grp}")
                models = _clean_models(data["groups"][grp])
                _apply_to_config(models, pf)
                data["active"][pf] = grp
                data["groups"][grp] = models
                _save(data)
            activated = grp
        gid = mg_run.start(
            prompt, group=grp or None, profile=pf,
            auto_escalate=bool(body.auto_escalate),
        )
        return {"ok": True, "gorev_id": gid, "profile": pf, "activated_group": activated}

    return await run_in_threadpool(_run)


@router.get("/api/model-groups/gorev/{gid}")
async def gorev_durum(gid: str) -> Dict[str, Any]:
    from hermes_cli import mg_run

    rec = mg_run.get(gid.strip())
    if rec is None:
        raise HTTPException(status_code=404, detail="Görev yok")
    return rec


@router.post("/api/model-groups/gorev/{gid}/iptal")
async def gorev_iptal(gid: str) -> Dict[str, Any]:
    from hermes_cli import mg_run

    ok = mg_run.cancel(gid.strip())
    if not ok:
        raise HTTPException(status_code=404, detail="Görev yok")
    return {"ok": True}


class OnayBody(BaseModel):
    karar: str = Field(..., pattern="^(approve|deny|onay|red|evet|hayir)$")


@router.post("/api/model-groups/gorev/{gid}/onay")
async def gorev_onay(gid: str, body: OnayBody) -> Dict[str, Any]:
    from hermes_cli import mg_run

    ok = mg_run.respond(gid.strip(), body.karar)
    if not ok:
        raise HTTPException(status_code=404, detail="Görev yok")
    return {"ok": True, "karar": body.karar}


# ── page ─────────────────────────────────────────────────────────────────

_PAGE = """<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Model Grupları — Hermes</title>
<style>
:root{color-scheme:dark;--bg:#0b0b0d;--panel:#151518;--panel2:#1b1b1f;--line:#2a2a30;--ink:#ececf0;--dim:#a0a0aa;--faint:#6b6b76;--acc:#e0b64b;--accink:#1a1400;--ok:#4ec77f;--bad:#e0685f;--warn:#e0b64b}
*{box-sizing:border-box}
body{margin:0;height:100vh;display:flex;flex-direction:column;overflow:hidden;background:var(--bg);color:var(--ink);font:14px/1.5 ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif}
*::-webkit-scrollbar{width:8px;height:8px}*::-webkit-scrollbar-thumb{background:var(--line);border-radius:8px}
header{height:52px;display:flex;align-items:center;gap:10px;padding:0 14px;border-bottom:1px solid var(--line);flex-shrink:0}
.mark{width:30px;height:30px;border-radius:8px;background:linear-gradient(145deg,var(--acc),#b8892e);color:var(--accink);display:flex;align-items:center;justify-content:center;font-weight:900;font-size:13px}
header h1{font-size:14px;margin:0;font-weight:600}
header .sub{font-size:10px;color:var(--faint);text-transform:uppercase;letter-spacing:.08em}
.spacer{flex:1}
.tab{background:var(--panel);border:1px solid var(--line);border-radius:999px;color:var(--dim);padding:5px 12px;font:inherit;font-size:12px;cursor:pointer}
.tab.on{background:var(--acc);color:var(--accink);font-weight:700;border-color:var(--acc)}
select{background:var(--bg);border:1px solid var(--line);color:var(--ink);border-radius:8px;padding:6px 8px;font:inherit;font-size:12px}
button{background:var(--acc);color:var(--accink);border:0;border-radius:8px;padding:7px 12px;font:inherit;font-weight:600;cursor:pointer}
button.ghost{background:var(--panel2);color:var(--dim)}
button.bad{background:#3a1f22;color:#ff8b8b}
button.good{background:var(--ok);color:#04140a}
button:disabled{opacity:.5;cursor:not-allowed}
input[type=text],textarea{background:var(--bg);border:1px solid var(--line);color:var(--ink);border-radius:10px;padding:9px 11px;font:inherit;width:100%}
textarea{resize:vertical;min-height:48px}
.view{flex:1;min-height:0;display:none}
.view.on{display:flex}
aside{width:240px;border-right:1px solid var(--line);background:#0f0f10;display:flex;flex-direction:column;flex-shrink:0}
aside .hd{display:flex;justify-content:space-between;align-items:center;padding:10px 12px;font-size:11px;color:var(--faint)}
#hist{flex:1;overflow:auto;padding:0 8px 8px}
#hist button{width:100%;text-align:left;background:transparent;color:var(--ink);border:0;border-radius:8px;padding:8px;font-weight:400}
#hist button:hover{background:#17171a}
#hist .t{font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#hist .p{font-size:11px;color:var(--faint)}
main{flex:1;display:flex;flex-direction:column;min-width:0}
#scroll{flex:1;overflow:auto}
#chat{max-width:760px;margin:0 auto;width:100%;padding:18px 16px;display:flex;flex-direction:column;gap:14px}
.empty{text-align:center;color:var(--faint);padding:60px 0;font-size:13px}
.msg .who{font-size:11px;color:var(--faint);margin-bottom:3px}
.msg.u .bub{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:9px 12px;font-size:13px;white-space:pre-wrap}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px}
.rail{display:flex;align-items:center;margin:2px 0 10px}
.rn{display:flex;flex-direction:column;align-items:center;gap:3px}
.rd{width:9px;height:9px;border-radius:50%;background:var(--line);border:1px solid var(--line)}
.rn.done .rd{background:var(--ok);border-color:var(--ok)}
.rn.active .rd{background:var(--acc);border-color:var(--acc);animation:pd 1s infinite}
.rl{font:600 8px/1 ui-monospace,monospace;color:var(--faint);text-transform:uppercase}
.rn.done .rl,.rn.active .rl{color:var(--dim)}
.rs{flex:1;height:2px;background:var(--line);min-width:18px}
.rs.done{background:var(--ok)}
@keyframes pd{50%{opacity:.35;transform:scale(.7)}}
@keyframes sweep{0%{background-position:-160% 0}100%{background-position:260% 0}}
.flow{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:8px;max-height:300px;overflow:auto}
.st{display:flex;gap:8px;align-items:baseline;padding:2px 0;font-size:12px;animation:rowin .2s ease both}
@keyframes rowin{from{opacity:0;transform:translateY(5px)}}
.st .tg{font:600 10px/1.4 ui-monospace,monospace;color:var(--faint);min-width:24px;text-align:right}
.st .nm{font-weight:600}.st .dt{color:var(--faint);font-size:11px}
.st[data-s="calisiyor"]{border-left:2px solid var(--acc);padding-left:8px;margin-left:-10px;background:linear-gradient(90deg,rgba(224,182,75,.10),transparent 55%);background-size:200% 100%;animation:rowin .2s ease both,sweep 1.4s linear infinite}
.st[data-s="hata"]{border-left:2px solid var(--bad);padding-left:8px;margin-left:-10px}
.st[data-s="ok"] .nm{color:var(--ok)}
.st[data-s="beklemede"] .nm{color:var(--warn)}
.intv{margin-top:10px;background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:10px}
.intv .lbl{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px}
.intv .btns{display:flex;gap:8px;flex-wrap:wrap}
.ans{margin-top:8px;background:#101a12;border:1px solid #244f2f;border-radius:10px;padding:9px;font-size:13px;white-space:pre-wrap;color:#d7dbe2}
.ans.err{background:#1e1112;border-color:#4f2626;color:#ff8b8b}
.onaybox{margin-top:10px;background:#241d0d;border:1px solid #4a3c14;border-radius:12px;padding:10px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--acc);display:inline-block;animation:pd 1s infinite;margin-right:6px}
.inbar{border-top:1px solid var(--line);background:#0f0f10;padding:10px 14px}
.inbar .wrap2{max-width:760px;margin:0 auto}
.inrow{display:flex;gap:8px}
.inmeta{margin-top:6px;display:flex;gap:8px;align-items:center;font-size:11px;color:var(--faint)}
#viewGroups{padding:16px;overflow:auto}
.gwrap{max-width:1180px;margin:0 auto;display:grid;grid-template-columns:1fr 1fr;gap:16px}
.gcard{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
.gcard h2{font-size:12px;margin:0 0 10px;color:var(--dim);text-transform:uppercase;letter-spacing:.04em}
.list{max-height:60vh;overflow:auto;border:1px solid var(--line);border-radius:8px;margin-top:8px}
.rw{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:7px 10px;border-bottom:1px solid #1d232b}
.rw:last-child{border-bottom:0}.rw code{font-size:12px;color:#d7dbe2;word-break:break-all}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
.ord{color:var(--acc);font-variant-numeric:tabular-nums;margin-right:6px}
input.role{width:88px;padding:4px 6px;font-size:12px;margin-right:4px}
.muted{color:var(--faint);font-size:12px;margin:6px 0}
.pill{background:#1e2530;border:1px solid #2c3540;border-radius:999px;padding:2px 8px;font-size:11px;color:#cdd3db}
.ok{color:var(--ok)}.err{color:#ff8b8b}
</style></head><body>
<header>
  <div class="mark">UB</div>
  <div><h1>Model Gruplari</h1><div class="sub">canli akis</div></div>
  <button class="tab on" id="tabChat">Sohbet</button>
  <button class="tab" id="tabGroups">Gruplar &amp; Katalog</button>
  <span class="spacer"></span>
  <label class="sub">profil <select id="profileSel"><option value="current">current</option></select></label>
  <label class="sub"><input type="checkbox" id="fbApproval"> kademe izni</label>
  <div id="costChip" class="sub" title="bugunun tahmini harcamasi (tikla: ayrinti)" style="cursor:pointer;background:var(--panel2);border:1px solid var(--line);border-radius:999px;padding:5px 12px;font-size:12px;color:var(--acc)">$ —</div>
</header>

<div class="view on" id="viewChat">
  <aside>
    <div style="padding:10px 12px"><button id="newChat" style="width:100%">+ Yeni Sohbet</button></div>
    <div class="hd"><span>GECMIS</span><button class="ghost" id="clrHist" style="padding:3px 8px;font-size:11px">Temizle</button></div>
    <div id="hist"></div>
  </aside>
  <main>
    <div id="scroll"><div id="chat"><div class="empty" id="empty">Gorev ver - canli akisi izle, diledigin an dur / iptal et. Kademe gecisinde onay ister.</div></div></div>
    <div id="taskbar" style="display:none;position:fixed;bottom:112px;left:0;right:0;z-index:60"></div>
    <div class="inbar"><div class="wrap2">
      <div class="inmeta"><span>bu mesajin mimarisi:</span>
        <select id="chatGroup"><option value="">(aktif grup)</option></select>
        <span id="chatInfo"></span></div>
      <div class="inrow" style="margin-top:6px">
        <textarea id="prompt" placeholder="prompt yaz... (Ctrl+Enter gonderir)"></textarea>
        <button id="send">Gonder</button>
      </div>
    </div></div>
  </main>
</div>

<div class="view" id="viewGroups">
 <div class="gwrap">
  <div class="gcard">
    <h2>Model Katalogu <span class="pill" id="catN"></span></h2>
    <input id="filter" type="text" placeholder='filtre - orn. "deepseek", "flash"'>
    <div class="muted">Modele bas -&gt; sagdaki gruba eklenir. <a href="#" id="refresh" style="color:var(--acc)">canli yenile</a></div>
    <div id="cat" class="list"></div>
  </div>
  <div class="gcard">
    <h2>Grup <span class="pill" id="pfPill"></span></h2>
    <div class="bar">
      <select id="groupSel"><option value="">- yeni grup -</option></select>
      <button id="newBtn" class="ghost">+ Yeni</button><button id="delBtn" class="bad">Sil</button>
    </div>
    <div class="bar"><input id="groupName" type="text" placeholder="grup adi (baba, hizli, tekli)"><button id="saveBtn">Kaydet</button></div>
    <div class="muted">Bu profilin aktif grubu: <b id="activeName">-</b> <button id="activateBtn" class="ghost">bu profil icin aktif yap</button></div>
    <datalist id="roles"><option value="ogrenci"><option value="usta"><option value="hakem"></datalist>
    <div class="muted">rol: <b>ogrenci</b> = ogrenmeye beslenir - <b>hakem</b> = kalite - serbest</div>
    <div id="chosen" class="list"></div>
    <div id="gmsg" class="muted"></div>
  </div>
 </div>
</div>

<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?'':s).replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
async function j(u,o){const r=await fetch(u,o);if(!r.ok)throw new Error((await r.json().catch(()=>({}))).detail||r.status);return r.json()}
const q=p=>'?profile='+encodeURIComponent(p);
let PF='current', CAT=[], CHOSEN=[], DATA={groups:{},active:''};
let CHAT=JSON.parse(localStorage.getItem('mg_chat')||'[]');
let HIST=JSON.parse(localStorage.getItem('mg_hist')||'[]');
const saveChat=()=>{localStorage.setItem('mg_chat',JSON.stringify(CHAT));
  try{fetch('/api/model-groups/sohbet',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({chat:CHAT,hist:HIST})}).catch(()=>{})}catch(e){}};
const saveHist=()=>{localStorage.setItem('mg_hist',JSON.stringify(HIST));
  try{fetch('/api/model-groups/sohbet',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({chat:CHAT,hist:HIST})}).catch(()=>{})}catch(e){}};

$('#tabChat').onclick=()=>{$('#tabChat').classList.add('on');$('#tabGroups').classList.remove('on');$('#viewChat').classList.add('on');$('#viewGroups').classList.remove('on')};
$('#tabGroups').onclick=()=>{$('#tabGroups').classList.add('on');$('#tabChat').classList.remove('on');$('#viewGroups').classList.add('on');$('#viewChat').classList.remove('on')};

function renderHist(){
  $('#hist').innerHTML=HIST.length?HIST.map((h,i)=>`<button data-h="${i}"><div class="t">${esc(h.title)}</div><div class="p">${esc(h.preview)}</div></button>`).join(''):'<div class="muted" style="padding:8px">yok</div>';
}
const RAIL=['ALINDI','MODEL','ARAC','TESLIM'];
function railHtml(as){
  const has=k=>as.some(s=>new RegExp(k,'i').test(s.ad));
  const done=[true,has('MODEL'),has('ARAC|ARA\\u00c7'),as.some(s=>/BITTI|B\\u0130TT\\u0130/i.test(s.ad)&&s.durum==='ok')];
  const li=done.lastIndexOf(true);
  return '<div class="rail">'+RAIL.map((l,i)=>{
    const c=done[3]?'done':(i<li?'done':(i===Math.max(li,0)?'active':''));
    const seg=i<3?`<div class="rs ${done[3]||i<li?'done':''}"></div>`:'';
    return `<div class="rn ${c}"><span class="rd"></span><span class="rl">${l}</span></div>${seg}`;
  }).join('')+'</div>';
}
function gorevHtml(m){
  const r=m.rec||{},as=r.asamalar||[];
  const rows=as.map(s=>`<div class="st" data-s="${s.durum}"><span class="tg">${esc(s.no)}</span><span class="nm">${esc(s.ad)}</span><span class="dt">${esc(s.detay)}</span></div>`).join('')||'<div class="st" data-s="calisiyor"><span class="tg">.</span><span class="nm">basliyor</span></div>';
  const canli=r.durum==='calisiyor', bekl=r.durum==='onay_bekliyor';
  let intv='';
  if(canli||bekl){
    let b=`<button class="bad" data-iptal="${m.gid}">x Iptal / Dur</button>`;
    if(bekl) b=`<button class="good" data-onay="${m.gid}">Onayla - ust kademeye gec</button><button class="bad" data-red="${m.gid}">Reddet - dur</button>`;
    intv=`<div class="${bekl?'onaybox':'intv'}"><div class="lbl">${bekl?'<span class="dot"></span>KADEME GECIS ONAYI BEKLIYOR':'MUDAHALE'}</div><div class="btns">${b}</div></div>`;
  }
  const ans=(r.sonuc&&r.sonuc.cevap&&r.durum==='bitti')?`<div class="ans">${esc(r.sonuc.cevap)}</div>`:'';
  const devam=(r.durum==='iptal')?`<button class="ghost" data-devam="${m.gid}" style="margin-top:8px">&#8635; Kaldigi Yerden Devam</button>`:'';
  const hata=r.durum==='hata'?`<div class="ans err">HATA: ${esc(r.hata||'hata')}</div>`:(r.durum==='iptal'?`<div class="ans err">x durduruldu</div>${devam}`:'');
  const meta=[canli?'<span class="dot"></span>CALISIYOR':r.durum,(r.group?'['+r.group+']':''),r.sure||'',r.mesaj_sayisi||''].filter(Boolean).join(' - ');
  return `<div class="who">hermes - ${meta}</div><div class="card">${railHtml(as)}<div class="flow">${rows}</div>${intv}${ans}${hata}</div>`;
}
function renderChat(){
  const c=$('#chat');
  if(!CHAT.length){c.innerHTML='<div class="empty">Gorev ver - canli akisi izle.</div>';return}
  const keep={};c.querySelectorAll('.flow').forEach((f,i)=>keep[i]=f.scrollTop);
  c.innerHTML=CHAT.map(m=>m.role==='user'
    ?`<div class="msg u"><div class="who">sen${m.group?' - ['+esc(m.group)+']':''}</div><div class="bub">${esc(m.text)}</div></div>`
    :`<div class="msg">${gorevHtml(m)}</div>`).join('');
  c.querySelectorAll('.flow').forEach((f,i)=>{if(keep[i]!=null)f.scrollTop=keep[i]});
  const s=$('#scroll');if(s.scrollHeight-s.scrollTop-s.clientHeight<160)s.scrollTop=s.scrollHeight;
}
async function pollGorev(m){
  for(let i=0;i<1200;i++){
    let r;try{r=await j('/api/model-groups/gorev/'+m.gid)}catch(x){await new Promise(s=>setTimeout(s,1500));continue}
    m.rec=r;renderChat();saveChat();
    if(r.durum==='bitti'||r.durum==='hata'||r.durum==='iptal'){$('#send').disabled=false;
      HIST.unshift({title:(m.prompt||'').slice(0,44),preview:r.durum,chat:JSON.parse(JSON.stringify(CHAT))});HIST=HIST.slice(0,40);saveHist();renderHist();return}
    await new Promise(s=>setTimeout(s,1500));
  }
  $('#send').disabled=false;
}
async function send(opts){
  opts=opts||{};
  const p=(opts.prompt||$('#prompt').value).trim();if(!p)return;
  const g=opts.group!==undefined?opts.group:$('#chatGroup').value;
  $('#send').disabled=true;
  if(!opts.prompt){CHAT.push({role:'user',text:p,group:g});$('#prompt').value=''}
  const m={role:'gorev',gid:null,prompt:p,group:g,rec:{durum:'calisiyor',asamalar:[{no:'0',ad:'CLIENT',durum:'ok',detay:'gonderiliyor'}]}};
  CHAT.push(m);renderChat();saveChat();
  try{
    const r=await j('/api/model-groups/gorev',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({prompt:p,group:g||null,profile:PF})});
    m.gid=r.gorev_id;if(r.activated_group)loadForProfile();
    await pollGorev(m);
  }catch(x){m.rec={durum:'hata',asamalar:[],hata:x.message};renderChat();saveChat();$('#send').disabled=false}
}
$('#send').onclick=()=>send();
$('#prompt').addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();send()}});
$('#newChat').onclick=()=>{CHAT=[];saveChat();renderChat()};
$('#clrHist').onclick=()=>{HIST=[];saveHist();renderHist()};
$('#hist').addEventListener('click',e=>{const b=e.target.closest('[data-h]');if(!b)return;
  CHAT=JSON.parse(JSON.stringify(HIST[+b.dataset.h].chat));saveChat();renderChat()});
$('#chat').addEventListener('click',async e=>{
  const ip=e.target.closest('[data-iptal]'),on=e.target.closest('[data-onay]'),rd=e.target.closest('[data-red]'),dv=e.target.closest('[data-devam]');
  if(ip){try{await j('/api/model-groups/gorev/'+ip.dataset.iptal+'/iptal',{method:'POST'})}catch(x){}}
  if(on){try{await j('/api/model-groups/gorev/'+on.dataset.onay+'/onay',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({karar:'approve'})})}catch(x){}}
  if(rd){try{await j('/api/model-groups/gorev/'+rd.dataset.red+'/onay',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({karar:'deny'})})}catch(x){}}
  if(dv){
    const m=CHAT.find(x=>x.role==='gorev'&&x.gid===dv.dataset.devam);
    if(!m||$('#send').disabled)return;
    const r=m.rec||{};
    let tail='';
    if(r.cikti&&r.cikti.length)tail=r.cikti.slice(-40).join('\\n');
    else if(r.sonuc&&r.sonuc.ham)tail=String(r.sonuc.ham).slice(-1500);
    tail=tail.slice(-1500).trim()||'(yakalanan cikti yok)';
    const orig=(m.prompt||'').slice(0,300);
    const dp='ONCEKI GOREV (kullanici tarafindan durduruldu): "'+orig+'"\\n\\nKALDIGI YER - son cikti:\\n'+tail+'\\n\\nBu ciktiyi onceki calismanin devami olarak degerlendir, gorevi kaldigin yerden surdur ve tamamla.';
    CHAT.push({role:'user',text:'\\u21bb Kaldigi yerden devam ediliyor',group:m.rec&&m.rec.group});
    send({prompt:dp,group:(m.rec&&m.rec.group)||undefined});
  }
});

function renderCat(){
  const f=$('#filter').value.trim().toLowerCase();
  const rows=CAT.filter(m=>!f||m.id.toLowerCase().includes(f)).slice(0,400);
  $('#catN').textContent=CAT.length+' model';
  $('#cat').innerHTML=rows.map(m=>`<div class="rw"><code>${m.id}</code><button class="ghost" data-add='${m.provider}||${m.model}'>+ Ekle</button></div>`).join('')||'<div class="rw muted">eslesme yok</div>';
}
function renderChosen(){
  $('#chosen').innerHTML=CHOSEN.map((m,i)=>`<div class="rw"><span><span class="ord">${i+1}.</span><code>${m.provider}/${m.model}</code></span><span><input class="role" list="roles" data-role="${i}" placeholder="rol" value="${m.role||''}"><button class="ghost" data-mv="${i},-1">^</button><button class="ghost" data-mv="${i},1">v</button><button class="bad" data-rm="${i}">x</button></span></div>`).join('')||'<div class="rw muted">bos - soldan "+ Ekle"</div>';
}
function renderGroups(){
  const sel=$('#groupSel'),cur=sel.value;
  sel.innerHTML='<option value="">- yeni grup -</option>'+Object.keys(DATA.groups).map(n=>`<option${n===cur?' selected':''}>${n}</option>`).join('');
  $('#activeName').textContent=DATA.active||'-';$('#pfPill').textContent=PF;
  const cg=$('#chatGroup'),cgv=cg.value;
  cg.innerHTML='<option value="">(aktif grup: '+(DATA.active||'-')+')</option>'+Object.keys(DATA.groups).map(n=>`<option${n===cgv?' selected':''}>${n}</option>`).join('');
}
function loadGroup(name){CHOSEN=name&&DATA.groups[name]?DATA.groups[name].map(x=>({...x})):[];$('#groupName').value=name||'';renderChosen()}
const gmsg=(t,c)=>$('#gmsg').innerHTML=c?`<span class="${c}">${t}</span>`:t;
async function loadProfiles(){try{const p=await j('/api/model-groups/profiles');
  const o=['current',...p.profiles.filter(n=>n&&n!=='current')];
  $('#profileSel').innerHTML=o.map(n=>`<option${n===PF?' selected':''}>${n}</option>`).join('')}catch(x){}}
async function loadForProfile(){
  const [d,st]=await Promise.all([j('/api/model-groups'+q(PF)),j('/api/model-groups/settings'+q(PF))]);
  DATA=d;renderGroups();$('#fbApproval').checked=!!st.fallback_approval;loadGroup($('#groupSel').value);
}
async function loadAll(){await loadProfiles();const c=await j('/api/model-groups/catalog'+q(PF));CAT=c.models;renderCat();await loadForProfile()}
$('#profileSel').addEventListener('change',async e=>{PF=e.target.value;try{const c=await j('/api/model-groups/catalog'+q(PF));CAT=c.models;renderCat();await loadForProfile()}catch(x){gmsg(x.message,'err')}});
$('#filter').addEventListener('input',renderCat);
$('#refresh').addEventListener('click',async e=>{e.preventDefault();gmsg('yenileniyor...');try{CAT=(await j('/api/model-groups/catalog?refresh=true&profile='+encodeURIComponent(PF))).models;renderCat();gmsg('yenilendi','ok')}catch(x){gmsg(x.message,'err')}});
$('#cat').addEventListener('click',e=>{const b=e.target.closest('[data-add]');if(!b)return;const [p,m]=b.dataset.add.split('||');if(CHOSEN.some(x=>x.provider===p&&x.model===m)){gmsg('zaten var');return}CHOSEN.push({provider:p,model:m});renderChosen()});
$('#chosen').addEventListener('input',e=>{const r=e.target.closest('[data-role]');if(r)CHOSEN[+r.dataset.role].role=r.value.trim()});
$('#chosen').addEventListener('click',e=>{const mv=e.target.closest('[data-mv]'),rm=e.target.closest('[data-rm]');
  if(mv){const [i,d]=mv.dataset.mv.split(',').map(Number),j2=i+d;if(j2<0||j2>=CHOSEN.length)return;[CHOSEN[i],CHOSEN[j2]]=[CHOSEN[j2],CHOSEN[i]];renderChosen()}
  if(rm){CHOSEN.splice(+rm.dataset.rm,1);renderChosen()}});
$('#groupSel').addEventListener('change',e=>loadGroup(e.target.value));
$('#newBtn').onclick=()=>{$('#groupSel').value='';loadGroup('')};
$('#saveBtn').onclick=async()=>{const name=$('#groupName').value.trim();if(!name)return gmsg('grup adi gir','err');if(!CHOSEN.length)return gmsg('model ekle','err');
  try{await j('/api/model-groups',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({name,models:CHOSEN})});await loadForProfile();$('#groupSel').value=name;loadGroup(name);gmsg('kaydedildi: '+name,'ok')}catch(x){gmsg(x.message,'err')}};
$('#delBtn').onclick=async()=>{const name=$('#groupSel').value;if(!name)return gmsg('grup sec','err');if(!confirm(name+' silinsin mi?'))return;
  try{await j('/api/model-groups/'+encodeURIComponent(name),{method:'DELETE'});await loadForProfile();loadGroup('');gmsg('silindi','ok')}catch(x){gmsg(x.message,'err')}};
$('#activateBtn').onclick=async()=>{const name=$('#groupName').value.trim();if(!name||!DATA.groups[name])return gmsg('once kaydet','err');
  try{const r=await j('/api/model-groups/active',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({name,profile:PF})});
    DATA.active=name;renderGroups();gmsg('['+r.profile+'] aktif: '+name+' -> '+r.primary.provider+'/'+r.primary.model,'ok')}catch(x){gmsg(x.message,'err')}};
$('#fbApproval').addEventListener('change',async e=>{try{await j('/api/model-groups/settings',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({fallback_approval:e.target.checked,profile:PF})});
  gmsg('['+PF+'] kademe izni '+(e.target.checked?'ACIK':'kapali'),'ok')}catch(x){e.target.checked=!e.target.checked}});

renderHist();renderChat();
loadAll().catch(x=>gmsg('yuklenemedi: '+x.message,'err'));
CHAT.filter(m=>m.role==='gorev'&&m.gid&&['calisiyor','onay_bekliyor'].includes((m.rec||{}).durum)).forEach(m=>pollGorev(m));

// ── kalici gorev kontrol cubugu ─────────────────────────────────────────
// Son gorevin durumunu her zaman gorunur tutar: DURDUR / ONAYLA-RED /
// DEVAM butonlari burada; sohbet kartlarindaki butonlarin aynisi.
function _activeTask(){ for(let i=CHAT.length-1;i>=0;i--){ if(CHAT[i].role==='gorev'&&CHAT[i].gid) return CHAT[i]; } return null; }
function renderTaskbar(){
  const tb=document.getElementById('taskbar'); if(!tb) return;
  const m=_activeTask();
  if(!m){ // hic gorev yok: HAZIR durumunda her zaman gorunur
    tb.style.display='block';
    tb.innerHTML='<div style="display:flex;align-items:center;gap:10px;padding:8px 14px;border-bottom:1px solid var(--line);background:var(--panel)"><span style="font-weight:700;font-size:12px;color:var(--dim)"><span class="dot"></span> HAZIR</span><span style="color:var(--faint);font-size:11px">gorev bekleniyor</span><span style="flex:1"></span><span style="color:var(--faint);font-size:11px">kontrol: durdur / onayla / devam burada belirir</span></div>';
    return;
  }
  const r=m.rec||{}, d=r.durum;
  const lbl={calisiyor:'<span class="dot"></span> GOREV CALISIYOR',onay_bekliyor:'<span class="dot"></span> ONAY BEKLIYOR',bitti:'BITTI',hata:'HATA',iptal:'DURDURULDU'}[d]||d||'';
  const gidHtml='<span style="color:var(--faint);font-size:11px">#'+esc(m.gid)+'</span>';
  let btns='';
  if(d==='calisiyor') btns='<button class="bad" data-iptal="'+esc(m.gid)+'">■ DURDUR</button>';
  else if(d==='onay_bekliyor') btns='<button class="good" data-onay="'+esc(m.gid)+'">&#10003; ONAYLA</button><button class="bad" data-red="'+esc(m.gid)+'">&#10007; REDDET</button>';
  else if(d==='iptal') btns='<button class="ghost" data-devam="'+esc(m.gid)+'">&#8635; KALDIGI YERDEN DEVAM</button>';
  const barStyle='display:flex;align-items:center;gap:10px;padding:8px 14px;border-bottom:1px solid var(--line);background:var(--panel)';
  if(d==='calisiyor'||d==='onay_bekliyor'){ tb.style.display='block'; tb.innerHTML='<div style="'+barStyle+'"><span style="font-weight:700;font-size:12px;color:'+(d==='onay_bekliyor'?'var(--warn)':'var(--ok)')+'">'+lbl+'</span>'+gidHtml+'<span style="flex:1"></span>'+btns+'</div>'; }
  else if(d==='iptal'){ tb.style.display='block'; tb.innerHTML='<div style="'+barStyle+'"><span style="font-weight:700;font-size:12px;color:var(--bad)">'+lbl+'</span>'+gidHtml+'<span style="flex:1"></span>'+btns+'</div>'; }
  else { // bitti / hata: sonucu goster ama kapatilabilir durumda
    tb.style.display='block'; tb.innerHTML='<div style="'+barStyle+'"><span style="font-weight:700;font-size:12px;color:'+(d==='hata'?'var(--bad)':'var(--dim)')+'">'+lbl+'</span>'+gidHtml+'<span style="flex:1"></span><span style="color:var(--faint);font-size:11px">yeni gorev verebilirsiniz</span></div>';
  }
}
const _origRenderChat=renderChat;
renderChat=function(){ _origRenderChat(); renderTaskbar(); };
document.addEventListener('DOMContentLoaded',renderTaskbar);
renderTaskbar();
// acilista sunucudaki (tum tarayicilarda ortak) sohbet gecmisini yukle:
// yerel kopyadan daha zenginse ekrana getir ve canli gorevleri yeniden izlemeye al.
(async()=>{try{
  const s=await j('/api/model-groups/sohbet');
  if(!s.ok)return;
  const sc=Array.isArray(s.chat)?s.chat:[], sh=Array.isArray(s.hist)?s.hist:[];
  if(sc.length>CHAT.length||sh.length>HIST.length){
    CHAT=sc;HIST=sh;
    localStorage.setItem('mg_chat',JSON.stringify(CHAT));
    localStorage.setItem('mg_hist',JSON.stringify(HIST));
    renderHist();renderChat();
  }
  CHAT.filter(m=>m.role==='gorev'&&m.gid&&['calisiyor','onay_bekliyor'].includes((m.rec||{}).durum)).forEach(m=>pollGorev(m));
}catch(e){}})();

// ── canli maliyet/token göstergesi ──────────────────────────────────────
async function loadKullanim(){
  try{
    const k=await j('/api/model-groups/kullanim'+q(PF));
    const chip=document.getElementById('costChip');
    if(!chip)return;
    if(!k.ok){chip.textContent='$ ?';chip.title='kullanim okunamadi: '+(k.reason||'?');return}
    const b=k.bugun;
    chip.textContent='$'+(b.tahmini_maliyet_usd||0).toFixed(4)+' · '+((b.giris||0)/1000).toFixed(0)+'k↑/'+((b.cikis||0)/1000).toFixed(1)+'k↓';
    chip.title='BUGÜN: '+b.oturum+' oturum, '+b.giris+' giriş, '+b.cikis+' çıkış, '+b.cache_okuma+' cache-okuma\\n\\nMODELLER:\\n'+(k.modeller||[]).map(m=>' '+m.model+' — '+m.oturum+' oturum, $'+(m.tahmini_maliyet_usd||0).toFixed(4)).join('\\n')+'\\n\\nSON OTURUMLAR:\\n'+(k.son_oturumlar||[]).map(s=>' '+s.baslik+' — $'+(s.tahmini_maliyet_usd||0).toFixed(4)).join('\\n');
  }catch(e){/* sessiz: chip olmadan da sayfa çalışır */}
}
const _costChip=document.getElementById('costChip');
if(_costChip)_costChip.onclick=()=>{alert((_costChip.title||'veri yok').replace(/\\n/g,'\\n'))};
loadKullanim(); setInterval(loadKullanim,15000);
</script></body></html>"""



@router.get("/model-groups", response_class=HTMLResponse)
async def page() -> HTMLResponse:
    # no-store: sayfa tek dosyalik canli panel; eski kodun onbellekten
    # gelmesi ("degisen bir sey yok" sikayeti) yasaklanir.
    return HTMLResponse(_PAGE, headers={"Cache-Control": "no-store, must-revalidate"})


@router.get("/mg", response_class=HTMLResponse)
async def page_short() -> HTMLResponse:
    return HTMLResponse(_PAGE, headers={"Cache-Control": "no-store, must-revalidate"})
