#!/usr/bin/env python3
"""Capability Registry v0 (ТЗ Wizard v2 §8.9, версия «малой кровью» по решению Анвара 16.07.2026).

Единый реестр возможностей для четырёх поверхностей (Chat / Wizard / Composer / Workspaces):
- собирает живые каталоги, которые сегодня существуют ПОРОЗНЬ:
  эксперты платформы (experts_db), витрина автоматизаций (_mkt_automations),
  установленное (_mkt_installed), блоки Композитора (composer:catalog),
  локальные модели (ollama), kp-паки знаний;
- пишет ДВА представления одного реестра:
  1) docs/CAPABILITIES.md — человекочитаемый файл (git: видят команда, Codex, любой агент);
  2) KV `capability:registry` (global, под agent_extella_default) — машинное зеркало,
     которое читают мост (/x/registry) и любая поверхность через kv/get.

Это НЕ полный Capability Plane из ТЗ (нет манифестов/версий/lockfile — они требуют
платформенного бэкенда, см. docs/WIZARD_V2_MAPPING.md §6). Это его честная «версия 0»:
одна инвентаризация вместо четырёх силосов + правило скоупа (всё под default-агентом).

Запуск: python3 scripts/capability_registry.py [--dry]
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
API = "https://api.extella.ai"
KV_KEY = "capability:registry"
MD_PATH = REPO / "docs" / "CAPABILITIES.md"

# префиксы прикладных экспертов; служебные (wz_) отмечаем отдельным типом
APP_PREFIXES = ("uc_", "ta_", "hr_", "hvk_", "fin_", "ci_", "kp_", "cap_", "svc_", "cx_", "cli_")


def token():
    cfg = Path.home() / "extella_wizard" / "app" / "config.json"
    if cfg.exists():
        t = json.loads(cfg.read_text(encoding="utf-8")).get("auth_token", "")
        if t:
            return t
    src = (Path.home() / ".claude" / "extella_mcp_server.py")
    if src.exists():
        m = re.search(r"AUTH_TOKEN\s*=\s*[\"']([^\"']+)", src.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    raise SystemExit("нет токена (config.json / extella_mcp_server.py)")


def api(path, payload, tok, timeout=60):
    req = urllib.request.Request(API + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"X-Auth-Token": tok, "Content-Type": "application/json",
                                          "X-Profile-Id": "default",
                                          "X-Agent-Id": "agent_extella_default"},  # канон: общий скоуп, не тени
                                 method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))


def kv_get_sharded(key, tok):
    """Читает KV-значение; поддерживает шардирование key:0..N по meta (наш паттерн витрины/файлов).
    enc=b64: чанки — base64 от utf-8 JSON (сырые JSON-срезы кириллицы платформа отбивает 500)."""
    r = api("/api/kv/get", {"key": key}, tok)
    v = r.get("value")
    if not v:
        return None
    try:
        meta = json.loads(v)
        if isinstance(meta, dict) and meta.get("chunks"):
            buf = ""
            for i in range(int(meta["chunks"])):
                c = api("/api/kv/get", {"key": key + ":" + str(i)}, tok)
                buf += c.get("value") or ""
            if meta.get("enc") == "b64":
                import base64
                buf = base64.b64decode(buf).decode("utf-8")
            return json.loads(buf)
        return meta
    except Exception:
        return None


def collect(tok):
    caps = []

    def add(cid, ctype, title, desc, surfaces, source, extra=None):
        rec = {"capability_id": str(cid)[:80], "type": ctype, "title": str(title or cid)[:120],
               "description": str(desc or "")[:200], "surfaces": surfaces, "source": source}
        if extra:
            rec.update(extra)
        caps.append(rec)

    # 1. Эксперты платформы (глобальный скоуп)
    try:
        r = api("/api/experts_db/list", {}, tok, timeout=120)
        experts = r.get("results") or r.get("experts") or r.get("result") or []
        for e in experts:
            name = e.get("name") or e.get("expert_name") or ""
            if not name:
                continue
            if name.startswith("wz_"):
                add(name, "service_expert", name, e.get("description"),
                    ["wizard"], "experts_db")
            elif name.startswith(APP_PREFIXES) or "_run_pipeline" in name:
                add(name, "expert", name, e.get("description"),
                    ["chat", "wizard", "composer"], "experts_db")
    except Exception as ex:
        print("  ! experts_db:", str(ex)[:100])

    # 2. Витрина автоматизаций (карточки процессов/паков)
    try:
        mkt = kv_get_sharded("_mkt_automations", tok)
        for a in (mkt if isinstance(mkt, list) else (mkt or {}).get("items", []) or []):
            if isinstance(a, dict) and (a.get("name") or a.get("pack_id")):
                add(a.get("pack_id") or a.get("name"), "automation", a.get("name"),
                    a.get("tagline") or a.get("description"),
                    ["wizard", "workspace", "chat"], "_mkt_automations",
                    {"session_id": a.get("sessionId") or a.get("session_id"),
                     "experts": (a.get("experts") or [])[:12]})
    except Exception as ex:
        print("  ! _mkt_automations:", str(ex)[:100])

    # 3. Установленные способности («Мои»: модели/MCP/CLI/навыки)
    try:
        inst = kv_get_sharded("_mkt_installed", tok)
        for it in (inst if isinstance(inst, list) else (inst or {}).get("items", []) or []):
            if isinstance(it, dict) and it.get("id"):
                add(it["id"], it.get("kind") or "installed", it.get("title") or it["id"],
                    it.get("desc"), ["composer", "wizard"], "_mkt_installed")
    except Exception as ex:
        print("  ! _mkt_installed:", str(ex)[:100])

    # 4. Блоки Композитора (вет-проверенный whitelist)
    try:
        cat = kv_get_sharded("composer:catalog", tok)
        for b in (cat if isinstance(cat, list) else (cat or {}).get("blocks", []) or []):
            if isinstance(b, dict) and b.get("id"):
                add(b["id"], "composer_block", b.get("id"), b.get("what"),
                    ["composer", "chat"], "composer:catalog",
                    {"requires_model": bool(b.get("requires_model"))})
    except Exception as ex:
        print("  ! composer:catalog:", str(ex)[:100])

    # 5. Локальные модели устройства (ollama)
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5) as f:
            tags = json.loads(f.read().decode("utf-8"))
        for m in tags.get("models", []):
            add(m.get("name"), "model", m.get("name"),
                "локальная модель (ollama, это устройство)",
                ["composer", "chat"], "ollama:local",
                {"size_gb": round((m.get("size") or 0) / 1e9, 1)})
    except Exception:
        pass  # ollama может быть не поднят — не ошибка

    # dedup по (type, capability_id) — первый источник побеждает
    seen, out = set(), []
    for c in caps:
        k = (c["type"], c["capability_id"])
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def write_md(caps, generated_at):
    order = ["automation", "expert", "composer_block", "model", "knowledge_pack",
             "cli", "mcp", "skill", "installed", "service_expert"]
    titles = {"automation": "Автоматизации (витрина — карточки процессов и паков)",
              "expert": "Прикладные эксперты (исполняемые блоки платформы)",
              "composer_block": "Блоки Композитора (вет-проверенный whitelist)",
              "model": "Локальные модели (это устройство)",
              "cli": "CLI-инструменты", "mcp": "MCP-серверы", "skill": "Навыки",
              "installed": "Установленное («Мои»)",
              "service_expert": "Служебные эксперты визарда (wz_*)"}
    by = {}
    for c in caps:
        by.setdefault(c["type"], []).append(c)
    lines = [
        "# Extella — единый реестр возможностей (Capability Registry v0)",
        "",
        "**Что это:** одна инвентаризация возможностей платформы для всех четырёх поверхностей",
        "(Chat, Wizard, Composer, Workspaces) — вместо четырёх раздельных каталогов (ТЗ v2 §8.9).",
        "Файл генерируется скриптом `scripts/capability_registry.py`; машинное зеркало — KV",
        "`" + KV_KEY + "` (global, скоуп agent_extella_default; мост отдаёт через `/x/registry`).",
        "**Не редактировать руками** — перегенерируйте скриптом.",
        "",
        "_Сгенерировано: " + generated_at + " · всего возможностей: " + str(len(caps)) + "_",
        "",
    ]
    for t in order:
        items = by.pop(t, [])
        if not items:
            continue
        lines += ["## " + titles.get(t, t) + " · " + str(len(items)), "",
                  "| id | название | описание | поверхности | источник |", "|---|---|---|---|---|"]
        for c in sorted(items, key=lambda x: x["capability_id"]):
            lines.append("| `%s` | %s | %s | %s | %s |" % (
                c["capability_id"], c["title"].replace("|", "/"),
                (c["description"] or "").replace("|", "/").replace("\n", " ")[:120],
                " ".join(c["surfaces"]), c["source"]))
        lines.append("")
    for t, items in by.items():   # незнакомые типы — не терять молча
        lines += ["## " + t + " · " + str(len(items)), ""]
        lines += ["- `%s` — %s" % (c["capability_id"], c["title"]) for c in items] + [""]
    MD_PATH.write_text("\n".join(lines), encoding="utf-8")


def write_kv(caps, generated_at, tok):
    doc = {"v": 0, "generated_at": generated_at, "count": len(caps), "capabilities": caps}
    blob = json.dumps(doc, ensure_ascii=False)
    # чанки — base64 по 8000 (паттерн файлового стора моста, FILE_CHUNK=8000): kv/set строит
    # эмбеддинг значения, длинные/плотные значения бьются об лимит токенов эмбеддера (500:
    # «Embedding error: 400 api.openai.com/v1/embeddings») — поэтому только мелкие ASCII-чанки.
    import base64
    b64 = base64.b64encode(blob.encode("utf-8")).decode("ascii")
    CH = 8000
    chunks = [b64[i:i + CH] for i in range(0, len(b64), CH)]
    for i, c in enumerate(chunks):
        api("/api/kv/set", {"key": KV_KEY + ":" + str(i), "value": c,
                            "description": "capability registry shard"}, tok)
    api("/api/kv/set", {"key": KV_KEY, "value": json.dumps({"chunks": len(chunks), "enc": "b64",
                                                            "count": len(caps), "generated_at": generated_at}),
                        "description": "unified capability registry v0 (sharded b64)"}, tok)
    return len(chunks)


def main():
    dry = "--dry" in sys.argv
    tok = token()
    print("Собираю каталоги…")
    caps = collect(tok)
    generated_at = datetime.now(timezone.utc).isoformat()[:16] + "Z"
    by_type = {}
    for c in caps:
        by_type[c["type"]] = by_type.get(c["type"], 0) + 1
    print("Итого:", len(caps), "·", ", ".join("%s=%d" % kv for kv in sorted(by_type.items())))
    write_md(caps, generated_at)
    print("MD:", MD_PATH)
    if not dry:
        n = write_kv(caps, generated_at, tok)
        print("KV:", KV_KEY, "(%d части)" % n)


if __name__ == "__main__":
    main()
