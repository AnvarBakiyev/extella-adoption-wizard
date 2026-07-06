# expert: wz_source_1c_file
# description: Источник данных 1С — ФАЙЛОВАЯ база (1Cv8.1CD, БЕЗ платформы 1С), трек B3. Исполняется на устройстве-ХОСТИНГЕ, где лежит файл .1CD (обычно устройство к
# params: api_token, client, mode, sid, source_key, api_base, limit

$extens("include.py")
include("import requests", ["extella-pip install requests"])
include("import onec_dtools", ["extella-pip install onec_dtools"])
include("import openpyxl", ["extella-pip install openpyxl"])
include("from cryptography.fernet import Fernet", ["extella-pip install cryptography"])

def wz_source_1c_file(api_token: str = "", client: str = "default", mode: str = "validate",
                      sid: str = "", source_key: str = "", api_base: str = "https://api.extella.ai",
                      limit: int = 0) -> dict:
    """Источник данных 1С — ФАЙЛОВАЯ база (1Cv8.1CD, БЕЗ платформы 1С), трек B3. Исполняется на
    устройстве-ХОСТИНГЕ, где лежит файл .1CD (обычно устройство клиента). Читает секрет
    sec:<client>:src_1c_file из vault (локальный vault.key): {db_path, table}. Через onec_dtools
    открывает базу. mode='validate' → список таблиц (имена + число полей) для выбора; mode='pull' →
    строки указанной таблицы → xlsx → УКЛАДЫВАЕМ в общий стор под source_key чанками (шифр vault.key)
    ТОЧНО как _sync_file_to_store. Путь к базе не секрет, но в лог не тащим лишнего. Зеркало источника."""
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

    _ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")

    headers = {"X-Auth-Token": api_token, "Content-Type": "application/json",
               "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}
    cands = [Path("/opt/extella-listener/extella_wizard/vault.key"),
             Path.home() / "extella_wizard/app/vault.key", Path.cwd() / "extella_wizard/vault.key"]
    kp = next((c for c in cands if c.exists()), None)
    if not kp:
        return {"ok": False, "err": "vault.key не найден на устройстве (провижининг ключа не выполнен)"}
    fkey = Fernet(kp.read_bytes())
    key = "sec:" + ns(client) + ":" + ns("src_1c_file")
    try:
        g = requests.post(api_base.rstrip("/") + "/api/kv/get", headers=headers, json={"key": key}, timeout=60).json()
        ct = g.get("value")
    except Exception as e:
        return {"ok": False, "err": "чтение секрета: " + str(e)[:100]}
    if not ct:
        return {"ok": False, "err": "источник 1С (файл) не подключён (нет секрета)"}
    try:
        env = json.loads(fkey.decrypt(ct.encode()).decode())
        if env.get("k") != "src_1c_file":
            return {"ok": False, "err": "привязка секрета не совпала (ожидался src_1c_file)"}
        creds = json.loads(env.get("v", "{}"))
    except Exception as e:
        return {"ok": False, "err": "расшифровка/формат секрета: " + str(e)[:100]}

    db_path = str(creds.get("db_path", "")).strip()
    table = str(creds.get("table", "")).strip()
    if not db_path:
        return {"ok": False, "err": "в секрете нет db_path (путь к файлу 1Cv8.1CD)"}
    if not Path(db_path).exists():
        return {"ok": False, "err": "файл базы не найден на устройстве: " + Path(db_path).name}

    def cell(v):
        if v is None:
            return ""
        if isinstance(v, bool) or isinstance(v, (int, float)):
            return v
        if isinstance(v, str):
            return _ILLEGAL.sub("", v)
        if isinstance(v, _dt.datetime):
            return v.replace(tzinfo=None) if v.tzinfo else v
        if isinstance(v, (_dt.date, _dt.time)):
            return v
        if isinstance(v, _dec.Decimal):
            return float(v)
        if isinstance(v, (bytes, bytearray, memoryview)):
            b = bytes(v)
            try:
                return _ILLEGAL.sub("", b.decode("utf-8"))
            except Exception:
                return b.hex()
        return _ILLEGAL.sub("", str(v))

    def field_name(field):
        return field.name if hasattr(field, "name") else str(field)

    try:
        with open(db_path, "rb") as f:
            db = onec_dtools.DatabaseReader(f)
            tnames = list(db.tables.keys())
            if mode == "validate":
                info = []
                for tn in tnames[:300]:
                    try:
                        nf = len(db.tables[tn].fields)
                    except Exception:
                        nf = None
                    info.append({"table": tn, "fields": nf})
                return {"ok": True, "tables": info, "table_count": len(tnames),
                        "host": socket.gethostname(), "source": "1c_file"}
            # mode == pull
            if not table:
                return {"ok": False, "err": "не указана таблица (table) для выгрузки; список — через проверку источника"}
            if table not in db.tables:
                return {"ok": False, "err": "таблицы '%s' нет в базе (проверьте источник для списка)" % table[:40]}
            desc = db.tables[table]
            cols = [field_name(fld) for fld in desc.fields]
            explicit = bool(limit and int(limit) > 0)
            cap = int(limit) if explicit else 50000   # дефолтный потолок строк
            allrows = []
            truncated = False
            for i, row in enumerate(db.tables[table]):
                if i >= cap:
                    truncated = True   # есть ещё строки сверх потолка
                    break
                try:
                    if hasattr(row, "is_empty") and row.is_empty:
                        continue
                except Exception:
                    pass
                rd = []
                for fld in desc.fields:
                    try:
                        val = row[fld]
                        if hasattr(val, "value"):
                            val = val.value
                    except Exception:
                        val = None
                    rd.append(cell(val))
                allrows.append(rd)
            if truncated and not explicit:
                return {"ok": False, "err": "таблица содержит больше " + str(cap) + " строк — данные НЕ выгружены, чтобы не отдать усечённое как полное; задайте limit или сузьте выборку"}
    except Exception as e:
        return {"ok": False, "err": "1c_file: " + str(e)[:150]}

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "data"
    ws.append([cell(c) for c in cols] if cols else ["(нет данных)"])
    for rd in allrows:
        ws.append(rd)
    buf = io.BytesIO()
    wb.save(buf)
    raw = buf.getvalue()
    if len(raw) > 25 * 1024 * 1024:
        return {"ok": False, "err": "выгрузка слишком большая (>25 МБ) — задайте limit"}

    # === укладка в общий стор ТОЧНО как _sync_file_to_store ===
    basename = "1c_file_pull.xlsx"
    base_key = source_key or ("file:" + str(sid) + ":" + hashlib.md5(basename.encode("utf-8")).hexdigest()[:12])
    try:
        old_n = int(json.loads((requests.post(api_base.rstrip("/") + "/api/kv/get", headers=headers,
                                               json={"key": base_key + ":meta"}, timeout=60).json() or {}).get("value") or "{}").get("chunks", 0))
    except Exception:
        old_n = 0
    payload = fkey.encrypt(raw).decode()
    FILE_CHUNK = 8000
    parts = [payload[i:i + FILE_CHUNK] for i in range(0, len(payload), FILE_CHUNK)]
    for i, pt in enumerate(parts):
        done = False
        for _ in range(4):
            if requests.post(api_base.rstrip("/") + "/api/kv/set", headers=headers,
                             json={"key": base_key + ":" + str(i), "value": pt, "description": "filechunk " + str(sid)},
                             timeout=25).json().get("status") == "success":
                done = True
                break
        if not done:
            return {"ok": False, "err": "не удалось записать чанк источника в стор"}
    m_ok = False
    for _ in range(4):
        if requests.post(api_base.rstrip("/") + "/api/kv/set", headers=headers,
                         json={"key": base_key + ":meta",
                               "value": json.dumps({"name": basename, "chunks": len(parts), "bytes": len(raw),
                                                    "enc": True, "pulled_at": int(time.time())}),
                               "description": "filemeta " + str(sid)}, timeout=25).json().get("status") == "success":
            m_ok = True
            break
    if not m_ok:
        return {"ok": False, "err": "не удалось записать meta источника в стор"}
    for i in range(len(parts), old_n):
        requests.post(api_base.rstrip("/") + "/api/kv/remove", headers=headers, json={"key": base_key + ":" + str(i)}, timeout=25)
    return {"ok": True, "rows": len(allrows), "source_key": base_key, "basename": basename,
            "bytes": len(raw), "host": socket.gethostname(), "source": "1c_file"}
