# expert: wz_source_postgres
# description: Источник данных PostgreSQL (ВХОД процесса, трек B3). Исполняется на устройстве-ХОСТИНГЕ: читает шифротекст секрета sec:<client>:src_postgres из общего
# params: api_token, client, mode, sid, source_key, api_base, limit

$extens("include.py")
include("import requests", ["extella-pip install requests"])
include("import pg8000.dbapi", ["extella-pip install pg8000"])
include("import openpyxl", ["extella-pip install openpyxl"])
include("from cryptography.fernet import Fernet", ["extella-pip install cryptography"])

def wz_source_postgres(api_token: str = "", client: str = "default", mode: str = "validate",
                       sid: str = "", source_key: str = "", api_base: str = "https://api.extella.ai",
                       limit: int = 0) -> dict:
    """Источник данных PostgreSQL (ВХОД процесса, трек B3). Исполняется на устройстве-ХОСТИНГЕ:
    читает шифротекст секрета sec:<client>:src_postgres из общего KV, расшифровывает ЛОКАЛЬНЫМ vault.key,
    достаёт {host,port,database,user,password,query|table,limit}, подключается драйвером pg8000
    (чистый Python, БЕЗ компилятора/libpq — критично для чистого хоста), выполняет SELECT (только чтение).
    mode='validate' → connect + SELECT 1 (проверка доступа); mode='pull' → строки → xlsx →
    УКЛАДЫВАЕМ в общий стор под source_key чанками base64 (шифр локальным vault.key) ТОЧНО как
    _sync_file_to_store, чтобы резолвер оркестратора материализовал данные БЕЗ правок.
    Пароль НИКОГДА не логируется/не возвращается. Зеркало шаблона источника."""
    import json
    import socket
    import re
    import hashlib
    import time
    import io
    import datetime as _dt
    import decimal as _dec
    from pathlib import Path

    def ns(s):
        s = str(s)
        return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:40] + "_" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]

    _ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")   # символы, недопустимые в XML/xlsx

    headers = {"X-Auth-Token": api_token, "Content-Type": "application/json",
               "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}
    cands = [Path("/opt/extella-listener/extella_wizard/vault.key"),
             Path.home() / "extella_wizard/app/vault.key", Path.cwd() / "extella_wizard/vault.key"]
    kp = next((c for c in cands if c.exists()), None)
    if not kp:
        return {"ok": False, "err": "vault.key не найден на устройстве (провижининг ключа не выполнен)"}
    fkey = Fernet(kp.read_bytes())
    key = "sec:" + ns(client) + ":" + ns("src_postgres")
    try:
        g = requests.post(api_base.rstrip("/") + "/api/kv/get", headers=headers, json={"key": key}, timeout=60).json()
        ct = g.get("value")
    except Exception as e:
        return {"ok": False, "err": "чтение секрета: " + str(e)[:100]}
    if not ct:
        return {"ok": False, "err": "источник PostgreSQL не подключён (нет секрета)"}
    try:
        env = json.loads(fkey.decrypt(ct.encode()).decode())
        if env.get("k") != "src_postgres":
            return {"ok": False, "err": "привязка секрета не совпала (ожидался src_postgres)"}
        creds = json.loads(env.get("v", "{}"))
    except Exception as e:
        return {"ok": False, "err": "расшифровка/формат секрета: " + str(e)[:100]}

    host = str(creds.get("host", "")).strip()
    port = int(creds.get("port", 5432) or 5432)
    database = str(creds.get("database", "") or creds.get("dbname", "")).strip()
    user = str(creds.get("user", "") or creds.get("username", "")).strip()
    password = str(creds.get("password", ""))
    if not host or not database or not user:
        return {"ok": False, "err": "в секрете нет host/database/user"}

    def scrub(s):
        s = str(s)
        if password and len(password) >= 3:
            s = s.replace(password, "<pw>")   # пароль не должен попасть в текст ошибки
        return s

    def cell(v):
        if v is None:
            return ""
        if isinstance(v, bool) or isinstance(v, (int, float)):
            return v
        if isinstance(v, str):
            return _ILLEGAL.sub("", v)
        if isinstance(v, _dt.datetime):
            return v.replace(tzinfo=None) if v.tzinfo else v          # tz-aware (timestamptz) → naive: openpyxl не примет tz
        if isinstance(v, _dt.time):
            return v.replace(tzinfo=None) if getattr(v, "tzinfo", None) else v
        if isinstance(v, _dt.date):
            return v
        if isinstance(v, _dec.Decimal):
            return float(v)
        if isinstance(v, (bytes, bytearray, memoryview)):
            return bytes(v).hex()
        return _ILLEGAL.sub("", str(v))

    # запрос: сырой SELECT из секрета, либо SELECT * FROM <table>
    q = str(creds.get("query", "")).strip().rstrip(";")
    tbl = str(creds.get("table", "")).strip()
    if not q and tbl:
        if not re.match(r'^[A-Za-z0-9_."]+$', tbl):
            return {"ok": False, "err": "недопустимое имя таблицы"}
        q = "SELECT * FROM " + tbl
    lim = int(limit) if (limit and int(limit) > 0) else int(creds.get("limit", 0) or 0)

    conn = None
    try:
        conn = pg8000.dbapi.connect(user=user, password=password, host=host, port=port,
                                    database=database, timeout=15)
        cur = conn.cursor()
        if mode == "validate":
            cur.execute("SELECT 1")
            cur.fetchone()
            return {"ok": True, "host": socket.gethostname(), "source": "postgres", "db": database}
        # mode == pull
        if not q:
            return {"ok": False, "err": "нет query или table в секрете (нужно для выгрузки)"}
        if not re.match(r"^\s*(select|with)\b", q, re.I):
            return {"ok": False, "err": "разрешён только SELECT/WITH (источник — только чтение)"}
        explicit = lim > 0
        eff_lim = lim if explicit else 50000    # дефолтный потолок строк против безлимитной выгрузки/OOM
        # LIMIT через обёртку-подзапрос: иммунно к хвостовому '--' комментарию и к уже присутствующему LIMIT (сервер режет на своей стороне)
        # +1: обнаружить переполнение, чтобы НЕ отдать усечённое как полное (иначе руководителю уйдёт заниженная сумма)
        runq = "SELECT * FROM (\n" + q + "\n) AS _wz_src LIMIT " + str(int(eff_lim) + 1)
        cur.execute(runq)
        colnames = [d[0] for d in (cur.description or [])]
        fetched = cur.fetchall() or []
        rows = [dict(zip(colnames, r)) for r in fetched]
        if len(rows) > eff_lim:
            if not explicit:
                return {"ok": False, "err": "источник вернул больше " + str(eff_lim) + " строк — данные НЕ выгружены, чтобы не отдать усечённое как полное; задайте limit или сузьте query"}
            rows = rows[:eff_lim]   # явный limit — уважаем как заданный клиентом потолок
    except Exception as e:
        return {"ok": False, "err": "postgres: " + scrub(str(e))[:150]}
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass

    # строки → xlsx (шапка = имена колонок)
    header = colnames or ["(нет данных)"]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "data"
    ws.append([cell(h) for h in header])
    for row in rows:
        ws.append([cell(row.get(k)) for k in colnames])
    buf = io.BytesIO()
    wb.save(buf)
    raw = buf.getvalue()
    if len(raw) > 25 * 1024 * 1024:
        return {"ok": False, "err": "выгрузка слишком большая (>25 МБ) — сузьте query или задайте limit"}

    # === укладка в общий стор ТОЧНО как _sync_file_to_store (чанки+meta, шифр vault.key) ===
    basename = "postgres_pull.xlsx"
    base = source_key or ("file:" + str(sid) + ":" + hashlib.md5(basename.encode("utf-8")).hexdigest()[:12])
    try:
        old_n = int(json.loads((requests.post(api_base.rstrip("/") + "/api/kv/get", headers=headers,
                                               json={"key": base + ":meta"}, timeout=60).json() or {}).get("value") or "{}").get("chunks", 0))
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
                done = True
                break
        if not done:
            return {"ok": False, "err": "не удалось записать чанк источника в стор"}
    m_ok = False
    for _ in range(4):
        if requests.post(api_base.rstrip("/") + "/api/kv/set", headers=headers,
                         json={"key": base + ":meta",
                               "value": json.dumps({"name": basename, "chunks": len(parts), "bytes": len(raw),
                                                    "enc": True, "pulled_at": int(time.time())}),
                               "description": "filemeta " + str(sid)}, timeout=25).json().get("status") == "success":
            m_ok = True
            break
    if not m_ok:
        return {"ok": False, "err": "не удалось записать meta источника в стор"}
    for i in range(len(parts), old_n):
        requests.post(api_base.rstrip("/") + "/api/kv/remove", headers=headers, json={"key": base + ":" + str(i)}, timeout=25)
    return {"ok": True, "rows": len(rows), "source_key": base, "basename": basename,
            "bytes": len(raw), "host": socket.gethostname(), "source": "postgres"}
