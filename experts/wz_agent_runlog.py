def wz_agent_runlog(agent_id="", action="list", ok="", note="", ts="",
                    api_token="", api_base="https://api.extella.ai", limit=50) -> dict:
    """Serverside run history for wizard-built agents. action=append писать прогон, action=list читать.
    Хранит в KV agent_runs:<agent_id> (global). Пишется и из тулбара (ручной запуск), и из планировщика."""
    import json, time, urllib.request
    from pathlib import Path

    def _b(v):
        return (not v) or str(v).startswith("{{")

    if _b(agent_id):
        return {"status": "error", "message": "agent_id обязателен"}
    if _b(api_base):
        api_base = "https://api.extella.ai"
    if _b(api_token):
        cfg = Path.home() / "extella_wizard" / "app" / "config.json"
        try:
            api_token = json.loads(cfg.read_text(encoding="utf-8")).get("auth_token", "") if cfg.exists() else ""
        except Exception:
            api_token = ""
    if not api_token:
        return {"status": "error", "message": "нет api_token"}

    hdr = {"X-Auth-Token": api_token, "Content-Type": "application/json",
           "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}

    def post(path, body, t=30):
        req = urllib.request.Request(api_base.rstrip("/") + path,
                                     data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers=hdr, method="POST")
        with urllib.request.urlopen(req, timeout=t) as r:
            return json.loads(r.read().decode("utf-8"))

    key = "agent_runs:" + str(agent_id)
    _cur = post("/api/kv/get", {"key": key, "global": True}).get("value")
    runs = []
    if _cur:
        try:
            runs = json.loads(_cur).get("runs", [])
        except Exception:
            runs = []

    if str(action) == "append":
        if _b(ok) or str(ok).lower() in ("null", "none", "deferred"):
            _ok = None
        else:
            _ok = str(ok).lower() in ("1", "true", "yes", "ok", "success")
        try:
            _ts = int(ts) if not _b(ts) else int(time.time() * 1000)
        except Exception:
            _ts = int(time.time() * 1000)
        rec = {"ts": _ts, "ok": _ok, "note": (note or "")[:120]}
        runs = ([rec] + runs)[:200]
        post("/api/kv/set", {"key": key, "value": json.dumps({"runs": runs}, ensure_ascii=False),
                             "description": "agent run history " + str(agent_id), "global": True})
        return {"status": "success", "count": len(runs), "run": rec}

    try:
        lim = int(limit)
    except Exception:
        lim = 50
    return {"status": "success", "runs": runs[:lim], "count": len(runs)}