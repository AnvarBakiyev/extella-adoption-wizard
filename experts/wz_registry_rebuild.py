$extens("include.py")
include("import urllib.request", [])

def wz_registry_rebuild(api_token: str = "", api_base: str = "https://api.extella.ai", write_md: str = "1") -> dict:
    """Capability Registry v0 (ТЗ Wizard v2 §8.9, «MD+KV» по решению Анвара): пересборка единого
    реестра возможностей АККАУНТА. Живёт у каждого клиента: исполняется под его токеном на его
    устройстве/хостинге, KV скоупится аккаунтом — изоляция бесплатно.
    Собирает 4 каталога (experts_db, _mkt_automations, _mkt_installed, composer:catalog) + локальные
    ollama-модели устройства (best-effort: на хостинге ollama нет — это не ошибка) → пишет:
    - KV capability:registry (+ :0..N): b64-шарды по 8000 (kv/set строит эмбеддинг значения,
      крупные значения бьются об его лимит токенов);
    - локальный MD для человека: ~/extella_wizard/registry/CAPABILITIES.md (write_md=1);
    - KV registry:last_rebuild (ISO ts) — маркер для суточного пересбора тиком.
    Скоуп-канон: все KV/эксперт-вызовы под X-Agent-Id=agent_extella_default (иначе «тени»)."""
    import json
    import base64
    import os
    from datetime import datetime, timezone
    from pathlib import Path

    def _blank(v):
        return (not v) or str(v).startswith("{{")

    if _blank(api_token):
        try:
            api_token = json.loads((Path.home() / "extella_wizard" / "app" / "config.json")
                                   .read_text(encoding="utf-8")).get("auth_token", "")
        except Exception:
            api_token = ""
    if not api_token:
        return {"status": "error", "message": "нет api_token и bridge-конфига (~/extella_wizard/app/config.json)"}
    if _blank(api_base):
        api_base = "https://api.extella.ai"

    HDRS = {"X-Auth-Token": api_token, "Content-Type": "application/json",
            "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}

    def api(path, payload, timeout=60):
        req = urllib.request.Request(api_base.rstrip("/") + path,
                                     data=json.dumps(payload).encode("utf-8"),
                                     headers=HDRS, method="POST")
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))

    def kv_get_sharded(key):
        try:
            r = api("/api/kv/get", {"key": key})
            v = r.get("value")
            if not v:
                return None
            meta = json.loads(v)
            if isinstance(meta, dict) and meta.get("chunks"):
                buf = ""
                for i in range(int(meta["chunks"])):
                    c = api("/api/kv/get", {"key": key + ":" + str(i)})
                    buf += c.get("value") or ""
                if meta.get("enc") == "b64":
                    buf = base64.b64decode(buf).decode("utf-8")
                return json.loads(buf)
            return meta
        except Exception:
            return None

    APP_PREFIXES = ("uc_", "ta_", "hr_", "hvk_", "fin_", "ci_", "kp_", "cap_", "svc_", "cx_", "cli_")
    caps = []
    problems = []

    def add(cid, ctype, title, desc, surfaces, source, extra=None):
        rec = {"capability_id": str(cid)[:80], "type": ctype, "title": str(title or cid)[:120],
               "description": str(desc or "")[:200], "surfaces": surfaces, "source": source}
        if extra:
            rec.update(extra)
        caps.append(rec)

    # 1. эксперты платформы (аккаунт клиента: его паки уже поставили ему экспертов)
    try:
        r = api("/api/experts_db/list", {}, timeout=120)
        for e in (r.get("results") or r.get("experts") or []):
            name = e.get("name") or e.get("expert_name") or ""
            if not name:
                continue
            if name.startswith("wz_"):
                add(name, "service_expert", name, e.get("description"), ["wizard"], "experts_db")
            elif name.startswith(APP_PREFIXES) or "_run_pipeline" in name:
                add(name, "expert", name, e.get("description"), ["chat", "wizard", "composer"], "experts_db")
    except Exception as ex:
        problems.append("experts_db: " + str(ex)[:80])

    # 2. витрина автоматизаций
    try:
        mkt = kv_get_sharded("_mkt_automations")
        for a in (mkt if isinstance(mkt, list) else (mkt or {}).get("items", []) or []):
            if isinstance(a, dict) and (a.get("name") or a.get("pack_id")):
                add(a.get("pack_id") or a.get("name"), "automation", a.get("name"),
                    a.get("tagline") or a.get("description"),
                    ["wizard", "workspace", "chat"], "_mkt_automations",
                    {"session_id": a.get("sessionId") or a.get("session_id"),
                     "experts": (a.get("experts") or [])[:12]})
    except Exception as ex:
        problems.append("_mkt_automations: " + str(ex)[:80])

    # 3. установленное («Мои»)
    try:
        inst = kv_get_sharded("_mkt_installed")
        for it in (inst if isinstance(inst, list) else (inst or {}).get("items", []) or []):
            if isinstance(it, dict) and it.get("id"):
                add(it["id"], it.get("kind") or "installed", it.get("title") or it["id"],
                    it.get("desc"), ["composer", "wizard"], "_mkt_installed")
    except Exception as ex:
        problems.append("_mkt_installed: " + str(ex)[:80])

    # 4. блоки Композитора
    try:
        cat = kv_get_sharded("composer:catalog")
        for b in (cat if isinstance(cat, list) else (cat or {}).get("blocks", []) or []):
            if isinstance(b, dict) and b.get("id"):
                add(b["id"], "composer_block", b.get("id"), b.get("what"),
                    ["composer", "chat"], "composer:catalog",
                    {"requires_model": bool(b.get("requires_model"))})
    except Exception as ex:
        problems.append("composer:catalog: " + str(ex)[:80])

    # 5. локальные модели ЭТОГО устройства (на хостинге ollama нет — молча пропускаем)
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=4) as f:
            tags = json.loads(f.read().decode("utf-8"))
        for m in tags.get("models", []):
            add(m.get("name"), "model", m.get("name"),
                "локальная модель (ollama, это устройство)", ["composer", "chat"], "ollama:local",
                {"size_gb": round((m.get("size") or 0) / 1e9, 1)})
    except Exception:
        pass

    # dedup (первый источник побеждает)
    seen = set()
    out = []
    for c in caps:
        k = (c["type"], c["capability_id"])
        if k not in seen:
            seen.add(k)
            out.append(c)
    caps = out
    generated_at = datetime.now(timezone.utc).isoformat()[:16] + "Z"

    # KV: b64-шарды по 8000 (паттерн файлового стора)
    doc = {"v": 0, "generated_at": generated_at, "count": len(caps), "capabilities": caps}
    blob = json.dumps(doc, ensure_ascii=False)
    b64 = base64.b64encode(blob.encode("utf-8")).decode("ascii")
    CH = 8000
    chunks = [b64[i:i + CH] for i in range(0, len(b64), CH)]
    try:
        for i, c in enumerate(chunks):
            api("/api/kv/set", {"key": "capability:registry:" + str(i), "value": c,
                                "description": "capability registry shard"})
        api("/api/kv/set", {"key": "capability:registry",
                            "value": json.dumps({"chunks": len(chunks), "enc": "b64",
                                                 "count": len(caps), "generated_at": generated_at}),
                            "description": "unified capability registry v0 (sharded b64)"})
        api("/api/kv/set", {"key": "registry:last_rebuild", "value": generated_at,
                            "description": "registry last full rebuild"})
    except Exception as ex:
        return {"status": "error", "message": "KV write: " + str(ex)[:150],
                "count": len(caps), "problems": problems}

    # локальный MD для человека (на устройстве исполнения; в UI реестр отдаёт /x/registry)
    md_file = None
    if str(write_md) not in ("0", "false", "{{write_md}}"):
        try:
            by = {}
            for c in caps:
                by.setdefault(c["type"], []).append(c)
            lines = ["# Extella — реестр возможностей аккаунта (Capability Registry v0)", "",
                     "_Сгенерировано: " + generated_at + " · всего: " + str(len(caps)) +
                     " · генератор: wz_registry_rebuild_", ""]
            for t in sorted(by):
                lines.append("## " + t + " · " + str(len(by[t])))
                for c in sorted(by[t], key=lambda x: x["capability_id"]):
                    lines.append("- `" + c["capability_id"] + "` — " +
                                 (c["description"] or c["title"]).replace("\n", " ")[:140])
                lines.append("")
            mdir = Path.home() / "extella_wizard" / "registry"
            mdir.mkdir(parents=True, exist_ok=True)
            mp = mdir / "CAPABILITIES.md"
            mp.write_text("\n".join(lines), encoding="utf-8")
            md_file = str(mp)
        except Exception as ex:
            problems.append("md: " + str(ex)[:80])

    return {"status": "success", "count": len(caps), "shards": len(chunks),
            "generated_at": generated_at, "md": md_file,
            "problems": problems or None}
