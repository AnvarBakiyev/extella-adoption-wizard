# expert: wz_source_amocrm
# description: Источник данных amoCRM / Kommo (ВХОД процесса, трек B3). Исполняется на устройстве-ХОСТИНГЕ: читает шифротекст секрета sec:<client>:src_amocrm из обще
# params: api_token, client, mode, sid, source_key, api_base, limit

$extens("include.py")
include("import requests", ["extella-pip install requests"])
include("import openpyxl", ["extella-pip install openpyxl"])
include("from cryptography.fernet import Fernet", ["extella-pip install cryptography"])

def wz_source_amocrm(api_token: str = "", client: str = "default", mode: str = "validate",
                     sid: str = "", source_key: str = "", api_base: str = "https://api.extella.ai",
                     limit: int = 0) -> dict:
    """Источник данных amoCRM / Kommo (ВХОД процесса, трек B3). Исполняется на устройстве-ХОСТИНГЕ:
    читает шифротекст секрета sec:<client>:src_amocrm из общего KV, расшифровывает ЛОКАЛЬНЫМ vault.key,
    достаёт {base_url|subdomain(+domain), token(долгосрочный JWT), entity}, ходит в REST /api/v4/<entity>
    (Bearer, пагинация page/limit 250, стоп по пустой странице/204). mode='validate' → GET /api/v4/account;
    mode='pull' → все записи → xlsx → УКЛАДЫВАЕМ в общий стор под source_key чанками base64 (шифр vault.key)
    ТОЧНО как _sync_file_to_store, чтобы резолвер оркестратора материализовал данные БЕЗ правок.
    Токен НИКОГДА не логируется/не возвращается. Зеркало wz_source_bitrix24."""
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

    _ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")

    headers = {"X-Auth-Token": api_token, "Content-Type": "application/json",
               "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}
    cands = [Path("/opt/extella-listener/extella_wizard/vault.key"),
             Path.home() / "extella_wizard/app/vault.key", Path.cwd() / "extella_wizard/vault.key"]
    kp = next((c for c in cands if c.exists()), None)
    if not kp:
        return {"ok": False, "err": "vault.key не найден на устройстве (провижининг ключа не выполнен)"}
    fkey = Fernet(kp.read_bytes())
    key = "sec:" + ns(client) + ":" + ns("src_amocrm")
    try:
        g = requests.post(api_base.rstrip("/") + "/api/kv/get", headers=headers, json={"key": key}, timeout=60).json()
        ct = g.get("value")
    except Exception as e:
        return {"ok": False, "err": "чтение секрета: " + str(e)[:100]}
    if not ct:
        return {"ok": False, "err": "источник amoCRM не подключён (нет секрета)"}
    try:
        env = json.loads(fkey.decrypt(ct.encode()).decode())
        if env.get("c") != client:
            return {"ok": False, "err": "привязка секрета к клиенту не совпала"}   # #8 client-isolation: проверка привязки на ЧТЕНИИ (была только на записи)
        if env.get("k") != "src_amocrm":
            return {"ok": False, "err": "привязка секрета не совпала (ожидался src_amocrm)"}
        creds = json.loads(env.get("v", "{}"))
    except Exception as e:
        return {"ok": False, "err": "расшифровка/формат секрета: " + str(e)[:100]}

    base = str(creds.get("base_url", "")).strip().rstrip("/")
    if not base:
        sub = str(creds.get("subdomain", "")).strip()
        dom = str(creds.get("domain", "amocrm.ru")).strip() or "amocrm.ru"
        if sub:
            base = "https://" + sub + "." + dom
    if not base.startswith("http"):
        return {"ok": False, "err": "нет base_url или subdomain (напр. mycompany.amocrm.ru)"}
    token = str(creds.get("token", "")).strip()
    if not token:
        return {"ok": False, "err": "нет долгосрочного токена (token) в секрете"}
    entity = str(creds.get("entity", "leads")).strip().lower() or "leads"
    if not re.match(r"^[a-z_]+$", entity):
        return {"ok": False, "err": "недопустимая сущность (entity)"}
    ah = {"Authorization": "Bearer " + token}

    def scrub(s):
        s = str(s)
        if token and len(token) >= 6:
            s = s.replace(token, "<token>")
        return s

    def cell(v):
        if v is None:
            return ""
        if isinstance(v, bool) or isinstance(v, (int, float)):
            return v
        if isinstance(v, str):
            return _ILLEGAL.sub("", v)
        return _ILLEGAL.sub("", json.dumps(v, ensure_ascii=False))   # вложенные структуры amoCRM → строка

    try:
        if mode == "validate":
            r = requests.get(base + "/api/v4/account", headers=ah, timeout=30)
            if r.status_code == 200:
                j = {}
                try:
                    j = r.json()
                except Exception:
                    pass
                return {"ok": True, "account": str(j.get("name") or j.get("id") or ""), "host": socket.gethostname(), "source": "amocrm"}
            jt = {}
            try:
                jt = r.json()
            except Exception:
                pass
            return {"ok": False, "err": scrub("amocrm: HTTP %s %s" % (r.status_code, str(jt.get("title") or jt.get("detail") or (r.text or "")[:80])))}

        # mode == pull: /api/v4/<entity>?limit=250&page=N, стоп по пустой странице/204
        allrows = []
        page = 1
        attempt = 0
        safety = 0
        hit_ceiling = True
        while page <= 200 and safety < 1200:   # потолок 200 страниц × 250 = 50000 записей; safety — backstop от зацикливания
            safety += 1
            r = requests.get(base + "/api/v4/" + entity, headers=ah, params={"limit": 250, "page": page}, timeout=60)
            if r.status_code == 429:   # rate-limit amoCRM: пауза и повтор ТОЙ ЖЕ страницы (собранное не теряем)
                if attempt < 5:
                    wait = r.headers.get("Retry-After")
                    try:
                        wait = int(wait)
                    except (TypeError, ValueError):
                        wait = min(2 ** attempt, 30)
                    time.sleep(wait)
                    attempt += 1
                    continue
                return {"ok": False, "err": "amocrm: превышен лимит запросов (429) — попробуйте позже"}
            if r.status_code == 204:   # amoCRM: нет данных на странице
                hit_ceiling = False
                break
            if r.status_code != 200:
                jt = {}
                try:
                    jt = r.json()
                except Exception:
                    pass
                return {"ok": False, "err": scrub("amocrm %s: HTTP %s %s" % (entity, r.status_code, str(jt.get("title") or jt.get("detail") or (r.text or "")[:80])))}
            j = {}
            try:
                j = r.json()
            except Exception:
                pass
            if not isinstance(j, dict):   # 200, но тело не JSON (WAF/прокси-заглушка) → честная ошибка с HTTP-контекстом
                return {"ok": False, "err": scrub("amocrm %s: HTTP %s %s" % (entity, r.status_code, (r.text or "")[:80]))}
            emb = ((j.get("_embedded") or {}).get(entity)) or []
            if not emb:
                hit_ceiling = False
                break
            allrows.extend([e for e in emb if isinstance(e, dict)])
            if limit and limit > 0 and len(allrows) >= int(limit):
                hit_ceiling = False
                break
            page += 1
            attempt = 0   # сброс бюджета ретраев на новую страницу
        if hit_ceiling:   # вышли по потолку страниц, данные ещё были → честный fail (не отдавать усечённое как полное)
            return {"ok": False, "err": "выгрузка amocrm %s превышает потолок 50 000 записей (200 страниц) — задайте limit; полный pull не выполнен" % entity}
        if limit and limit > 0:
            allrows = allrows[:int(limit)]

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
        ws.append([cell(h) for h in header] if header else ["(нет данных)"])
        for row in allrows:
            ws.append([cell(row.get(k)) for k in header])
        buf = io.BytesIO()
        wb.save(buf)
        raw = buf.getvalue()
        if len(raw) > 25 * 1024 * 1024:
            return {"ok": False, "err": "выгрузка слишком большая (>25 МБ) — задайте limit"}

        # === укладка в общий стор ТОЧНО как _sync_file_to_store (чанки+meta, шифр vault.key) ===
        basename = "amocrm_pull.xlsx"
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
                "bytes": len(raw), "host": socket.gethostname(), "source": "amocrm"}
    except Exception as e:
        return {"ok": False, "err": "amocrm: " + scrub(str(e))[:150]}
