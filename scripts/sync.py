#!/usr/bin/env python3
"""sync.py — синхронизатор экспертов Визарда: репо (experts/*.py) ↔ платформа Extella.

СЛУЖЕБНЫЙ инструмент РАЗРАБОТЧИКА (не платформенный эксперт, не ставится клиентам).
Запускается с ноутбука разработчика; тянется к платформе по его токену.

Токен берётся из EXTELLA_TOKEN или ~/extella_wizard/app/config.json (auth_token). НЕ печатается.

Команды:
  python scripts/sync.py diff            — показать расхождения (только читает, ничего не меняет)
  python scripts/sync.py pull [name...]  — стянуть эксперты с платформы в репо (перезапишет файлы)
  python scripts/sync.py push [name...] --yes  — залить репо на платформу (МЕНЯЕТ платформу; нужен --yes)

Источник правды по коду — репо. push прошивает репо→платформу; pull возвращает правки, сделанные
вживую на платформе, обратно в репо. diff гоняем перед релизом.
"""
import sys, os, json, hashlib, urllib.request, urllib.error, re
from pathlib import Path

BASE = "https://api.extella.ai"
REPO = Path(__file__).resolve().parent.parent
EXPERTS_DIR = REPO / "experts"

# Эксперты, которым НЕ место в клиентском визард-репо (решения Анвара 07.07.2026).
# diff их не считает дрейфом, pull/push их не трогает.
EXCLUDE = {
    # Внутренний harness подписанного Wizard-релиза; с v5.25 запускается локально и одинаков для
    # всех аккаунтов. Platform `global` account-scoped, поэтому публикация туда только создаёт drift.
    "wz_auto_compose", "wz_build_plan", "wz_generate_blueprint",
    # CLI-витрина живёт в toolbar; визард подхватывает её оттуда — не домен этого репо
    "wz_cli_capability_factory", "wz_cli_installer", "wz_cli_capability_pack",
    # Демо для клиента (оценка колл-центра по эталонному чек-листу) — не продуктовая часть визарда
    "wz_run_demo",
    # Наши VPS-ops / дев-инструменты — не в клиентскую поставку
    "wz_ops_report", "wz_programs_harvest", "wz_expert_janitor",
    # Диагностические зонды
    "wz_embedding_canary", "wz_nohup_probe", "wz_persist_probe_g", "wz_persist_probe_s",
    "wz_vault_selftest", "wz_pick_path",
    # Служебная синхронизация устройства (TODO: классифицировать при Фазе 1)
    "wz_child_from_device", "wz_save_from_device",
}


def token():
    t = os.environ.get("EXTELLA_TOKEN", "").strip()
    if t:
        return t
    cfg = Path.home() / "extella_wizard" / "app" / "config.json"
    if cfg.exists():
        try:
            return json.loads(cfg.read_text(encoding="utf-8")).get("auth_token", "").strip()
        except Exception:
            pass
    sys.exit("Нет токена: задай EXTELLA_TOKEN или ~/extella_wizard/app/config.json (auth_token)")


def api(endpoint, payload, tok, timeout=60):
    req = urllib.request.Request(
        BASE + endpoint, data=json.dumps(payload).encode(),
        headers={"X-Auth-Token": tok, "Content-Type": "application/json",
                 "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"__http__": e.code, "__body__": e.read().decode()[:300]}
    except Exception as e:
        return {"__err__": str(e)[:200]}


_META_HDR = re.compile(r"^#\s*(expert|description|params)\s*:", re.I)


def _strip_meta(code):
    """Срезать ведущую метадата-шапку (# expert:/# description:/# params: + пустые строки).
    Платформа хранит код БЕЗ неё (имя/описание/params — отдельными полями) — иначе ложный дифф."""
    lines = code.split("\n")
    i = 0
    while i < len(lines) and (not lines[i].strip() or _META_HDR.match(lines[i])):
        i += 1
    return "\n".join(lines[i:])


def norm(code):
    """Нормализация перед сравнением: перевод строк, срез метадата-шапки, хвостовых пробелов, краёв."""
    code = (code or "").replace("\r\n", "\n").replace("\r", "\n")
    code = _strip_meta(code)
    return "\n".join(ln.rstrip() for ln in code.split("\n")).strip()


def fp(code):
    return hashlib.sha1(norm(code).encode("utf-8")).hexdigest()[:12]


def expert_name(path):
    """Имя эксперта: из шапки '# expert:' если есть, иначе имя файла без .py."""
    try:
        for ln in path.read_text(encoding="utf-8").splitlines()[:8]:
            m = re.match(r"#\s*expert:\s*(\S+)", ln)
            if m:
                return m.group(1)
    except Exception:
        pass
    return path.stem


def local_experts():
    return {expert_name(p): p for p in sorted(EXPERTS_DIR.glob("*.py"))}


def platform_code(name, tok):
    r = api("/api/expert/get", {"name": name, "global": True}, tok)
    if not isinstance(r, dict):
        return None
    return r.get("expert_code") or (r.get("expert") or {}).get("code")


def platform_list(tok):
    r = api("/api/experts_db/list", {}, tok)
    items = r if isinstance(r, list) else (r.get("experts") or r.get("results") or r.get("data") or [])
    names = set()
    for it in items:
        nm = it.get("name") if isinstance(it, dict) else str(it)
        if nm:
            names.add(nm)
    return names


def cmd_diff(tok, only=None):
    loc = {k: v for k, v in local_experts().items() if k not in EXCLUDE}
    if only:
        loc = {k: v for k, v in loc.items() if k in only}
    plat_names = platform_list(tok)
    same, differ, only_local, only_plat = [], [], [], []
    for name, path in loc.items():
        pcode = platform_code(name, tok)
        if pcode is None:
            only_local.append(name)
            continue
        if fp(path.read_text(encoding="utf-8")) == fp(pcode):
            same.append(name)
        else:
            differ.append(name)
    for name in sorted(plat_names - set(loc)):
        if name.startswith(("wz_", "kp_")) and name not in EXCLUDE:
            only_plat.append(name)
    print("=== sync diff: репо (experts/) ↔ платформа ===")
    print(f"✅ синхронно:        {len(same)}")
    print(f"⚠️  отличаются:       {len(differ)}" + ("  → " + ", ".join(differ) if differ else ""))
    print(f"📤 только в репо:     {len(only_local)}" + ("  → " + ", ".join(only_local) if only_local else ""))
    print(f"📥 только на платформе:{len(only_plat)}" + ("  → " + ", ".join(only_plat) if only_plat else ""))
    print(f"🚫 исключено (toolbar/ops/демо): {len(EXCLUDE)}")
    print(f"\nвсего в репо: {len(loc)} | на платформе (wz_/kp_): {len([n for n in plat_names if n.startswith(('wz_','kp_'))])}")
    if differ or only_local or only_plat:
        print("\nдальше: `pull` — забрать платформенные правки в репо; `push --yes` — прошить репо на платформу.")


def cmd_pull(tok, only=None):
    loc = local_experts()
    names = only or [n for n in loc if n not in EXCLUDE]
    n = 0
    for name in names:
        if name in EXCLUDE:
            print(f"  🚫 {name}: в списке исключений — пропуск"); continue
        r = api("/api/expert/get", {"name": name, "global": True}, tok)
        ex = (r.get("expert") if isinstance(r, dict) else None) or (r if isinstance(r, dict) else {})
        pcode = r.get("expert_code") or ex.get("code")
        if not pcode:
            print(f"  ⏭  {name}: нет на платформе"); continue
        dest = loc.get(name) or (EXPERTS_DIR / (name + ".py"))
        if dest.exists() and fp(dest.read_text(encoding="utf-8")) == fp(pcode):
            continue
        # шапка-метаданные для install.py (diff её срезает — ложного дрейфа не будет)
        desc = (ex.get("description") or name).replace("\n", " ")[:200]
        body = pcode if pcode.endswith("\n") else pcode + "\n"
        header = f"# expert: {name}\n# description: {desc}\n# params:\n\n"
        dest.write_text(header + body, encoding="utf-8")
        print(f"  ⬇️  {name} → {dest.relative_to(REPO)}"); n += 1
    print(f"pull: обновлено файлов {n}. Проверь `git diff`, потом коммить.")


def cmd_push(tok, only=None, yes=False):
    if not yes:
        sys.exit("push МЕНЯЕТ платформу. Повтори с флагом --yes.")
    loc = local_experts()
    names = only or list(loc)
    n = 0
    for name in names:
        path = loc.get(name)
        if not path:
            print(f"  ⏭  {name}: нет в репо"); continue
        code = path.read_text(encoding="utf-8")
        cur = api("/api/expert/get", {"name": name, "global": True}, tok)
        ex = cur.get("expert") if isinstance(cur, dict) else {}
        ex = ex or (cur if isinstance(cur, dict) else {})
        desc = ex.get("description") or name
        kwargs = ex.get("kwargs") or ex.get("params") or {}
        cspl = ex.get("cspl") or "fython"
        res = api("/api/expert/save", {"name": name, "description": desc, "code": code,
                                       "kwargs": kwargs, "cspl": cspl, "global": True}, tok)
        ok = isinstance(res, dict) and res.get("status") == "success"
        print(f"  {'⬆️ ' if ok else '❌'} {name}: {res.get('status') or res}")
        n += ok
    print(f"push: прошито {n}/{len(names)}.")


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ("diff", "pull", "push"):
        sys.exit(__doc__)
    cmd = args[0]
    yes = "--yes" in args
    only = [a for a in args[1:] if not a.startswith("--")] or None
    tok = token()
    if cmd == "diff":
        cmd_diff(tok, only)
    elif cmd == "pull":
        cmd_pull(tok, only)
    elif cmd == "push":
        cmd_push(tok, only, yes)


if __name__ == "__main__":
    main()
