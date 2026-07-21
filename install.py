#!/usr/bin/env python3
"""Детерминированный установщик плагина «Визард внедрения».
Запускать ИЗ КАТАЛОГА репозитория:  python3 install.py
Читает токен из ~/extella_wizard/app/config.json (тот же, что у моста), сохраняет все эксперты
(global, cspl=fython), правила и концепты, и создаёт запись плагина в тулбаре. Секреты не печатает."""
import json, os, re, sys, glob, shutil, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.expanduser("~/extella_wizard/app/config.json")
if not os.path.exists(CFG_PATH):
    print("НЕТ config.json по пути", CFG_PATH, "— сначала разверни UI/мост (ui/ + config.json)."); sys.exit(1)
cfg = json.load(open(CFG_PATH, encoding="utf-8"))
TOKEN = cfg.get("auth_token", "")
BASE = cfg.get("api_base", "https://api.extella.ai")
HDR = {"X-Auth-Token": TOKEN, "Content-Type": "application/json",
       "X-Profile-Id": "default", "X-Agent-Id": cfg.get("agent_id", "agent_extella_default")}
if not TOKEN:
    print("В config.json нет auth_token."); sys.exit(1)

# ---- 0. ЛОКАЛЬНЫЙ КАТАЛОГ ВОЗМОЖНОСТЕЙ ----
# План строится по этому файлу до начала стройки. Чистая установка раньше копировала только ui/*,
# поэтому новый Mac падал на шаге «План». Держим canonical-копию и резерв рядом с server.py:
# мост сможет сам восстановить первую из второй.
cat_src = os.path.join(HERE, "catalog", "catalog.json")
cat_dir = os.path.expanduser("~/extella_wizard/catalog")
app_dir = os.path.expanduser("~/extella_wizard/app")
if not os.path.isfile(cat_src):
    print("НЕТ catalog/catalog.json в пакете — установка остановлена."); sys.exit(1)
os.makedirs(cat_dir, exist_ok=True); os.makedirs(app_dir, exist_ok=True)
shutil.copy2(cat_src, os.path.join(cat_dir, "catalog.json"))
shutil.copy2(cat_src, os.path.join(app_dir, "catalog.json"))
print("== Каталог возможностей ==\n  ✅ canonical + резерв в app")

# QA-дельта: без переменной ставим полный пакет как раньше; с ней — только перечисленные
# repo-relative файлы (experts/..., concepts/..., rules/...). UI копирует быстрый shell-обновлятор.
DELTA_FILES = {x.strip().replace("\\", "/") for x in os.environ.get("EXTELLA_DELTA_FILES", "").split(",") if x.strip()}

def selected(path):
    rel = os.path.relpath(path, HERE).replace(os.sep, "/")
    return not DELTA_FILES or rel in DELTA_FILES

def api(path, payload, timeout=120):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"), headers=HDR, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def header(src):
    desc = params = ""
    for line in src.splitlines()[:8]:
        if line.startswith("# description:"): desc = line.split(":", 1)[1].strip()
        elif line.startswith("# params:"): params = line.split(":", 1)[1].strip()
    kwargs = {p.strip(): "" for p in params.split(",") if p.strip()}
    return desc, kwargs

# ---- 1. ЭКСПЕРТЫ (главное — иначе "Expert not found") ----
print("== Эксперты ==")
ok = fail = 0
for f in sorted(glob.glob(os.path.join(HERE, "experts", "*.py"))):
    if not selected(f): continue
    name = os.path.basename(f)[:-3]
    src = open(f, encoding="utf-8").read()
    desc, kwargs = header(src)
    try:
        r = api("/api/expert/save", {"name": name, "description": desc or name, "code": src,
                                     "kwargs": kwargs, "cspl": "fython", "global": True})
        good = (r.get("status") == "success")   # СТРОГО: только явный success (иначе ошибка маскировалась под ✅)
        print(("  ✅ " if good else "  ❌ ") + name + ("" if good else "  " + str(r)[:80]))
        ok += 1 if good else 0; fail += 0 if good else 1
    except Exception as e:
        print("  ❌ " + name + " — " + str(e)[:80]); fail += 1
print(f"  Итог: сохранено {ok}, ошибок {fail}")

# проверка ключевого эксперта
try:
    lst = api("/api/experts_db/list", {})
    names = [e.get("name") or e.get("expert_name") for e in (lst.get("results") or lst.get("experts") or [])]
    print("  wz_session на месте:", "wz_session" in names)
except Exception:
    pass

# ---- 2. ПРАВИЛА (best-effort) ----
print("== Правила ==")
for f in glob.glob(os.path.join(HERE, "rules", "*.md")):
    if not selected(f): continue
    for blk in re.split(r"(?m)^##\s*rule_id:", open(f, encoding="utf-8").read()):
        blk = blk.strip()
        if not blk or blk.startswith("#"): continue
        rid, _, body = blk.partition("\n")
        try:
            api("/api/rules/add", {"rule": body.strip()[:2000], "global": True}); print("  ✅ rule:", rid.strip()[:40])
        except Exception as e:
            print("  ⚠️ rule", rid.strip()[:20], "—", str(e)[:60])

# ---- 3. КОНЦЕПТЫ (best-effort; вектор создаётся при наличии api_key эмбеддинга) ----
print("== Концепты ==")
for f in glob.glob(os.path.join(HERE, "concepts", "*.md")):
    if not selected(f): continue
    if os.path.basename(f).startswith("README"): continue
    body = "\n".join(l for l in open(f, encoding="utf-8").read().splitlines()
                     if not l.startswith("# concept_id:") and not l.startswith("# tag:")).strip()
    if not body: continue
    try:
        api("/api/concept/add", {"text": body, "global": True}); print("  ✅", os.path.basename(f))
    except Exception as e:
        print("  ⚠️", os.path.basename(f), "—", str(e)[:60])

# ---- 4. ЗАПИСЬ ПЛАГИНА В ТУЛБАР (чтобы появился во вкладке «Плагины») ----
print("== Реестр плагина ==")
reg_dir = os.path.expanduser("~/extella-plugins/_registry")
os.makedirs(reg_dir, exist_ok=True)
reg = {
    "id": "extella_adoption_wizard", "name": "Визард внедрения",
    "tagline": "От бизнес-боли к работающему ИИ-процессу",
    "type": "custom", "version": "1.0.0", "mode": "repo_ui", "installed": True,
    "ui": {"type": "local_server", "port": 8765, "rootPath": "~/extella_wizard/app",
           "mainFile": "wizard.html", "openInBrowser": False, "expectsHealth": False},
    "service": {"isApp": True, "port": 8765, "healthPath": "/x/health",
                "launchCmd": "python3 ~/extella_wizard/app/server.py", "ready": True},
    "experts": [os.path.basename(f)[:-3] for f in sorted(glob.glob(os.path.join(HERE, "experts", "*.py")))],
}
open(os.path.join(reg_dir, "extella_adoption_wizard.json"), "w", encoding="utf-8").write(json.dumps(reg, ensure_ascii=False, indent=2))
print("  ✅ ~/extella-plugins/_registry/extella_adoption_wizard.json")

print("\nГотово. Перезагрузи http://127.0.0.1:8765 и создай сессию. Плагин появится во вкладке «Плагины» после перезапуска приложения Extella.")
