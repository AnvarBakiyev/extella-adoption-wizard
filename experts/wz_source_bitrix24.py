# expert: wz_source_bitrix24
# description: Источник данных Bitrix24 (ВХОД процесса, трек B3). Исполняется на устройстве-ХОСТИНГЕ: читает шифротекст секрета sec:<client>:src_bitrix24 из общего K
# params: api_token, client, mode, sid, source_key, api_base, limit

$extens("include.py")
include("import requests", ["extella-pip install requests"])
include("import openpyxl", ["extella-pip install openpyxl"])
include("from cryptography.fernet import Fernet", ["extella-pip install cryptography"])

def wz_source_bitrix24(api_token: str = "", client: str = "default", mode: str = "validate",
                       sid: str = "", source_key: str = "", api_base: str = "https://api.extella.ai",
                       limit: int = 0) -> dict:
    """Источник данных Bitrix24 (ВХОД процесса, трек B3). Исполняется на устройстве-ХОСТИНГЕ:
    читает шифротекст секрета sec:<client>:src_bitrix24 из общего KV, расшифровывает ЛОКАЛЬНЫМ vault.key,
    достаёт {webhook_url, entity, select, filter}, ходит в incoming webhook Bitrix24 (одна строка URL,
    без OAuth/протухания), тянет crm.<entity>.list с пагинацией (страница 50, start=next).
    mode='validate' → лёгкий вызов profile (проверка доступа);
    mode='pull' → все записи → xlsx → УКЛАДЫВАЕМ в общий стор под source_key чанками base64
    (шифр локальным vault.key) ТОЧНО как _sync_file_to_store, чтобы резолвер оркестратора
    материализовал данные БЕЗ правок. Код вебхука (секрет) НИКОГДА не логируется/не возвращается.
    Зеркало wz_source_gsheets / шаблона коннектора вывода."""
    import json
    import socket
    import re
    import hashlib
    import time
    import io
    from pathlib import Path

    def ns(s):
        s = str(s)
        return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:40] + "_" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]

    headers = {"X-Auth-Token": api_token, "Content-Type": "application/json",
               "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}
    cands = [Path("/opt/extella-listener/extella_wizard/vault.key"),
             Path.home() / "extella_wizard/app/vault.key", Path.cwd() / "extella_wizard/vault.key"]
    kp = next((c for c in cands if c.exists()), None)
    if not kp:
        return {"ok": False, "err": "vault.key не найден на устройстве (провижининг ключа не выполнен)"}
    fkey = Fernet(kp.read_bytes())
    key = "sec:" + ns(client) + ":" + ns("src_bitrix24")
    try:
        g = requests.post(api_base.rstrip("/") + "/api/kv/get", headers=headers, json={"key": key}, timeout=60).json()
        ct = g.get("value")
    except Exception as e:
        return {"ok": False, "err": "чтение секрета: " + str(e)[:100]}
    if not ct:
        return {"ok": False, "err": "источник Bitrix24 не подключён (нет секрета)"}
    try:
        env = json.loads(fkey.decrypt(ct.encode()).decode())
        if env.get("k") != "src_bitrix24":
            return {"ok": False, "err": "привязка секрета не совпала (ожидался src_bitrix24)"}
        creds = json.loads(env.get("v", "{}"))
    except Exception as e:
        return {"ok": False, "err": "расшифровка/формат секрета: " + str(e)[:100]}

    wh = str(creds.get("webhook_url", "")).strip().rstrip("/")
    if not wh.startswith("http") or "/rest/" not in wh:
        return {"ok": False, "err": "нет корректного webhook_url (вида https://<домен>.bitrix24.kz/rest/<id>/<код>)"}
    entity = str(creds.get("entity", "crm.deal")).strip() or "crm.deal"
    if not re.match(r"^[a-z0-9_.]+$", entity):
        return {"ok": False, "err": "недопустимая сущность (entity)"}
    select = creds.get("select") or ["*"]
    filt = creds.get("filter") or {}

    _ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")   # символы, недопустимые в XML/xlsx (валят openpyxl)

    def scrub(s):
        # вырезать секрет (URL вебхука и его код) из текста ошибки — requests кладёт полный URL в str(e)
        s = str(s)
        if wh:
            s = s.replace(wh, "<webhook>")
            code = wh.rstrip("/").rsplit("/", 1)[-1]
            if code and len(code) >= 6:
                s = s.replace(code, "<code>")
        return s

    def call(method, body):
        return requests.post(wh + "/" + method, json=body, timeout=60)

    def cell(v):
        if v is None:
            return ""
        if isinstance(v, bool) or isinstance(v, (int, float)):
            return v
        if isinstance(v, str):
            return _ILLEGAL.sub("", v)
        return _ILLEGAL.sub("", json.dumps(v, ensure_ascii=False))   # мульти-поля Bitrix (списки/словари) → строка

    try:
        if mode == "validate":
            r = call("profile", {})
            j = {}
            try:
                j = r.json()
            except Exception:
                pass
            if r.status_code == 200 and j.get("result"):
                res = j["result"]
                who = res.get("NAME") or res.get("LOGIN") or res.get("ID")
                return {"ok": True, "account": str(who), "host": socket.gethostname(), "source": "bitrix24"}
            return {"ok": False, "err": scrub("bitrix24: HTTP %s %s" % (r.status_code, str(j.get("error_description") or j.get("error") or (r.text or "")[:80])))}

        # mode == pull: тянем все записи crm.<entity>.list циклом по next (страница 50)
        method = entity + ".list"
        allrows = []
        start = 0
        nxt = None
        for _page in range(200):   # потолок 200 страниц (~10000 записей) — не зациклить (канон)
            body = {"select": select, "order": {"ID": "ASC"}, "start": start}
            if filt:
                body["filter"] = filt
            r = call(method, body)
            j = {}
            try:
                j = r.json()
            except Exception:
                pass
            if r.status_code != 200 or "result" not in j:
                return {"ok": False, "err": scrub("bitrix24 %s: HTTP %s %s" % (method, r.status_code, str(j.get("error_description") or j.get("error") or (r.text or "")[:80])))}
            res = j.get("result")
            if isinstance(res, list):
                allrows.extend(res)
            elif isinstance(res, dict):   # некоторые сущности отдают {items:[...]} / dict
                allrows.extend(res.get("items") or [res])
            nxt = j.get("next")
            if nxt and (not limit or len(allrows) < limit):
                start = nxt
            else:
                break
        # вышли по потолку 200 страниц, а next ещё живой → честный fail (не отдавать усечённое как полное)
        if nxt and (not limit or len(allrows) < limit):
            return {"ok": False, "err": "выгрузка crm.%s превышает потолок 10 000 записей (200 страниц) — задайте filter или limit; полный pull не выполнен (во избежание тихой потери данных)" % entity}
        if limit and limit > 0:
            allrows = allrows[:int(limit)]
        allrows = [r for r in allrows if isinstance(r, dict)]

        # строки-словари → xlsx: шапка = объединение ключей с сохранением порядка
        header = []
        seen = set()
        for row in allrows:
            for k in row.keys():
                if k not in seen:
                    seen.add(k)
                    header.append(k)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "data"
        ws.append(header or ["(нет данных)"])
        for row in allrows:
            ws.append([cell(row.get(k)) for k in header])
        buf = io.BytesIO()
        wb.save(buf)
        raw = buf.getvalue()
        if len(raw) > 25 * 1024 * 1024:
            return {"ok": False, "err": "выгрузка слишком большая (>25 МБ) — сузьте filter или задайте limit"}

        # === укладка в общий стор ТОЧНО как _sync_file_to_store (чанки+meta, шифр vault.key) ===
        basename = "bitrix24_pull.xlsx"
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
        return {"ok": True, "rows": len(allrows), "source_key": base, "basename": basename,
                "bytes": len(raw), "host": socket.gethostname(), "source": "bitrix24"}
    except Exception as e:
        return {"ok": False, "err": "bitrix24: " + scrub(str(e))[:150]}   # scrub: убрать код вебхука из текста ошибки
