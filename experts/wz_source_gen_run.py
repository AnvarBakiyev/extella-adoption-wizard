# expert: wz_source_gen_run
# description: On-device executor for GENERATIVE data sources. Runs an LLM-generated fetch(secret) function
#   safely on the client's device: AST-guarded (no os/subprocess/eval/open), sandboxed builtins + whitelisted
#   imports, secret injected from the local vault (client-isolated), never hardcoded. mode=verify returns a
#   preview of real rows for the trust gate; mode=pull stores rows into the shared file-store like wz_source_*.
$extens("include.py")
include("import requests", ["extella-pip install requests"])
include("import openpyxl", ["extella-pip install openpyxl"])
include("from cryptography.fernet import Fernet", ["extella-pip install cryptography"])

def wz_source_gen_run(api_token: str = "", client: str = "default", mode: str = "verify",
                      gen_id: str = "", sid: str = "", source_key: str = "",
                      api_base: str = "https://api.extella.ai", limit: int = 0) -> dict:
    import json, socket, ast, re, io, time, hashlib
    from pathlib import Path

    if not api_base or str(api_base).startswith("{{"):
        api_base = "https://api.extella.ai"
    if not gen_id or str(gen_id).startswith("{{"):
        return {"ok": False, "err": "не задан gen_id генеративного источника"}

    def ns(s):
        # КАНОН namespace (как мост _ns и wz_source_*): читаемый префикс + хеш полного значения.
        s = str(s)
        return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:40] + "_" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]

    headers = {"X-Auth-Token": api_token, "Content-Type": "application/json",
               "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}

    def kv_get(key):
        try:
            return (requests.post(api_base.rstrip("/") + "/api/kv/get", headers=headers,
                                  json={"key": key}, timeout=60).json() or {}).get("value")
        except Exception:
            return None

    # 1) сгенерированный код из KV (пишет мост при setup генеративного источника)
    rec_raw = kv_get("gensrc:" + ns(client) + ":" + ns(gen_id))
    if not rec_raw:
        return {"ok": False, "err": "генеративный источник не найден (нет записи кода)"}
    try:
        rec = json.loads(rec_raw)
    except Exception:
        return {"ok": False, "err": "запись генеративного источника битая"}
    code = rec.get("code") or ""
    if not code:
        return {"ok": False, "err": "пустой код генеративного источника"}

    # 2) AST-гард: запрещаем файловую систему/процессы/интроспекцию в СГЕНЕРИРОВАННОМ коде
    BAD_MOD = {"os", "subprocess", "sys", "shutil", "socket", "pathlib", "importlib", "ctypes",
               "pickle", "marshal", "builtins", "threading", "multiprocessing", "signal", "resource", "pty"}
    BAD_CALL = {"eval", "exec", "compile", "__import__", "open", "input", "globals", "locals", "vars", "getattr", "setattr", "delattr"}
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return {"ok": False, "err": "сгенерированный код не парсится: " + str(e)[:80]}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in BAD_MOD:
                    return {"ok": False, "err": "код источника пытается импортировать запрещённое: " + a.name}
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in BAD_MOD:
                return {"ok": False, "err": "код источника пытается импортировать запрещённое: " + str(node.module)}
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in BAD_CALL:
            return {"ok": False, "err": "код источника содержит запрещённый вызов: " + node.func.id}
        elif isinstance(node, ast.Attribute) and str(node.attr).startswith("__"):
            return {"ok": False, "err": "код источника трогает dunder-атрибуты (запрещено)"}

    # 3) секрет (ключ/база) из локального сейфа — client-isolation, конверт {c,k,v}
    cands = [Path("/opt/extella-listener/extella_wizard/vault.key"),
             Path.home() / "extella_wizard/app/vault.key", Path.cwd() / "extella_wizard/vault.key"]
    kp = next((c for c in cands if c.exists()), None)
    secret = {}
    conn_name = "gen_" + ns(gen_id)
    ct = kv_get("sec:" + ns(client) + ":" + ns(conn_name))
    if ct:
        if not kp:
            return {"ok": False, "err": "секрет есть, но vault.key не найден на устройстве"}
        try:
            env = json.loads(Fernet(kp.read_bytes()).decrypt(ct.encode()).decode())
        except Exception as e:
            return {"ok": False, "err": "расшифровка секрета: " + str(e)[:80]}
        if env.get("c") != client:
            return {"ok": False, "err": "привязка секрета к клиенту не совпала"}
        try:
            secret = json.loads(env.get("v", "{}"))
        except Exception:
            secret = {}
    if not isinstance(secret, dict):
        secret = {}

    # 4) песочница: безопасные builtins + белый список импортов (fetch делает ТОЛЬКО HTTP)
    ALLOWED_IMPORTS = {"requests", "json", "re", "datetime", "math", "base64", "time",
                       "hashlib", "hmac", "collections", "itertools", "decimal", "urllib.parse"}
    _real_import = __import__
    def _safe_import(name, *a, **k):
        root = str(name).split(".")[0]
        if name in ALLOWED_IMPORTS or root in {m.split(".")[0] for m in ALLOWED_IMPORTS}:
            return _real_import(name, *a, **k)
        raise ImportError("импорт '%s' запрещён в генеративном источнике" % name)
    safe_builtins = {"__import__": _safe_import,
                     "len": len, "range": range, "str": str, "int": int, "float": float, "dict": dict,
                     "list": list, "tuple": tuple, "set": set, "bool": bool, "bytes": bytes, "print": (lambda *a, **k: None),
                     "sorted": sorted, "sum": sum, "min": min, "max": max, "enumerate": enumerate, "zip": zip,
                     "map": map, "filter": filter, "any": any, "all": all, "abs": abs, "round": round,
                     "isinstance": isinstance, "hasattr": hasattr, "format": format, "repr": repr,
                     "Exception": Exception, "ValueError": ValueError, "KeyError": KeyError, "TypeError": TypeError,
                     "True": True, "False": False, "None": None}
    g = {"__builtins__": safe_builtins}
    import requests as _rq
    g["requests"] = _rq
    g["json"] = json
    try:
        exec(compile(tree, "<gensrc>", "exec"), g)
    except Exception as e:
        return {"ok": False, "err": "инициализация кода источника: " + str(e)[:120]}
    fn = g.get("fetch")
    if not callable(fn):
        return {"ok": False, "err": "в сгенерированном коде нет функции fetch(secret)"}
    try:
        rows = fn(dict(secret))
    except Exception as e:
        return {"ok": False, "err": "исполнение fetch: " + str(e)[:150]}
    if not isinstance(rows, list):
        return {"ok": False, "err": "fetch вернул не список строк (получено: " + type(rows).__name__ + ")"}
    rows = [r for r in rows if isinstance(r, dict)]

    # переполнение (как в остальных источниках)
    explicit = bool(limit and int(limit) > 0)
    cap = int(limit) if explicit else 50000
    if len(rows) > cap:
        if not explicit:
            return {"ok": False, "err": "источник вернул больше " + str(cap) + " строк — задайте limit"}
        rows = rows[:cap]

    if mode == "verify":
        return {"ok": True, "rows": len(rows), "preview": rows[:20],
                "columns": list(rows[0].keys()) if rows else [], "host": socket.gethostname()}

    # mode == pull: строки → xlsx → общий файл-стор (как wz_source_postgres)
    if not kp:
        return {"ok": False, "err": "vault.key не найден — некуда шифровать выгрузку"}
    fkey = Fernet(kp.read_bytes())
    cols = []
    for r in rows:
        for k in r.keys():
            if k not in cols:
                cols.append(k)
    cols = cols or ["(нет данных)"]

    def cell(v):
        if v is None or isinstance(v, (str, int, float, bool)):
            return v
        return json.dumps(v, ensure_ascii=False)

    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "data"
    ws.append([str(c) for c in cols])
    for r in rows:
        ws.append([cell(r.get(k)) for k in cols])
    buf = io.BytesIO(); wb.save(buf); raw = buf.getvalue()
    if len(raw) > 25 * 1024 * 1024:
        return {"ok": False, "err": "выгрузка слишком большая (>25 МБ) — задайте limit"}

    basename = "gen_" + ns(gen_id) + "_pull.xlsx"
    base = source_key or ("file:" + str(sid) + ":" + hashlib.md5(basename.encode("utf-8")).hexdigest()[:12])
    try:
        old_n = int(json.loads(kv_get(base + ":meta") or "{}").get("chunks", 0))
    except Exception:
        old_n = 0
    payload = fkey.encrypt(raw).decode()
    FILE_CHUNK = 8000
    parts = [payload[i:i + FILE_CHUNK] for i in range(0, len(payload), FILE_CHUNK)]
    for i, pt in enumerate(parts):
        done = False
        for _ in range(4):
            if requests.post(api_base.rstrip("/") + "/api/kv/set", headers=headers,
                             json={"key": base + ":" + str(i), "value": pt, "description": "filechunk " + str(sid)},
                             timeout=25).json().get("status") == "success":
                done = True; break
        if not done:
            return {"ok": False, "err": "не удалось записать чанк источника в стор"}
    m_ok = False
    for _ in range(4):
        if requests.post(api_base.rstrip("/") + "/api/kv/set", headers=headers,
                         json={"key": base + ":meta",
                               "value": json.dumps({"name": basename, "chunks": len(parts), "bytes": len(raw),
                                                    "enc": True, "pulled_at": int(time.time())}),
                               "description": "filemeta " + str(sid)}, timeout=25).json().get("status") == "success":
            m_ok = True; break
    if not m_ok:
        return {"ok": False, "err": "не удалось записать meta источника в стор"}
    for i in range(len(parts), old_n):
        requests.post(api_base.rstrip("/") + "/api/kv/remove", headers=headers, json={"key": base + ":" + str(i)}, timeout=25)
    return {"ok": True, "rows": len(rows), "source_key": base, "basename": basename,
            "bytes": len(raw), "host": socket.gethostname(), "source": "generative"}
