# expert: wz_source_gsheets
# description: Источник данных Google Sheets (ВХОД процесса, трек B3). Исполняется на устройстве-ХОСТИНГЕ: читает шифротекст секрета sec:<client>:src_gsheets из обще
# params: api_token, client, mode, sid, source_key, api_base, limit

$extens("include.py")
include("import requests", ["extella-pip install requests"])
include("import openpyxl", ["extella-pip install openpyxl"])
include("from cryptography.fernet import Fernet", ["extella-pip install cryptography"])

def wz_source_gsheets(api_token: str = "", client: str = "default", mode: str = "validate",
                      sid: str = "", source_key: str = "", api_base: str = "https://api.extella.ai",
                      limit: int = 0) -> dict:
    """Источник данных Google Sheets (ВХОД процесса, трек B3). Исполняется на устройстве-ХОСТИНГЕ:
    читает шифротекст секрета sec:<client>:src_gsheets из общего KV, расшифровывает ЛОКАЛЬНЫМ vault.key,
    достаёт {service_account_json, spreadsheet_id, range}, минтит JWT RS256 сервис-аккаунтом
    (cryptography.hazmat, БЕЗ PyJWT/gspread), меняет на access_token, читает диапазон Sheets API v4.
    mode='validate' → читаем диапазон, отдаём колонки/оценку строк (проверка доступа);
    mode='pull' → весь диапазон → xlsx → УКЛАДЫВАЕМ в общий стор под source_key чанками base64
    (шифр локальным vault.key) ТОЧНО как загруженный файл (_sync_file_to_store), чтобы резолвер
    оркестратора материализовал данные БЕЗ единой правки. Зеркало шаблона коннектора вывода.
    Приватный ключ и access_token НИКОГДА не логируются/не возвращаются."""
    import json
    import socket
    import re
    import hashlib
    import time
    import base64
    import io
    from pathlib import Path

    def ns(s):
        s = str(s)
        return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:40] + "_" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]

    def b64u(b):
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    headers = {"X-Auth-Token": api_token, "Content-Type": "application/json",
               "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}
    cands = [Path("/opt/extella-listener/extella_wizard/vault.key"),
             Path.home() / "extella_wizard/app/vault.key", Path.cwd() / "extella_wizard/vault.key"]
    kp = next((c for c in cands if c.exists()), None)
    if not kp:
        return {"ok": False, "err": "vault.key не найден на устройстве (провижининг ключа не выполнен)"}
    fkey = Fernet(kp.read_bytes())
    key = "sec:" + ns(client) + ":" + ns("src_gsheets")
    try:
        g = requests.post(api_base.rstrip("/") + "/api/kv/get", headers=headers, json={"key": key}, timeout=60).json()
        ct = g.get("value")
    except Exception as e:
        return {"ok": False, "err": "чтение секрета: " + str(e)[:100]}
    if not ct:
        return {"ok": False, "err": "источник Google Sheets не подключён (нет секрета)"}
    try:
        env = json.loads(fkey.decrypt(ct.encode()).decode())
        if env.get("k") != "src_gsheets":
            return {"ok": False, "err": "привязка секрета не совпала (ожидался src_gsheets)"}
        creds = json.loads(env.get("v", "{}"))
    except Exception as e:
        return {"ok": False, "err": "расшифровка/формат секрета: " + str(e)[:100]}

    saj = creds.get("service_account_json")
    if isinstance(saj, str):
        try:
            saj = json.loads(saj)
        except Exception:
            return {"ok": False, "err": "service_account_json не парсится как JSON"}
    saj = saj or {}
    sheet_id = str(creds.get("spreadsheet_id", "")).strip()
    rng = str(creds.get("range", "") or "A:Z").strip()

    # === РЕЖИМ ПУБЛИЧНОЙ ССЫЛКИ ("всем, у кого есть ссылка") — CSV-экспорт БЕЗ авторизации ===
    if bool(creds.get("public")) or not saj:
        import csv
        if not sheet_id:
            return {"ok": False, "err": "в секрете нет spreadsheet_id"}
        gid = str(creds.get("gid", "")).strip()
        url = "https://docs.google.com/spreadsheets/d/" + sheet_id + "/export?format=csv" + (("&gid=" + gid) if gid else "")
        try:
            rq = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
        except Exception as e:
            return {"ok": False, "err": "сеть google (public): " + str(e)[:100]}
        ctype = rq.headers.get("content-type", "")
        if rq.status_code != 200 or "text/html" in ctype:
            return {"ok": False, "err": "таблица недоступна по ссылке (HTTP %s) — откройте доступ «всем, у кого есть ссылка»" % rq.status_code}
        vals = list(csv.reader(io.StringIO(rq.text)))
        if mode == "validate":
            cols = vals[0] if vals else []
            return {"ok": True, "sample_cols": [str(c) for c in cols][:30], "rows_estimate": max(0, len(vals) - 1),
                    "host": socket.gethostname(), "source": "gsheets", "access": "public-link"}
        # pull → xlsx → стор
        if limit and int(limit) > 0:
            vals = vals[:int(limit) + 1]
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "data"
        for row in vals:
            ws.append(["" if c is None else c for c in row])
        buf = io.BytesIO(); wb.save(buf); raw = buf.getvalue()
        if len(raw) > 25 * 1024 * 1024:
            return {"ok": False, "err": "выгрузка слишком большая (>25 МБ)"}
        basename = "gsheets_pull.xlsx"
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
        return {"ok": True, "rows": max(0, len(vals) - 1), "source_key": base, "basename": basename,
                "bytes": len(raw), "host": socket.gethostname(), "source": "gsheets", "access": "public-link"}

    client_email = saj.get("client_email", "")
    private_key = saj.get("private_key", "")
    if isinstance(private_key, str) and "\\n" in private_key and "\n" not in private_key:
        private_key = private_key.replace("\\n", "\n")   # перенесённый вручную ключ с литеральными \n
    if not client_email or not private_key or not sheet_id:
        return {"ok": False, "err": "в секрете нет client_email/private_key/spreadsheet_id"}

    # === JWT RS256 сервис-аккаунтом → обмен на access_token (~1ч) ===
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        nowt = int(time.time())
        hdr = b64u(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claim = b64u(json.dumps({"iss": client_email,
                                 "scope": "https://www.googleapis.com/auth/spreadsheets.readonly",
                                 "aud": "https://oauth2.googleapis.com/token",
                                 "iat": nowt, "exp": nowt + 3600}).encode())
        signing_input = (hdr + "." + claim).encode("ascii")
        pk = serialization.load_pem_private_key(private_key.encode("utf-8"), password=None)
        sig = b64u(pk.sign(signing_input, padding.PKCS1v15(), hashes.SHA256()))
        assertion = hdr + "." + claim + "." + sig
    except Exception as e:
        return {"ok": False, "err": "сборка/подпись JWT: " + str(e)[:120]}
    try:
        tr = requests.post("https://oauth2.googleapis.com/token",
                           data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
                           timeout=30)
        tj = {}
        try:
            tj = tr.json()
        except Exception:
            pass
        access = tj.get("access_token")
        if not access:
            # честный отказ (напр. invalid_grant на фейк-подписи) — путь до реального Google OAuth доказан
            return {"ok": False, "err": "google oauth: " + str(tj.get("error_description") or tj.get("error") or ("HTTP " + str(tr.status_code)))[:170]}
    except Exception as e:
        return {"ok": False, "err": "сеть google oauth: " + str(e)[:100]}

    ahead = {"Authorization": "Bearer " + access}

    def values(a1):
        u = "https://sheets.googleapis.com/v4/spreadsheets/" + sheet_id + "/values/" + requests.utils.quote(a1, safe="!:")
        return requests.get(u, headers=ahead, params={"majorDimension": "ROWS", "valueRenderOption": "UNFORMATTED_VALUE"}, timeout=90)

    try:
        r = values(rng)
        if r.status_code != 200:
            jt = {}
            try:
                jt = r.json()
            except Exception:
                pass
            return {"ok": False, "err": "sheets: HTTP %s %s" % (r.status_code, str(jt.get("error", {}).get("message", ""))[:130])}
        vals = (r.json() or {}).get("values", [])
        if mode == "validate":
            cols = vals[0] if vals else []
            return {"ok": True, "sample_cols": [str(c) for c in cols][:30], "rows_estimate": max(0, len(vals) - 1),
                    "host": socket.gethostname(), "source": "gsheets"}
        # mode == pull: строки → xlsx (первая строка = шапка)
        if limit and limit > 0:
            vals = vals[:int(limit) + 1]   # limit строк данных + шапка
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "data"
        for row in vals:
            ws.append(["" if c is None else c for c in row])
        buf = io.BytesIO()
        wb.save(buf)
        raw = buf.getvalue()
        if len(raw) > 25 * 1024 * 1024:   # тот же потолок, что у _sync_file_to_store (защита памяти/KV)
            return {"ok": False, "err": "выгрузка слишком большая (>25 МБ) — сузьте диапазон или задайте limit"}
        # === укладка в общий стор ТОЧНО как _sync_file_to_store (чанки+meta, шифр vault.key) ===
        basename = "gsheets_pull.xlsx"
        base = source_key or ("file:" + str(sid) + ":" + hashlib.md5(basename.encode("utf-8")).hexdigest()[:12])
        try:
            old_n = int(json.loads((requests.post(api_base.rstrip("/") + "/api/kv/get", headers=headers,
                                                   json={"key": base + ":meta"}, timeout=60).json() or {}).get("value") or "{}").get("chunks", 0))
        except Exception:
            old_n = 0
        payload = fkey.encrypt(raw).decode()   # в общий KV — только шифротекст
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
        for i in range(len(parts), old_n):   # удалить хвост старых чанков
            requests.post(api_base.rstrip("/") + "/api/kv/remove", headers=headers, json={"key": base + ":" + str(i)}, timeout=25)
        return {"ok": True, "rows": max(0, len(vals) - 1), "source_key": base, "basename": basename,
                "bytes": len(raw), "host": socket.gethostname(), "source": "gsheets"}
    except Exception as e:
        return {"ok": False, "err": "sheets: " + str(e)[:120]}
