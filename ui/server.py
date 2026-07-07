#!/usr/bin/env python3
"""Extella Adoption Wizard — local bridge server.

Runs on the client device (next to the Listener). Serves the wizard UI and
bridges it to the Extella cloud API so the browser never sees tokens/keys
and CORS never applies (same origin).

Reads  = local session/blueprint files (instant).
Writes = run_expert calls through the platform (traced runs).
Chat   = agent/run proxy to the Adoption Wizard agent.

Config: config.json next to this file:
  {"port": 8765, "auth_token": "...", "agent_id": "agent_...",
   "llm_api_key": "...", "llm_base_url": "https://api.openai.com/v1",
   "llm_model": "gpt-4o"}
"""
import ast
import json
import re
import threading
import time
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG = json.loads((APP_DIR / "config.json").read_text(encoding="utf-8"))
SESS_DIR = Path.home() / "extella_wizard" / "sessions"
RUNS_DIR = Path.home() / "extella_wizard" / "runs"
_CAT_DIR = Path.home() / "extella_wizard" / "catalog"
CATALOG_PATH = (_CAT_DIR / "catalog.json") if (_CAT_DIR / "catalog.json").exists() else (_CAT_DIR / "catalog_v1.json")
# Industry libraries seeded by the synthetic-seed process (matrix "processes x industries").
# library/manifest.json lists industries; each available one has checklist/taxonomy/regulatory.
LIB_DIR = Path.home() / "extella_wizard" / "library"


def _lib_manifest():
    """Read the industry-library manifest; return {} if none seeded yet."""
    mf = LIB_DIR / "manifest.json"
    if not mf.exists():
        return {}
    try:
        return json.loads(mf.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _lib_entry(industry):
    """Manifest record for one industry id, or None."""
    for it in (_lib_manifest().get("industries") or []):
        if it.get("id") == industry:
            return it
    return None


def _lib_checklist_path(industry):
    """Absolute path to the seeded checklist for an industry, or '' if unavailable.
    Used to run the demo on the industry checklist instead of the generic default."""
    ent = _lib_entry(industry)
    if not ent or not ent.get("available") or not ent.get("checklist"):
        return ""
    p = LIB_DIR / ent["checklist"]
    return str(p) if p.exists() else ""

# pipeline sub-stages inferred from artifact files appearing in the run dir
PIPELINE_ARTIFACTS = [
    ("parsed.pkl", "Разбор выгрузки"),
    ("anonymized.pkl", "Псевдонимизация ПДн"),
    ("llm_input.jsonl", "Подготовка к ИИ-оценке"),
    ("operator_load.json", "Нагрузка операторов"),
    ("concurrent.json", "Пики одновременных чатов"),
    ("daily.json", "Дневная статистика"),
    ("eval_sample.json", "ИИ-оценка по чек-листу"),
]
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
BASE = "https://api.extella.ai"
FILE_CHUNK = 8000            # размер чанка base64 в KV (крупные значения KV нестабильны → чанкуем)
HOST_TARGET = "85800354-f7b7-449f-b526-9357cd91f780"  # managed-хостинг VPS (PS.kz) — куда пиннить процессы 24/7
SCHED_INDEX_KEY = "sched:__index__"  # индекс активных расписаний (список sid) — тик читает его вместо прохода по всему KV
INBOUND_INDEX_KEY = "inbound:__index__"  # индекс процессов с включённым приёмом входящих (B2) — тик читает его
BRIDGE_VERSION = "3.47"       # версия моста; /x/health отдаёт её, single-instance по ней решает «свежий/старый»
_MON_CACHE = {"at": None, "resp": None}   # короткий TTL-кэш /x/monitor (частые обновления панели — мгновенно)
CLIENT_ID = str(CONFIG.get("client_id", "default"))  # арендатор (клиент) — namespace секретов/данных для мультитенантности
REL_PREFIX = "rel:bridge"    # канал релизов моста в KV (наш код моста, не секрет; для авто-обновления устройств)
MAX_UPDATE_ATTEMPTS = 3      # после стольких неуспешных загрузок обновления — откат на предыдущую версию
# ПУБЛИЧНЫЙ ключ подписи релизов (Ed25519). Приватный — ТОЛЬКО у нас (~/.extella_release_key), НЕ в KV/пакете.
# Мост применяет обновление ТОЛЬКО с валидной подписью → канал в общем KV не даёт RCE на парк.
RELEASE_PUBKEY_HEX = "ed55efce4bf8ef6559c04cb83fccdd66aae8bfbc0176a27d495982d79bbddac8"
_OWNER = False               # выставляется в __main__: True если процесс под launchd (единственный, кто самоперезапускается)
_UPDATE_LOCK = threading.Lock()  # взаимное исключение apply-обновления
_TG_LOGIN = {}               # состояние интерактивного входа Telegram (login_id → {phone,hash,ss,...}), эфемерно
_START_TS = time.time()      # момент старта процесса (для uptime в /x/health)
HEADERS = {"X-Auth-Token": CONFIG["auth_token"], "Content-Type": "application/json",
           "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}
def qwen_agent():
    """Qwen-агент для keyless-LLM: явный override (llm_agent_id) → СОБСТВЕННЫЙ Qwen-Визард клиента (config.agent_id).
    Никогда Claude (agent_extella_default) и никогда чужой агент (иначе 'Agent does not belong to this user')."""
    return CONFIG.get("llm_agent_id") or CONFIG.get("agent_id", "")


def api(endpoint, payload, timeout=180):
    req = urllib.request.Request(BASE + endpoint, data=json.dumps(payload).encode(),
                                 headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"status": "error", "http_code": e.code, "message": e.read().decode()[:500]}
    except Exception as e:
        return {"status": "error", "message": str(e)[:300]}


def _file_key(sid, basename):
    """Детерминированный ASCII-ключ файла в общем сторе (KV). md5(basename), т.к.
    KV нестабилен на юникод-ключах с крупными значениями."""
    import hashlib
    return "file:" + str(sid) + ":" + hashlib.md5(str(basename).encode("utf-8")).hexdigest()[:12]


def _sync_file_to_store(sid, path):
    """Синк файла на хостинг: кладёт байты файла в общий стор (KV) чанками base64,
    чтобы процесс по расписанию на другом устройстве материализовал его резолвером.
    Короткий per-call таймаут + общий дедлайн, чтобы не подвесить надолго при деградации KV.
    Возвращает base-ключ или None. Обычно вызывается в фоновом потоке."""
    import base64 as _b64
    try:
        p = Path(path)
        raw = p.read_bytes()
        if len(raw) > 25 * 1024 * 1024:          # единый лимит размера (не только на /x/upload)
            return None
        base = _file_key(sid, p.name)
        # старое число чанков — чтобы удалить хвост (иначе сырые plaintext-чанки прошлого синка утекут в KV)
        try:
            old_n = int(json.loads((api("/api/kv/get", {"key": base + ":meta"}) or {}).get("value") or "{}").get("chunks", 0))
        except Exception:
            old_n = 0
        # ШИФРУЕМ полезную нагрузку файла (в общем KV — только шифротекст, не сырой файл клиента).
        # Фолбэк на голый base64 только если vault недоступен (функциональность важнее); печатаем предупреждение.
        enc = False
        try:
            payload = _vault_fernet().encrypt(raw).decode()   # вывод Fernet — ASCII base64
            enc = True
        except Exception as e:
            print("WARN: vault недоступен, файл синкается БЕЗ шифрования: " + str(e)[:120])
            payload = _b64.b64encode(raw).decode()
        parts = [payload[i:i + FILE_CHUNK] for i in range(0, len(payload), FILE_CHUNK)]
        deadline = time.monotonic() + 180        # общий бюджет синка
        for i, pt in enumerate(parts):
            ok = False
            for _ in range(4):
                if time.monotonic() > deadline:
                    return None
                if api("/api/kv/set", {"key": base + ":" + str(i), "value": pt,
                                       "description": "filechunk " + str(sid)}, timeout=25).get("status") == "success":
                    ok = True
                    break
            if not ok:
                return None
        # meta пишем ПОСЛЕДНЕЙ (резолвер ориентируется на неё)
        m_ok = False
        for _ in range(4):
            if time.monotonic() > deadline:
                return None
            if api("/api/kv/set", {"key": base + ":meta",
                                   "value": json.dumps({"name": p.name, "chunks": len(parts), "bytes": len(raw), "enc": enc}),
                                   "description": "filemeta " + str(sid)}, timeout=25).get("status") == "success":
                m_ok = True
                break
        if not m_ok:
            return None
        # удаляем ХВОСТ старых чанков (в т.ч. сырые plaintext от прошлого синка) — закрывает утечку
        for i in range(len(parts), old_n):
            api("/api/kv/remove", {"key": base + ":" + str(i)})
        return base
    except Exception:
        return None


def _ns(s):
    """Инъективный namespace-компонент: читаемый префикс + хеш полного значения (усечение НЕ схлопывает разных)."""
    import hashlib
    s = str(s)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:40]
    return safe + "_" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def _derive_vault_key(pin, client=None):
    """Выводит vault-ключ (Fernet-формат) из PIN клиента детерминированно (PBKDF2-HMAC-SHA256,
    600k итераций, per-client соль). Одинаков на маке и хостинге при том же PIN+client → раздавать
    файл-ключ не нужно. ВНИМАНИЕ: стойкость ≈ стойкости PIN (короткий PIN перебираем по шифротексту)."""
    import hashlib
    import base64 as _b64
    client = CLIENT_ID if client is None else client
    salt = hashlib.sha256(("extella-vault:" + str(client)).encode("utf-8")).digest()
    dk = hashlib.pbkdf2_hmac("sha256", str(pin).encode("utf-8"), salt, 600000, dklen=32)
    return _b64.urlsafe_b64encode(dk)   # bytes, совместимо с Fernet


def _tg_api_creds():
    """api_id/api_hash приложения Extella для Telegram (одни на всех клиентов — идентифицируют приложение,
    не пользователя). Из config.json (tg_api_id/tg_api_hash) или dev-credentials. (None,None) если нет."""
    aid, ah = CONFIG.get("tg_api_id"), CONFIG.get("tg_api_hash")
    if aid and ah:
        return int(aid), str(ah)
    try:
        p = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/0. Dronor/credentials/telegram_develpoment_api.txt"
        ls = [l.strip() for l in p.read_text().splitlines() if l.strip()]
        return int(ls[0]), ls[1]
    except Exception:
        return None, None


def _store_client_secret(client, connector, value):
    """Шифрует value конвертом и кладёт в vault (как /x/secret_set) + индекс. Для внутреннего вызова."""
    env = json.dumps({"c": client, "k": connector, "v": value}, ensure_ascii=False)
    ct = _vault_fernet().encrypt(env.encode("utf-8")).decode()
    if api("/api/kv/set", {"key": _secret_kvkey(client, connector), "value": ct, "description": "secret " + connector}).get("status") != "success":
        return False
    try:
        idx = json.loads((api("/api/kv/get", {"key": _secidx_key(client)}) or {}).get("value") or "{}")
    except Exception:
        idx = {}
    idx[connector] = datetime.now(timezone.utc).isoformat()
    api("/api/kv/set", {"key": _secidx_key(client), "value": json.dumps(idx, ensure_ascii=False), "description": "secidx"})
    return True


def _secret_kvkey(client, connector):
    """Namespace секрета в общем сторе: sec:<client>:<connector> (в KV — только шифротекст)."""
    return "sec:" + _ns(client) + ":" + _ns(connector)


def _secidx_key(client):
    """Индекс подключённых коннекторов клиента (одна KV-запись — листинг без скана всего стора)."""
    return "secidx:" + _ns(client)


def _vault_fernet(allow_create=True):
    """Fernet на локальном vault-ключе (файл vault.key, только на устройстве клиента, НЕ в KV, НЕ в пакете wz_wizard_serve).
    Тот же ключ лежит на хостинге клиента. Молчаливую регенерацию не делаем, если секреты уже есть (осиротит их)."""
    from cryptography.fernet import Fernet
    import os as _os
    kp = APP_DIR / "vault.key"
    if not kp.exists():
        if not allow_create:
            raise RuntimeError("vault.key отсутствует")
        # регенерировать можно ТОЛЬКО когда секретов ещё нет — иначе осиротим существующие
        try:
            idx = json.loads((api("/api/kv/get", {"key": _secidx_key(CLIENT_ID)}) or {}).get("value") or "{}")
        except Exception:
            idx = {}
        if idx:
            raise RuntimeError("vault.key отсутствует, а секреты есть — отказ (регенерация осиротит секреты; восстановите ключ)")
        kp.write_bytes(Fernet.generate_key())
        try:
            _os.chmod(kp, 0o600)
        except Exception:
            pass
    return Fernet(kp.read_bytes())


def parse_expert_result(res):
    """run_expert returns result as a python-repr string; recover the dict."""
    if not isinstance(res, dict):
        return {"status": "error", "message": "unexpected response type"}
    raw = res.get("result")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        for loader in (json.loads, ast.literal_eval):
            try:
                v = loader(raw)
                if isinstance(v, dict):
                    return v
            except Exception:
                continue
        return {"status": res.get("status", "unknown"), "raw": raw[:2000]}
    return res


def run_expert(expert_name, params, wait=300, target=None, glob=False):
    body = {"expert_name": expert_name, "params": params}
    if target:
        body["target"] = target      # пиннинг на устройство (напр. процесс-на-источнике на хостинге)
    if glob:
        body["global"] = True
    res = api("/api/expert/run", body)
    task_id = res.get("task_id") if isinstance(res, dict) else None
    # deferred-задача распознаётся ТОЛЬКО по явному признаку "deferred" в result;
    # иначе поле task_id внутри результата эксперта (напр. номер задачи стройки "t1")
    # ошибочно принималось за handle отложенного запуска → 422 uuid_parsing.
    if not task_id and isinstance(res, dict) and isinstance(res.get("result"), str) \
            and "deferred" in res["result"].lower():
        parsed = parse_expert_result(res)
        cand = parsed.get("task_id")
        if isinstance(cand, str) and len(cand) >= 20 and "-" in cand:
            task_id = cand
    if task_id:
        t0 = time.time()
        while time.time() - t0 < wait:
            time.sleep(5)
            st = api("/api/tasks/check", {"task_id": task_id})
            status = str(st.get("status", "")).lower()
            _r = st.get("result")
            _has = _r not in (None, "") and not (isinstance(_r, str) and "deferred" in _r.lower())
            # завершение = ЯВНЫЙ терминальный статус ИЛИ появился result. Прочее (в т.ч. "time:…"-хартбиты
            # с result=null во время выполнения) = задача ещё бежит (иначе медленный Qwen-таск бросался на полпути).
            if status.startswith(("success", "completed", "done", "finished", "ok", "error", "failed", "cancel", "timeout")) or _has:
                return parse_expert_result(st)
        return {"status": "timeout", "task_id": task_id}
    return parse_expert_result(res)


def _sched_kv(sid):
    """Читает расписание процесса из общего KV (sched:<sid>)."""
    g = api("/api/kv/get", {"key": "sched:" + sid})
    if not isinstance(g, dict) or not g.get("value"):
        return None
    try:
        return json.loads(g["value"])
    except Exception:
        return None


def _safe_runs(kvget):
    try:
        return json.loads((kvget or {}).get("value", "{}")).get("runs", []) or []
    except Exception:
        return []


def _sched_scan_sids():
    """Разовый полный проход KV → set sid всех существующих расписаний (sched:<sid>).
    Дорого (у kv/list нет префикс-фильтра — тянет весь стор), поэтому ТОЛЬКО для бутстрапа/
    починки индекса, никогда в горячем пути. Тик в штатном режиме сюда не заходит."""
    lst = api("/api/kv/list", {})
    items = lst.get("results") or lst.get("items") or []
    sids = set()
    for it in items:
        k = it.get("kv_key") or it.get("key") or ""
        if k.startswith("sched:") and k != SCHED_INDEX_KEY:
            sids.add(k[len("sched:"):])
    return sids


def _sched_index_read():
    """Полный индекс активных расписаний как dict {'sids': [...], 'scan_ts': ...} или None."""
    g = api("/api/kv/get", {"key": SCHED_INDEX_KEY})
    if not isinstance(g, dict) or not g.get("value"):
        return None
    try:
        v = json.loads(g["value"])
        if isinstance(v, list):
            v = {"sids": v}
        return v if isinstance(v, dict) and isinstance(v.get("sids"), list) else None
    except Exception:
        return None


def _sched_index_update(add=None, remove=None):
    """Инкрементально правит индекс активных расписаний (sched:__index__) на один sid при
    создании/удалении расписания. Индекса ещё нет → бутстрап разовым сканом (чтобы уже
    существующие расписания не выпали). scan_ts (метка последней полной пересборки) сохраняется —
    её ведёт тик, чтобы страховочный full-scan шёл по интервалу, а не на каждом инкременте.
    Best-effort: при сбое тик восстановит индекс полным сканом."""
    idx = _sched_index_read()
    if idx is None:
        sids = _sched_scan_sids()
        scan_ts = datetime.now(timezone.utc).isoformat()
    else:
        sids = set(idx.get("sids") or [])
        scan_ts = idx.get("scan_ts")
    if add:
        sids.add(add)
    if remove:
        sids.discard(remove)
    val = {"sids": sorted(sids), "updated_at": datetime.now(timezone.utc).isoformat()}
    if scan_ts:
        val["scan_ts"] = scan_ts
    api("/api/kv/set", {"key": SCHED_INDEX_KEY, "value": json.dumps(val, ensure_ascii=False),
                        "description": "schedule index"})


def _inbound_index_update(add=None, remove=None):
    """Инкрементально правит индекс приёма входящих (inbound:__index__) на один sid.
    Индекс новый (не требует бутстрап-скана как расписания). Best-effort."""
    g = api("/api/kv/get", {"key": INBOUND_INDEX_KEY})
    sids = set()
    if isinstance(g, dict) and g.get("value"):
        try:
            v = json.loads(g["value"])
            sids = set(v.get("sids") or []) if isinstance(v, dict) else set(v or [])
        except Exception:
            sids = set()
    if add:
        sids.add(add)
    if remove:
        sids.discard(remove)
    api("/api/kv/set", {"key": INBOUND_INDEX_KEY,
                        "value": json.dumps({"sids": sorted(sids),
                                             "updated_at": datetime.now(timezone.utc).isoformat()},
                                            ensure_ascii=False),
                        "description": "inbound index"})


# ===== B3: источники данных на ВХОД (зеркало коннекторов вывода) =====
# Фиксированное имя выгрузки источника → basename, из которого считается source_key
# (_file_key(sid, basename)); резолвер оркестратора материализует его без изменений.
_SOURCE_BASENAME = {"gsheets": "gsheets_pull.xlsx", "bitrix24": "bitrix24_pull.xlsx",
                    "postgres": "postgres_pull.xlsx", "mysql": "mysql_pull.xlsx",
                    "amocrm": "amocrm_pull.xlsx", "1c_file": "1c_file_pull.xlsx",
                    "1c_winrm": "1c_winrm_pull.xlsx"}


def _source_kindkey(kind):
    k = re.sub(r"[^a-z0-9_]", "", str(kind).lower())[:30]
    return k[4:] if k.startswith("src_") else k


def _source_basename(kind):
    kk = _source_kindkey(kind)
    return _SOURCE_BASENAME.get(kk, kk + "_pull.xlsx")


def _run_source(kind, mode, sid="", source_key=""):
    """Запускает эксперт-источник wz_source_<kind> на ХОСТИНГЕ (пиннинг HOST_TARGET):
    mode='validate' проверка доступа; mode='pull' тянет данные и кладёт в стор под source_key.
    Возвращает dict результата эксперта ({ok, ...})."""
    exp = "wz_source_" + _source_kindkey(kind)
    rr = api("/api/expert/run", {"expert_name": exp, "global": True, "target": HOST_TARGET,
                                 "params": {"api_token": CONFIG["auth_token"], "client": CLIENT_ID,
                                            "mode": mode, "sid": sid, "source_key": source_key}}, 120)
    out = rr.get("result", rr)
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except Exception:
            try:
                import ast as _ast
                out = _ast.literal_eval(out)
            except Exception:
                out = {"raw": out[:150]}
    return out if isinstance(out, dict) else {"ok": False, "raw": str(out)[:150]}


def _audit_experts(names):
    """Детерминированный предзапусковый аудит кода построенных экспертов."""
    import re as _re
    issues = []
    for n in names:
        e = api("/api/expert/get", {"name": n, "global": True})
        code = e.get("expert_code", "") if isinstance(e, dict) else ""
        if not code:
            continue
        checks = {
            "секрет в коде": bool(_re.search(r"(sk-[A-Za-z0-9]{20}|api[_-]?key\s*=\s*['\"][A-Za-z0-9]{16})", code)),
            "отправка почты": ("smtplib" in code or "sendmail" in code),
            "внешняя запись": bool(_re.search(r"https?://(?!api\.extella\.ai|disnet\.extella\.ai)[a-z0-9.]+/", code)),
            "путь устройства": ("/Users/" in code or "/home/" in code),
        }
        for k, v in checks.items():
            if v:
                issues.append(n + ": " + k)
    verdict = "allow" if not issues else "allow-with-confirmation"
    return {"verdict": verdict, "issues": issues}


def _inspect_sample(session_id):
    """Реальные колонки загруженного файла-образца для грануднинга кодогена."""
    fdir = SESS_DIR / (session_id + "_files")
    if not fdir.is_dir():
        return "", None
    files = [p for p in sorted(fdir.iterdir()) if p.is_file()]
    if not files:
        return "", None
    f = files[0]
    ext = f.suffix.lower()
    try:
        if ext in (".xlsx", ".xls"):
            import openpyxl
            wb = openpyxl.load_workbook(str(f), read_only=True, data_only=True)
            ws = wb[wb.sheetnames[0]]
            rows = [[("" if c.value is None else str(c.value)) for c in r]
                    for r in ws.iter_rows(min_row=1, max_row=15)]
            hdr, best = 0, -1
            for i, r in enumerate(rows):
                sc = sum(1 for v in r if v.strip()) + sum(1 for v in r if v.strip() and not v.replace(".", "").replace("-", "").isdigit())
                if sc > best:
                    best, hdr = sc, i
            cols = [v for v in rows[hdr] if v.strip()]
            sample = rows[hdr + 1] if hdr + 1 < len(rows) else []
            hint = ("\n\nФАКТИЧЕСКАЯ СТРУКТУРА ФАЙЛА (СТРОЙ СТРОГО ПОД ЭТИ КОЛОНКИ, не выдумывай поля): "
                    + "лист '" + str(ws.title) + "', заголовки в строке #" + str(hdr + 1)
                    + ", колонки: " + json.dumps(cols, ensure_ascii=False)
                    + ", пример: " + json.dumps(sample, ensure_ascii=False)[:300])
            return hint, str(f)
        if ext == ".csv":
            import csv as _csv
            rd = list(_csv.reader(open(str(f), "r", encoding="utf-8", errors="replace")))
            cols = [v for v in (rd[0] if rd else []) if v.strip()]
            return ("\n\nФАКТИЧЕСКАЯ СТРУКТУРА ФАЙЛА (СТРОЙ ПОД ЭТИ КОЛОНКИ): csv, колонки: "
                    + json.dumps(cols, ensure_ascii=False)), str(f)
    except Exception:
        return "", str(f)
    return "", str(f)


_BUILD_SYS = """Ты — генератор кода СТАДИИ КОНВЕЙЕРА для платформы Extella. Верни ТОЛЬКО JSON:
{"code":"<полный код>", "description":"<англ.: что делает>"}

ЖЁСТКИЙ КОНТРАКТ СТАДИИ (соблюдать точно):
- Сигнатура РОВНО: def <ИМЯ>(input_path: str = "", output_path: str = "") -> dict. НИКАКИХ других параметров.
- ВХОД: читай из input_path. %(INPUT_DESC)s
- РАБОТА: %(PURPOSE)s
- ВЫХОД: запиши результат в output_path как JSON. %(OUTPUT_DESC)s Если это отчётная стадия — можешь дополнительно писать .md/.docx рядом (import docx через include), но JSON в output_path обязателен.
- ВЕРНИ компактный dict: {"status":"success","output_path":output_path, ...ключевые счётчики}. НЕ клади крупные данные в возврат.

Стандарт: первая строка $extens("include.py"); зависимости через include("import X",["extella-pip install X"]) (openpyxl/ docx — так; стдлиб json/csv/datetime — include("import json",[])); РОВНО ОДНА top-level функция (имя строго заданное), хелперы ВНУТРИ неё, не переопределяй include/load_module; валидация входов с ранним return {"status":"error"}; без хардкода путей/ключей; не обращаться к KV."""


def _build_one(expert_name, task, schema_hint, is_first, is_last, accept_input, llm):
    """Кодоген СТАДИИ по единому контракту input_path->output_path + ВНЕШНЕЕ save (persist)
    + приёмка запуском на реальном входе. Возвращает (ok, output_path, detail)."""
    import urllib.request as _u
    cspl = task.get("cspl", "fython")
    if is_first:
        input_desc = ("input_path — путь к ИСХОДНОМУ файлу данных клиента (xlsx/csv). Распарси его "
                      "(openpyxl для xlsx). ВАЖНО: строка заголовков даёт НАЗВАНИЯ колонок; ДАННЫЕ начинаются "
                      "со строки СРАЗУ ПОСЛЕ заголовков. Саму строку заголовков в записи НЕ включай. "
                      "Для КАЖДОЙ непустой строки данных собери словарь {название_колонки: ЗНАЧЕНИЕ ЯЧЕЙКИ (.value)}. "
                      "Значение — число/текст/дата, НЕ номер строки и НЕ индекс колонки. Пропускай пустые строки и "
                      "строки, повторяющие заголовки. Пиши json.dump(..., ensure_ascii=False, default=str)." + schema_hint)
        out_desc = "Запиши НОРМАЛИЗОВАННЫЙ список записей (list of dict со ЗНАЧЕНИЯМИ ячеек) как JSON."
    else:
        input_desc = ("input_path — путь к JSON-файлу от предыдущей стадии (список записей или "
                      "{\"records\":[...],\"summary\":{...}}). Прочитай json.load, работай с записями.")
        out_desc = ("ОБЯЗАТЕЛЬНО ВЫЧИСЛИ агрегаты из входных записей (НЕ копируй записи без обработки!): "
                    "определи числовую колонку ИТОГОВОЙ суммы — ПРЕДПОЧИТАЙ колонку, где в названии есть "
                    "'сумма'/'итог'/'стоимость'/'total'/'amount' (НЕ бери 'цена'/'price'/'цена за единицу', "
                    "если есть колонка итоговой суммы), и посчитай "
                    "total_count (число записей), total_sum (сумма по ней); построй разбивки — словари "
                    "{значение: сумма} по каждой НЕчисловой категориальной колонке (напр. Категория, Способ закупки). "
                    "Запиши JSON {\"summary\": {\"total_count\": N, \"total_sum\": X, \"by_<колонка>\": {...}, ...}, "
                    "\"records\": [...]}. "
                    + ("Это ФИНАЛЬНАЯ стадия — дополнительно собери человекочитаемый отчёт (.md рядом с output_path) "
                       "из summary." if is_last else ""))
    sysmsg = _BUILD_SYS % {"INPUT_DESC": input_desc, "PURPOSE": str(task.get("purpose", "обработай данные")),
                           "OUTPUT_DESC": out_desc}
    user = ("Имя эксперта (СТРОГО): " + expert_name + "\nCSPL: " + cspl +
            "\nНазначение: " + str(task.get("purpose", "")) +
            "\nСгенерируй код стадии строго по контракту (input_path, output_path).")
    out_path = "/tmp/stage_" + expert_name + ".json"
    last_err = None
    for attempt in range(3):
        try:
            if llm.get("api_key"):
                rq = _u.Request(llm["base_url"].rstrip("/") + "/chat/completions",
                                data=json.dumps({"model": llm["model"], "temperature": 0,
                                                 "response_format": {"type": "json_object"},
                                                 "messages": [{"role": "system", "content": sysmsg},
                                                              {"role": "user", "content": user}],
                                                 "max_tokens": 3500}).encode(),
                                headers={"Authorization": "Bearer " + llm["api_key"], "Content-Type": "application/json"},
                                method="POST")
                with _u.urlopen(rq, timeout=150) as r:
                    _content = json.loads(r.read().decode())["choices"][0]["message"]["content"]
            else:
                # платформенная Qwen через агента (кодоген без внешнего ключа)
                _qwen = llm.get("agent_id") or ""   # Qwen-агент клиента; НИКОГДА не Claude-дефолт
                rq = _u.Request(llm.get("api_base", "https://api.extella.ai").rstrip("/") + "/api/agent/run",
                                data=json.dumps({"agent_id": _qwen,
                                                 "input": sysmsg + "\n\n" + user + "\n\nВерни СТРОГО валидный JSON {\"description\":..,\"code\":..} без markdown.",
                                                 "run_timeout": 240, "store": False}).encode(),
                                headers={"X-Auth-Token": llm.get("api_token", ""), "Content-Type": "application/json",
                                         "X-Profile-Id": "default", "X-Agent-Id": _qwen or "agent_extella_default"},
                                method="POST")
                with _u.urlopen(rq, timeout=300) as r:
                    _out = json.loads(r.read().decode())
                _content = "".join(c.get("text", "") for it in (_out.get("output") or [])
                                   if it.get("type") == "message"
                                   for c in (it.get("content") or []) if c.get("type") == "output_text")
                _m = re.search(r"\{.*\}", _content, re.S)
                _content = _m.group(0) if _m else _content
            spec = json.loads(_content)
        except Exception as e:
            # транзиентный HTTP 500 / timeout платформы ≠ провал — ретраим (доктрина Extella)
            last_err = "LLM: " + str(e)[:150]
            time.sleep(2 + attempt * 4)
            continue
        code = spec.get("code", "")
        code = re.sub(r"(?ms)^def\s+(?:load_module|include)\s*\(.*?(?=^\S|\Z)", "", code)
        tops = re.findall(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", code, flags=re.M)
        if len(tops) != 1:
            user += "\n\nОШИБКА: РОВНО ОДНА top-level функция, нашёл " + str(tops) + ". Хелперы внутри."
            continue
        if tops[0] != expert_name:
            code = re.sub(r"^def\s+" + re.escape(tops[0]) + r"\s*\(", "def " + expert_name + "(", code, count=1, flags=re.M)
        sv = api("/api/expert/save", {"name": expert_name, "description": str(spec.get("description", ""))[:900],
                                      "code": code, "kwargs": {"input_path": "", "output_path": ""},
                                      "cspl": cspl, "global": True})
        if sv.get("status") not in ("success", None) and sv.get("id") is None and "error" in str(sv).lower():
            return False, None, "save: " + str(sv)[:150]
        # приёмка = реальный прогон стадии на фактическом входе (это же и звено среза)
        if Path(out_path).exists():
            try: Path(out_path).unlink()
            except Exception: pass
        run_out = run_expert(expert_name, {"input_path": accept_input, "output_path": out_path}, wait=300)
        # СТРОГАЯ приёмка: выход должен быть валидным JSON с непустыми данными (не просто «файл есть»)
        why = None
        if not Path(out_path).exists() or Path(out_path).stat().st_size == 0:
            why = "output_path не создан/пуст; run=" + str(run_out)[:180]
        else:
            try:
                data = json.loads(Path(out_path).read_text(encoding="utf-8"))
            except Exception as e:
                why = "выход не валидный JSON (" + str(e)[:80] + ") — пиши json.dump(ensure_ascii=False, default=str)"
            else:
                recs = data if isinstance(data, list) else (data.get("records") or data.get("rows") or data.get("items") or [])
                if is_first:
                    if not isinstance(recs, list) or len(recs) == 0 or not isinstance(recs[0], dict):
                        why = "первая стадия должна вернуть НЕПУСТОЙ список словарей-записей"
                    else:
                        def _is_headerish(rec):
                            vals = list(rec.values())
                            if vals and all(isinstance(v, int) for v in vals) and (max(vals) - min(vals)) <= len(vals) + 2:
                                return True  # значения-индексы колонок
                            return sum(1 for k, v in rec.items() if str(v).strip() == str(k).strip()) >= max(2, len(rec) // 2)
                        # прагматично: САМИ чистим строки-заголовки/индексы из вывода, не заваливаем сборку
                        cleaned = [r for r in recs if isinstance(r, dict) and not _is_headerish(r)]
                        if not cleaned:
                            why = "после отсева заголовков не осталось записей-данных — парсер не извлёк реальные строки"
                        elif len(cleaned) != len(recs):
                            Path(out_path).write_text(json.dumps(cleaned, ensure_ascii=False, default=str), encoding="utf-8")
                else:
                    summ = data.get("summary") if isinstance(data, dict) else None
                    has_num = isinstance(summ, dict) and any(
                        isinstance(v, (int, float)) or (isinstance(v, dict) and any(isinstance(x, (int, float)) for x in v.values()))
                        for v in summ.values())
                    if not has_num:
                        why = ("стадия обязана ВЫЧИСЛИТЬ summary с числами (total_count/total_sum/by_<колонка>), "
                               "а не копировать записи; сейчас summary пуст или без чисел")
        if why is None:
            return True, out_path, "built+accepted"
        user += "\n\nПРИЁМКА УПАЛА (вход " + str(accept_input) + "): " + why + ". Исправь под контракт."
    return False, None, "3 попытки: " + str(why or last_err or "не прошли приёмку")[:150]


_ORCH_TEMPLATE = '''$extens("include.py")
include("import requests", ["extella-pip install requests"])
include("import openpyxl", ["extella-pip install openpyxl"])
include("from cryptography.fernet import Fernet", ["extella-pip install cryptography"])

def %(NAME)s(source_file: str = "", work_dir: str = "%(WORKDIR)s", api_token: str = "", api_base: str = "https://api.extella.ai", target: str = "", source_key: str = "") -> dict:
    """Автосгенерированный оркестратор процесса. Гоняет контрактную цепочку стадий
    (input_path -> output_path) на исходном файле, чистит заголовки, возвращает сводку
    и рисует отчёт .md + .xlsx. Параметры: source_file, work_dir, api_token, target
    (пиннинг устройства для стадий), source_key (ключ файла в общем сторе для резолвера)."""
    import json
    import requests
    from pathlib import Path
    from datetime import datetime, timezone

    STAGES = %(STAGES)s
    if not api_token:
        cfg = Path.home() / "extella_wizard" / "app" / "config.json"
        try:
            api_token = json.loads(cfg.read_text(encoding="utf-8")).get("auth_token", "") if cfg.exists() else ""
        except Exception:
            api_token = ""
    if not api_token:
        return {"status": "error", "message": "api_token не передан и нет bridge-конфига"}
    if not source_file:
        return {"status": "error", "message": "source_file обязателен"}
    wd = Path(work_dir); wd.mkdir(parents=True, exist_ok=True)
    headers = {"X-Auth-Token": api_token, "Content-Type": "application/json",
               "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}

    # резолвер источника: если локального пути нет (процесс исполняется на хостинге) —
    # материализуем файл из общего стора (KV) чанками base64 в рабочую папку
    _resolve_err = ""
    if source_file and not Path(source_file).exists():
        import base64 as _b64, hashlib as _hl
        _bn = Path(source_file).name
        _base = source_key or ("file:%(SID)s:" + _hl.md5(_bn.encode("utf-8")).hexdigest()[:12])
        try:
            _mr = requests.post(api_base.rstrip("/") + "/api/kv/get", headers=headers, json={"key": _base + ":meta"}, timeout=120).json()
            _mv = _mr.get("value")
            if _mv:
                _meta = json.loads(_mv); _buf = ""
                for _i in range(int(_meta.get("chunks", 0))):
                    _cr = requests.post(api_base.rstrip("/") + "/api/kv/get", headers=headers, json={"key": _base + ":" + str(_i)}, timeout=120).json()
                    _buf += _cr.get("value") or ""
                if _buf:
                    if _meta.get("enc"):
                        # файл зашифрован — расшифровываем ЛОКАЛЬНЫМ vault.key устройства (не из KV)
                        _kc = [Path("/opt/extella-listener/extella_wizard/vault.key"), Path.home() / "extella_wizard/app/vault.key", Path.cwd() / "extella_wizard/vault.key"]
                        _kp = next((c for c in _kc if c.exists()), None)
                        if not _kp:
                            _resolve_err = "зашифрованный источник: локальный vault.key не найден на устройстве-хостинге"
                            _rawf = None
                        else:
                            _rawf = Fernet(_kp.read_bytes()).decrypt(_buf.encode())
                    else:
                        _rawf = _b64.b64decode(_buf)
                    if _rawf:
                        _tmp = wd / _bn
                        _tmp.write_bytes(_rawf)
                        source_file = str(_tmp)
        except Exception as _e:
            _resolve_err = _resolve_err or ("не удалось восстановить источник из стора: " + str(_e)[:120])
    # честный fail вместо тихой пропажи файла (напр. enc без ключа / битые чанки)
    if source_file and not Path(source_file).exists():
        return {"status": "error", "message": _resolve_err or ("источник не найден на устройстве и не восстановлен из стора: " + str(source_file))}

    def is_headerish(rec):
        if not isinstance(rec, dict): return True
        vals = list(rec.values())
        if vals and all(isinstance(v, int) for v in vals) and (max(vals) - min(vals)) <= len(vals) + 2: return True
        return sum(1 for k, v in rec.items() if str(v).strip() == str(k).strip()) >= max(2, len(rec) // 2)

    def clean_file(path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return
        if isinstance(data, list):
            cl = [r for r in data if not is_headerish(r)]
            if cl and len(cl) != len(data):
                Path(path).write_text(json.dumps(cl, ensure_ascii=False, default=str), encoding="utf-8")

    prev, last_out = source_file, None
    for i, name in enumerate(STAGES):
        outp = str(wd / ("stage%%d.json" %% i))
        body = {"expert_name": name, "params": {"input_path": prev, "output_path": outp}, "global": True}
        if target:
            body["target"] = target
        r = requests.post(api_base.rstrip("/") + "/api/expert/run", headers=headers, json=body, timeout=600)
        try:
            res = r.json().get("result", r.json())
        except Exception:
            res = {}
        ok = (isinstance(res, dict) and res.get("status") == "success") or (Path(outp).exists() and Path(outp).stat().st_size > 0)
        if ok and i == 0:
            clean_file(outp)
        if not Path(outp).exists():
            return {"status": "error", "failed_stage": name, "detail": str(res)[:200]}
        prev, last_out = outp, outp

    summary = {}
    try:
        data = json.loads(Path(last_out).read_text(encoding="utf-8"))
        summary = data.get("summary", {}) if isinstance(data, dict) else {}
    except Exception:
        pass
    try:
        (wd / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception:
        pass

    # детерминированный рендер отчёта .md + .xlsx из summary
    md = wd / "report.md"; xlsx = wd / "report.xlsx"
    lines = ["# Сводка процесса", "", "_Сформировано: " + datetime.now(timezone.utc).isoformat()[:16].replace("T", " ") + " UTC_", ""]
    tc = summary.get("total_count"); ts = summary.get("total_sum")
    if tc is not None: lines.append("- Всего позиций: **%%s**" %% tc)
    if ts is not None: lines.append("- Общая сумма: **%%s**" %% format(ts, ",").replace(",", " "))
    for k, v in summary.items():
        if k.startswith("by_") and isinstance(v, dict) and v:
            lines += ["", "## " + k[3:], "", "| Значение | Сумма |", "|---|---|"]
            for kk, vv in list(v.items())[:20]:
                lines.append("| %%s | %%s |" %% (kk, vv))
    md.write_text("\\n".join(lines), encoding="utf-8")
    try:
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Сводка"
        ws.append(["Показатель", "Значение"])
        if tc is not None: ws.append(["Всего позиций", tc])
        if ts is not None: ws.append(["Общая сумма", ts])
        for k, v in summary.items():
            if k.startswith("by_") and isinstance(v, dict) and v:
                ws.append([]); ws.append([k[3:], ""])
                for kk, vv in list(v.items())[:50]:
                    ws.append([kk, vv])
        wb.save(str(xlsx))
    except Exception:
        pass

    result = {"status": "success", "summary": summary, "total_count": tc, "total_sum": ts,
              "report_md": str(md), "report_xlsx": str(xlsx), "host": __import__("socket").gethostname()}
    # межустройственный слепок последнего прогона в KV — планировщик читает его,
    # т.к. вложенный прогон возвращается отложенным без task_id.
    try:
        ns = Path(work_dir).name.replace("_run", "")
        rec = {"at": datetime.now(timezone.utc).isoformat(), "status": "success",
               "total_count": tc, "total_sum": ts, "report_xlsx": str(xlsx),
               "host": __import__("socket").gethostname()}
        requests.post(api_base.rstrip("/") + "/api/kv/set", headers=headers,
                      json={"key": "lastrun:" + ns, "value": json.dumps(rec, ensure_ascii=False, default=str),
                            "description": "lastrun " + ns}, timeout=60)
    except Exception:
        pass
    return result
'''


def _make_orchestrator(ns, stage_names, work_dir, session_id=""):
    """Создаёт (external save → persist) вызываемый оркестратор процесса с вшитыми стадиями.
    session_id вшивается (%(SID)s) для резолвера файла из общего стора на хостинге."""
    name = ns + "_run_pipeline"
    code = _ORCH_TEMPLATE % {"NAME": name, "WORKDIR": work_dir,
                             "STAGES": json.dumps(stage_names, ensure_ascii=False),
                             "SID": session_id}
    sv = api("/api/expert/save", {"name": name,
                                  "description": "Auto-generated process orchestrator: runs the contract pipeline ("
                                                 + " -> ".join(stage_names) + ") on a source file, cleans headers, "
                                                 "returns summary and renders .md/.xlsx report. Params: source_file, work_dir, api_token, target, source_key.",
                                  "code": code, "kwargs": {"source_file": "", "work_dir": work_dir,
                                                           "api_token": "", "api_base": "https://api.extella.ai",
                                                           "target": "", "source_key": ""},
                                  "cspl": "fython", "global": True})
    ok = sv.get("status") == "success" or sv.get("id") is not None
    return (name if ok else None), sv


def _run_build(session_id, build_id):
    """Фоновая стройка процесса: план -> сборка задач -> аудит. Прогресс в build_progress.json."""
    bdir = RUNS_DIR / build_id
    bdir.mkdir(parents=True, exist_ok=True)
    prog = {"build_id": build_id, "session_id": session_id, "status": "running", "stages": []}

    def now():
        return datetime.now(timezone.utc).isoformat()

    def save():
        prog["updated_at"] = now()
        (bdir / "build_progress.json").write_text(json.dumps(prog, ensure_ascii=False, indent=2), encoding="utf-8")

    def stage(sid, title, status="running", **extra):
        for s in prog["stages"]:
            if s["id"] == sid:
                s["status"] = status
                s.update(extra)
                save()
                return
        prog["stages"].append({"id": sid, "title": title, "status": status, **extra})
        save()

    llm = {"api_key": CONFIG.get("llm_api_key", ""), "model": CONFIG.get("llm_model", "gpt-4o"),
           "base_url": CONFIG.get("llm_base_url", "https://api.openai.com/v1"),
           "api_token": CONFIG.get("auth_token", ""), "api_base": BASE,
           # keyless-кодоген идёт на канонический Qwen (см. qwen_agent) — НЕ Claude-дефолт agent_extella_default (жёг бы баланс).
           "agent_id": qwen_agent()}
    tok = {"api_token": CONFIG["auth_token"]}

    # namespace: короткий snake-префикс для экспертов процесса (из имени клиента)
    try:
        _s = json.loads((SESS_DIR / (session_id + ".json")).read_text(encoding="utf-8"))
    except Exception:
        _s = {}
    _words = re.findall(r"[A-Za-z]+", _s.get("client_name", "") or "")
    if _words:
        ns = ("".join(w[0] for w in _words[:3]) if len("".join(w[0] for w in _words[:3])) >= 2 else _words[0][:3]).lower()
    else:
        ns = "p" + session_id.split("_")[-1][:3]
    ns = re.sub(r"[^a-z0-9]", "", ns)[:5] or "proc"

    try:
        # 1. План стройки
        stage("plan", "Составляю план стройки", "running")
        r = run_expert("wz_build_plan", dict(session_id=session_id, namespace=ns, **llm), wait=900)   # llm уже несёт api_token/api_base/api_key (без **tok — иначе дубль api_token)
        if not isinstance(r, dict) or r.get("status") == "error":
            stage("plan", "Составляю план стройки", "error", error=str(r)[:300])
            prog["status"] = "error"; save(); return
        plan_path = SESS_DIR / (session_id + "_build_plan.json")
        if not plan_path.exists():
            stage("plan", "Составляю план стройки", "error", error="план не сохранился")
            prog["status"] = "error"; save(); return
        pdoc = json.loads(plan_path.read_text(encoding="utf-8"))
        plan = pdoc.get("plan", pdoc)
        tasks = plan.get("tasks", [])
        built_names = []
        stage("plan", "Составляю план стройки", "success", tasks_count=len(tasks))

        schema_hint, sample_file = _inspect_sample(session_id)

        # ДАТА-СТАДИИ конвейера (парсинг/анализ/отчёт) — строим ВСЕ заново под единый контракт
        # (реюз старых экспертов не по контракту рвёт цепочку). Не-дата задачи (расписание) — вне среза.
        def is_data_stage(t):
            nm = (t.get("expert_name") or "").lower()
            return not any(x in nm for x in ("schedule", "orchestr", "pipeline", "notif", "send", "email", "cron"))
        data_tasks = [t for t in tasks if is_data_stage(t)]
        other_tasks = [t for t in tasks if not is_data_stage(t)]

        for t in other_tasks:
            tid = t.get("id", "x")
            stage("task_" + tid, "Вне конвейера данных: " + (t.get("title") or t.get("expert_name") or tid),
                  "success", skipped=True)

        # 2. Сборка МОСТОМ по единому контракту + вертикальный срез на реальном файле:
        #    каждая дата-стадия принимает выход предыдущей (первая — исходный файл клиента).
        current_input = sample_file
        slice_ok = bool(sample_file)
        for idx, t in enumerate(data_tasks):
            tid = t.get("id", "t%d" % (idx + 1))
            title = t.get("title") or t.get("expert_name") or tid
            nm = t.get("expert_name") or (ns + "_" + tid)
            stage("task_" + tid, "Собираю и проверяю: " + title, "running")
            if not current_input:
                stage("task_" + tid, "Ошибка: " + title, "error", expert=nm,
                      detail="нет входа для стадии (не приложен файл-образец?)")
                slice_ok = False
                break
            ok, outp, detail = _build_one(nm, t, schema_hint, is_first=(idx == 0),
                                          is_last=(idx == len(data_tasks) - 1),
                                          accept_input=current_input, llm=llm)
            if ok:
                built_names.append(nm)
                current_input = outp  # выход стадии = вход следующей (это и есть срез)
            else:
                slice_ok = False
            stage("task_" + tid, ("Собрано+прогнано: " if ok else "Ошибка: ") + title,
                  "success" if ok else "error", expert=nm, detail=str(detail)[:200])
            if not ok:
                break

        # итог среза: последний output = сводка
        slice_summary = None
        if slice_ok and current_input and current_input != sample_file and Path(current_input).exists():
            try:
                sdata = json.loads(Path(current_input).read_text(encoding="utf-8"))
                slice_summary = sdata.get("summary") if isinstance(sdata, dict) else {"records": len(sdata)}
                prog["slice_output"] = current_input
                prog["slice_summary"] = slice_summary
            except Exception:
                pass

        built_ok = [n for n in built_names if n]
        had_build_tasks = any(str(t.get("action", "build")).lower() != "reuse" for t in tasks)
        if had_build_tasks and not built_ok:
            prog["status"] = "error"
            prog["error"] = "ни один компонент не собрался (сборщик не смог пройти приёмку — вероятно, нужен файл-образец)"
            save(); return

        # 3. Автосоздание вызываемого оркестратора процесса (стадии — построенные дата-эксперты)
        orchestrator = None
        stage_experts = [t.get("expert_name") or (ns + "_" + t.get("id", "")) for t in data_tasks]
        stage_experts = [n for n in stage_experts if n in built_ok]
        if stage_experts:
            stage("orchestrator", "Собираю оркестратор процесса", "running")
            orchestrator, _sv = _make_orchestrator(ns, stage_experts, "/tmp/" + ns + "_run", session_id)
            stage("orchestrator", "Оркестратор процесса: " + (orchestrator or "ошибка"),
                  "success" if orchestrator else "error", expert=orchestrator)
            if orchestrator:
                prog["orchestrator"] = orchestrator

        # 4. Аудит перед запуском
        stage("audit", "Проверяю процесс перед запуском", "running")
        aud = _audit_experts([n for n in built_names if n])
        prog["audit"] = aud
        prog["built_experts"] = [n for n in built_names if n]
        stage("audit", "Проверяю процесс перед запуском", "success",
              verdict=aud["verdict"], issues=aud["issues"])

        prog["status"] = "built"
        save()
        # отметка в сессии
        try:
            sp = SESS_DIR / (session_id + ".json")
            s = json.loads(sp.read_text(encoding="utf-8"))
            s["stage"] = "built"
            s.setdefault("builds", []).append({"build_id": build_id, "at": now(),
                                               "experts": prog["built_experts"], "audit": aud,
                                               "orchestrator": orchestrator,
                                               "slice_summary": prog.get("slice_summary"),
                                               "source_file": sample_file})
            s["updated_at"] = now()
            sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    except Exception as e:
        prog["status"] = "error"; prog["error"] = str(e)[:300]; save()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep stdout log terse
        print("%s %s" % (self.command, self.path))

    def _send(self, obj, code=200, ctype="application/json; charset=utf-8"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _blocked_origin(self):
        """CSRF-защита мутирующих эндпоинтов: блокируем ЯВНО внешние веб-origin (evil.com),
        пропускаем локальные/тулбарные (пусто, null, 127.0.0.1/localhost, не-http схемы)."""
        o = (self.headers.get("Origin", "") or "").strip()
        if not o or o == "null":
            return False
        m = re.match(r"^https?://([^/:]+)", o)
        if not m:
            return False
        return m.group(1) not in ("127.0.0.1", "localhost")

    # ---------------- reads: local files ----------------
    def do_GET(self):
        path, _, query = self.path.partition("?")
        qs = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        if path in ("/", "/wizard.html", "/index.html"):
            self._send((APP_DIR / "wizard.html").read_bytes(), ctype="text/html; charset=utf-8")
        elif path == "/x/download":
            # скачать отчёт прогона. Только локальный файл под разрешёнными корнями (защита от обхода путей).
            import urllib.parse as _up, mimetypes as _mt
            raw = _up.unquote(qs.get("path", ""))
            fp = None
            try:
                cand = Path(raw).resolve()
                roots = [Path("/tmp").resolve(), Path("/private/tmp").resolve(),
                         (Path.home() / "extella_wizard").resolve(), SESS_DIR.resolve()]
                if cand.is_file() and any(str(cand) == str(r) or str(cand).startswith(str(r) + "/") for r in roots):
                    fp = cand
            except Exception:
                fp = None
            if not fp:
                self._send({"status": "error", "message": "файл недоступен (нет локально или не на этом устройстве)"}, 404)
                return
            data = fp.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", _mt.guess_type(fp.name)[0] or "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % fp.name)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/x/health":
            import os as _os, socket as _sock
            self._send({"status": "ok", "version": BRIDGE_VERSION, "pid": _os.getpid(),
                        "uptime_s": int(time.time() - _START_TS),
                        "sessions": len(list(SESS_DIR.glob("wz_*.json"))),
                        "host": _sock.gethostname()})
        elif path == "/x/update_check":
            man = _release_trusted_manifest() or {}   # только ПОДПИСАННЫЙ релиз считается
            latest = man.get("version")
            halted = bool(man.get("disabled"))        # стоп-кран: релиз отозван (подписанный halt)
            poison = None
            try:
                poison = json.loads((APP_DIR / ".update_poison").read_text()).get("version") if (APP_DIR / ".update_poison").exists() else None
            except Exception:
                poison = None
            avail = (not halted) and bool(latest) and _ver_tuple(latest) > _ver_tuple(BRIDGE_VERSION) and latest != poison
            self._send({"status": "success", "current": BRIDGE_VERSION, "latest": latest,
                        "update_available": avail, "signed": bool(man), "halted": halted, "poison_skipped": poison})
        elif path == "/x/secrets":
            # список подключённых коннекторов клиента — ТОЛЬКО имена/даты, БЕЗ значений.
            # Арендатор = этот мост (CLIENT_ID); читаем один индекс, а не сканируем весь KV.
            client = CLIENT_ID
            try:
                idx = json.loads((api("/api/kv/get", {"key": _secidx_key(client)}) or {}).get("value") or "{}")
            except Exception:
                idx = {}
            out = [{"connector": k, "set_at": v} for k, v in sorted(idx.items())]
            self._send({"status": "success", "client": client, "secrets": out})
        elif path == "/x/catalog":
            self._send(json.loads(CATALOG_PATH.read_text(encoding="utf-8")))
        elif path == "/x/library":
            # Industry library for the UI depth panel: checklist + taxonomy + regulatory.
            # ?industry=<id> returns one industry; no arg returns the manifest only.
            mf = _lib_manifest()
            industry = qs.get("industry", "")
            if not industry:
                self._send({"status": "success", "manifest": mf})
                return
            ent = _lib_entry(industry)
            if not ent or not ent.get("available"):
                self._send({"status": "success", "industry": industry, "available": False,
                            "title": (ent or {}).get("title", industry),
                            "manifest": mf})
                return
            out = {"status": "success", "industry": industry, "available": True,
                   "title": ent.get("title", industry),
                   "validated_on": ent.get("validated_on"),
                   "reuse_from_core_pct": ent.get("reuse_from_core_pct"),
                   "checklist": None, "taxonomy": None, "regulatory_md": None}
            try:
                if ent.get("checklist") and (LIB_DIR / ent["checklist"]).exists():
                    out["checklist"] = json.loads((LIB_DIR / ent["checklist"]).read_text(encoding="utf-8"))
                if ent.get("taxonomy") and (LIB_DIR / ent["taxonomy"]).exists():
                    out["taxonomy"] = json.loads((LIB_DIR / ent["taxonomy"]).read_text(encoding="utf-8"))
                if ent.get("regulatory") and (LIB_DIR / ent["regulatory"]).exists():
                    out["regulatory_md"] = (LIB_DIR / ent["regulatory"]).read_text(encoding="utf-8")
            except Exception as e:
                out["load_error"] = str(e)[:200]
            self._send(out)
        elif path == "/x/sessions":
            out = []
            for p in sorted(SESS_DIR.glob("wz_*.json")):
                if p.name.endswith("_blueprint.json"):
                    continue
                try:
                    s = json.loads(p.read_text(encoding="utf-8"))
                    out.append({"session_id": s.get("session_id"), "client_name": s.get("client_name"),
                                "stage": s.get("stage"), "updated_at": s.get("updated_at"),
                                "answers_count": len(s.get("answers", {})),
                                "comments_open": sum(1 for c in s.get("comments", []) if not c.get("resolved"))})
                except Exception:
                    continue
            self._send({"sessions": out})
        elif path == "/x/session":
            sid = qs.get("id", "")
            p = SESS_DIR / (sid + ".json")
            if not sid or not p.exists() or "/" in sid or ".." in sid:
                self._send({"status": "error", "message": "session not found"}, 404)
            else:
                self._send(json.loads(p.read_text(encoding="utf-8")))
        elif path == "/x/blueprint":
            sid = qs.get("session_id", "")
            p = SESS_DIR / (sid + "_blueprint.json")
            if not sid or not p.exists() or "/" in sid or ".." in sid:
                self._send({"status": "error", "message": "blueprint not generated yet"}, 404)
            else:
                self._send(json.loads(p.read_text(encoding="utf-8")))
        elif path == "/x/spec":
            sid = qs.get("session_id", "")
            p = SESS_DIR / (sid + "_spec.md")
            if not SAFE_ID.match(sid or "") or not p.exists():
                self._send({"status": "error", "message": "spec not generated yet"}, 404)
            else:
                self._send({"status": "success", "markdown": p.read_text(encoding="utf-8")})
        elif path == "/x/automations":
            # список построенных процессов-автоматизаций (по сессиям с builds)
            out = []
            for p in sorted(SESS_DIR.glob("wz_*.json"), reverse=True):
                if p.name.endswith(("_blueprint.json", "_build_plan.json")):
                    continue
                try:
                    s = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    continue
                builds = s.get("builds") or []
                if not builds:
                    continue
                lb = builds[-1]
                bp = None
                bpp = SESS_DIR / (s.get("session_id", "") + "_blueprint.json")
                if bpp.exists():
                    try:
                        bp = json.loads(bpp.read_text(encoding="utf-8")).get("blueprint", {})
                    except Exception:
                        bp = None
                manual_runs = s.get("runs") or []
                skv = _sched_kv(s.get("session_id", ""))
                sched_runs = (skv or {}).get("runs") or []
                runs = sorted(manual_runs + sched_runs, key=lambda r: r.get("at", ""))
                out.append({
                    "session_id": s.get("session_id"),
                    "client_name": s.get("client_name"),
                    "process_name": (bp or {}).get("process_name") or s.get("client_name"),
                    "stage": s.get("stage"),
                    "components": lb.get("experts") or [],
                    "orchestrator": lb.get("orchestrator"),
                    "audit": (lb.get("audit") or {}).get("verdict"),
                    "slice_summary": lb.get("slice_summary"),
                    "source_file": lb.get("source_file"),
                    "schedule": s.get("schedule"),
                    "production_agent": s.get("production_agent"),
                    "runs_count": len(runs),
                    "last_run": runs[-1] if runs else None,
                    "stages_meta": [{"title": st.get("title"), "inputs": st.get("inputs"),
                                     "outputs": st.get("outputs"), "capability_ids": st.get("capability_ids")}
                                    for st in ((bp or {}).get("stages") or [])],
                })
            self._send({"status": "success", "automations": out})
        elif path == "/x/monitor":
            # Сводное ЗДОРОВЬЕ пилота: по каждому процессу — расписание/просрочка, последний прогон,
            # источник, доставка (connlog), входящие; агрегатный health ok/warn/error. Для панели наблюдения.
            now_dt = datetime.now(timezone.utc)
            try:
                if _MON_CACHE["at"] and (now_dt - _MON_CACHE["at"]).total_seconds() < 12:
                    self._send(_MON_CACHE["resp"]); return   # свежий кэш — мгновенный ответ
            except Exception:
                pass
            procs = []
            summ = {"total": 0, "scheduled": 0, "healthy": 0, "warn": 0, "error": 0, "overdue": 0}
            valid = []
            for p in sorted(SESS_DIR.glob("wz_*.json"), reverse=True):
                if p.name.endswith(("_blueprint.json", "_build_plan.json")):
                    continue
                try:
                    s = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    # битый/полузаписанный файл сессии — не прячем, показываем как error (сигнал надзору)
                    procs.append({"session_id": p.stem, "process_name": p.stem, "health": "error",
                                  "reasons": ["файл сессии повреждён/нечитаем"], "schedule": None,
                                  "last_run": None, "runs_count": 0, "source": None, "deliver": None,
                                  "delivery": None, "inbound": None, "production_agent": None})
                    continue
                if not s.get("builds"):
                    continue
                valid.append(s)

            def _mon_one(s):
                # карточка здоровья одного процесса (до 3 KV-round-trip) — вызывается параллельно
                sid = s.get("session_id", "")
                skv = _sched_kv(sid) or {}
                sched = s.get("schedule") or {}
                runs = sorted((s.get("runs") or []) + (skv.get("runs") or []), key=lambda r: r.get("at", ""))
                last = runs[-1] if runs else None
                interval = int(skv.get("interval_min") or sched.get("interval_min") or 0)
                nd = skv.get("next_due_ts")
                overdue = False
                if sched and nd and interval:
                    try:
                        due = datetime.fromisoformat(nd.replace("Z", "+00:00"))
                        overdue = (now_dt - due).total_seconds() > interval * 60 * 2   # позади >2 интервалов → тик не сработал
                    except Exception:
                        overdue = False
                deliver = str(skv.get("deliver") or sched.get("deliver") or "").strip().lower()
                delivery = None
                if deliver and re.match(r"^[a-z0-9_]+$", deliver):
                    try:
                        gv = api("/api/kv/get", {"key": "connlog:" + _ns(CLIENT_ID) + ":" + deliver}, 15)
                        delivery = json.loads(gv["value"]) if isinstance(gv, dict) and gv.get("value") else None
                    except Exception:
                        delivery = None
                inbound = None
                try:
                    ig = api("/api/kv/get", {"key": "inbound:" + sid}, 15)
                    ic = json.loads(ig["value"]) if isinstance(ig, dict) and ig.get("value") else None
                    if ic:
                        inbound = {"mode": ic.get("mode"), "channel": ic.get("channel"), "last_inbound_ts": ic.get("last_inbound_ts")}
                except Exception:
                    inbound = None
                src = s.get("source") or None
                health = "ok"
                reasons = []
                if last and last.get("status") not in (None, "success"):
                    health = "error"; reasons.append("последний прогон: " + str(last.get("status")))
                if overdue:
                    health = "error"; reasons.append("расписание просрочено (тик не сработал)")
                if deliver and delivery and delivery.get("ok") is False:
                    if health == "ok":
                        health = "warn"
                    reasons.append("последняя доставка не прошла")
                if sched and interval and not last:
                    if health == "ok":
                        health = "warn"
                    reasons.append("ещё не было прогона")
                return {
                    "session_id": sid,
                    "process_name": s.get("client_name") or sid,
                    "health": health, "reasons": reasons,
                    "schedule": ({"period": sched.get("period"), "interval_min": interval,
                                  "next_due": nd, "overdue": overdue} if sched else None),
                    "last_run": ({"at": (last or {}).get("at"), "status": (last or {}).get("status"),
                                  "total_sum": (last or {}).get("total_sum"), "total_count": (last or {}).get("total_count")} if last else None),
                    "runs_count": len(runs),
                    "source": ({"kind": src.get("kind"), "refresh": src.get("refresh")} if src else None),
                    "deliver": deliver or None,
                    "delivery": ({"at": delivery.get("at"), "ok": delivery.get("ok"), "err": delivery.get("err")} if delivery else None),
                    "inbound": inbound,
                    "production_agent": s.get("production_agent"),
                }

            # ПАРАЛЛЕЛЬНО: иначе до 3 KV-round-trip × N сессий последовательно = ~26с
            try:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=8) as _ex:
                    procs.extend(list(_ex.map(_mon_one, valid)))
            except Exception:
                procs.extend([_mon_one(s) for s in valid])

            for pr in procs:   # агрегируем сводку из готовых карточек
                summ["total"] += 1
                summ[{"ok": "healthy", "warn": "warn", "error": "error"}.get(pr.get("health"), "error")] += 1
                sc = pr.get("schedule") or {}
                if sc.get("interval_min"):
                    summ["scheduled"] += 1
                if sc.get("overdue"):
                    summ["overdue"] += 1
            # процессы с проблемами — вперёд
            order = {"error": 0, "warn": 1, "ok": 2}
            procs.sort(key=lambda x: order.get(x["health"], 3))
            _resp = {"status": "success", "at": now_dt.isoformat(), "summary": summ, "processes": procs}
            _MON_CACHE["at"] = datetime.now(timezone.utc); _MON_CACHE["resp"] = _resp   # TTL с момента ЗАВЕРШЕНИЯ
            self._send(_resp)
        elif path == "/x/runs":
            sid = qs.get("session_id", "")
            sp = SESS_DIR / (sid + ".json")
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
            else:
                s = json.loads(sp.read_text(encoding="utf-8"))
                sched_runs = (_sched_kv(sid) or {}).get("runs") or []
                runs = sorted((s.get("runs") or []) + sched_runs, key=lambda r: r.get("at", ""))
                self._send({"status": "success", "runs": runs})
        elif path == "/x/files":
            sid = qs.get("session_id", "")
            sp = SESS_DIR / (sid + ".json")
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
            else:
                s = json.loads(sp.read_text(encoding="utf-8"))
                self._send({"status": "success", "files": s.get("files", [])})
        elif path == "/x/build_progress":
            bid = qs.get("build_id", "")
            bp = RUNS_DIR / bid / "build_progress.json"
            if not SAFE_ID.match(bid or "") or not bp.exists():
                self._send({"status": "error", "message": "build not found"}, 404)
            else:
                self._send({"status": "success", "progress": json.loads(bp.read_text(encoding="utf-8"))})
        elif path == "/x/demo_progress":
            rid = qs.get("run_id", "")
            rd = RUNS_DIR / rid
            if not SAFE_ID.match(rid or "") or not rd.exists():
                self._send({"status": "error", "message": "run not found"}, 404)
                return
            try:
                prog = json.loads((rd / "progress.json").read_text(encoding="utf-8"))
            except Exception:
                prog = {"run_id": rid, "status": "starting", "steps": []}
            subs = [{"id": f, "title": t, "done": (rd / f).exists()}
                    for f, t in PIPELINE_ARTIFACTS]
            self._send({"status": "success", "progress": prog,
                        "pipeline_substages": subs,
                        "result_ready": (rd / "result.json").exists()})
        elif path == "/x/demo_result":
            rid = qs.get("run_id", "")
            rd = RUNS_DIR / rid
            if not SAFE_ID.match(rid or "") or not (rd / "result.json").exists():
                self._send({"status": "error", "message": "result not ready"}, 404)
            else:
                self._send(json.loads((rd / "result.json").read_text(encoding="utf-8")))
        elif not path.startswith("/x/"):
            # SPA-фолбэк: любой не-API GET (тулбар может грузить /wizard.html, deep-link и т.п.) → отдаём приложение
            self._send((APP_DIR / "wizard.html").read_bytes(), ctype="text/html; charset=utf-8")
        else:
            self._send({"status": "error", "message": "not found"}, 404)

    # ---------------- writes: platform runs ----------------
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            self._send({"status": "error", "message": "bad JSON"}, 400)
            return

        if self.path == "/x/quit":
            # мягкое завершение (для «перенимания» порта новым/владельческим инстансом) — только localhost (мост слушает 127.0.0.1)
            self._send({"status": "ok", "message": "shutting down"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        if self.path == "/x/update_apply":
            # безопасное само-обновление: ПОДПИСЬ → sha256 → компиляция → РЕАЛЬНЫЙ smoke → атомарный своп → рестарт
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            import os as _os, sys as _sys, shutil as _sh, subprocess as _sp, py_compile as _pc, socket as _sock, tempfile as _tf
            if not _OWNER:   # только launchd-инстанс применяет: иначе os._exit некому респавнить → кирпич
                self._send({"status": "error", "applied": False, "message": "обновление только на управляемом сервисе (launchd)"}, 409)
                return
            if (APP_DIR / ".update_state").exists():   # уже идёт обновление — не затираем .prev промежуточной версией
                self._send({"status": "error", "applied": False, "message": "обновление уже в процессе"}, 409)
                return
            if not _UPDATE_LOCK.acquire(blocking=False):
                self._send({"status": "error", "applied": False, "message": "обновление уже выполняется"}, 409)
                return
            try:
                man = _release_trusted_manifest()   # ПОДПИСЬ Ed25519 проверена; иначе None
                if not man:
                    self._send({"status": "error", "applied": False, "message": "релиз без валидной подписи — отклонён"}, 403)
                    return
                if man.get("disabled"):             # стоп-кран: релиз отозван — не применяем
                    self._send({"status": "success", "applied": False, "reason": "halted", "message": "канал релизов остановлен (стоп-кран)"})
                    return
                latest = man.get("version")
                if _ver_tuple(latest) <= _ver_tuple(BRIDGE_VERSION):
                    self._send({"status": "success", "applied": False, "reason": "up-to-date", "current": BRIDGE_VERSION, "latest": latest})
                    return
                try:
                    if (APP_DIR / ".update_poison").exists() and json.loads((APP_DIR / ".update_poison").read_text()).get("version") == latest:
                        self._send({"status": "error", "applied": False, "message": "версия помечена проблемной (был откат) — пропуск"}, 409)
                        return
                except Exception:
                    pass
                fl = man.get("files", {})
                srv_new = _release_download("server.py", (fl.get("server.py") or {}).get("sha256", ""), (fl.get("server.py") or {}).get("chunks", 0))
                html_new = _release_download("wizard.html", (fl.get("wizard.html") or {}).get("sha256", ""), (fl.get("wizard.html") or {}).get("chunks", 0))
                if not srv_new or not html_new:
                    self._send({"status": "error", "applied": False, "message": "download/sha256 mismatch"}, 502)
                    return
                stg_srv = APP_DIR / "server.py.new"; stg_html = APP_DIR / "wizard.html.new"
                stg_srv.write_bytes(srv_new); stg_html.write_bytes(html_new)
                try:
                    _pc.compile(str(stg_srv), doraise=True)   # синтаксис
                except Exception as e:
                    stg_srv.unlink(missing_ok=True); stg_html.unlink(missing_ok=True)
                    self._send({"status": "error", "applied": False, "message": "compile failed: " + str(e)[:150]}, 500)
                    return
                # РЕАЛЬНЫЙ smoke: новый код+UI в temp-каталоге на эфемерном порту → health(новая версия)+GET/ 200 HTML
                smoke_ok, smoke_err, proc = False, "", None
                tmp = _tf.mkdtemp(prefix="wzsmoke_")
                try:
                    _sh.copy2(str(stg_srv), str(Path(tmp) / "server.py"))
                    _sh.copy2(str(stg_html), str(Path(tmp) / "wizard.html"))
                    _sh.copy2(str(APP_DIR / "config.json"), str(Path(tmp) / "config.json"))
                    _s = _sock.socket(); _s.bind(("127.0.0.1", 0)); sport = _s.getsockname()[1]; _s.close()
                    proc = _sp.Popen([_sys.executable, str(Path(tmp) / "server.py"), "--smoke", str(sport)],
                                     cwd=tmp, stdout=_sp.PIPE, stderr=_sp.PIPE)
                    for _ in range(30):
                        time.sleep(0.5)
                        if proc.poll() is not None:
                            smoke_err = "новый код упал на старте: " + (proc.stderr.read().decode(errors="ignore")[-200:] if proc.stderr else "")
                            break
                        try:
                            hh = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/x/health" % sport, timeout=2).read().decode())
                        except Exception:
                            continue
                        if str(hh.get("version")) != str(latest):
                            smoke_err = "health версия %s != %s" % (hh.get("version"), latest); break
                        try:
                            gg = urllib.request.urlopen("http://127.0.0.1:%d/" % sport, timeout=3)
                            body = gg.read(400)
                            bl = body.lower()
                            is_html = bl.startswith(b"<!") or b"<html" in bl or b"<!doctype" in bl
                            smoke_ok = (gg.status == 200 and is_html)
                            if not smoke_ok:
                                smoke_err = "GET / не отдал HTML (status %s)" % gg.status
                        except Exception as e:
                            smoke_err = "GET / упал: " + str(e)[:120]
                        break
                    if not smoke_ok and not smoke_err:
                        smoke_err = "новый код не поднялся за отведённое время"
                except Exception as e:
                    smoke_err = "smoke error: " + str(e)[:150]
                finally:
                    try:
                        if proc and proc.poll() is None: proc.terminate()
                    except Exception: pass
                    _sh.rmtree(tmp, ignore_errors=True)
                if not smoke_ok:
                    stg_srv.unlink(missing_ok=True); stg_html.unlink(missing_ok=True)
                    self._send({"status": "error", "applied": False, "message": "smoke-тест не пройден: " + smoke_err[:200]}, 500)
                    return
                # маркер ДО необратимого свопа; бэкап; атомарный своп (server.py — ПОСЛЕДНИМ, он триггерит рестарт)
                (APP_DIR / ".update_state").write_text(json.dumps({"to": latest, "from": BRIDGE_VERSION, "attempts": 0,
                                                                   "state": "swapping", "at": datetime.now(timezone.utc).isoformat()}))
                _sh.copy2(str(APP_DIR / "server.py"), str(APP_DIR / "server.py.prev"))
                _sh.copy2(str(APP_DIR / "wizard.html"), str(APP_DIR / "wizard.html.prev"))
                _os.replace(str(stg_html), str(APP_DIR / "wizard.html"))
                _os.replace(str(stg_srv), str(APP_DIR / "server.py"))
                self._send({"status": "success", "applied": True, "from": BRIDGE_VERSION, "to": latest,
                            "message": "обновление применено (подпись+smoke ок), перезапуск..."})
                def _restart():
                    time.sleep(0.6); _os._exit(3)
                threading.Thread(target=_restart, daemon=True).start()
                return
            finally:
                try:
                    _UPDATE_LOCK.release()
                except Exception:
                    pass

        if self.path in ("/x/connector_test", "/x/connector_send"):
            # интеграции: коннектор-эксперт на ХОСТИНГЕ расшифровывает токен из vault и вызывает внешний API.
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            connector = re.sub(r"[^a-z0-9_]", "", str(body.get("connector", "telegram")).lower())[:30]
            mode = "send" if self.path == "/x/connector_send" else "validate"
            text = str(body.get("text", ""))[:3000]
            rr = api("/api/expert/run", {"expert_name": "wz_connector_" + connector, "global": True, "target": HOST_TARGET,
                                         "params": {"api_token": CONFIG["auth_token"], "client": CLIENT_ID, "mode": mode, "text": text}}, 60)
            out = rr.get("result", rr)
            if isinstance(out, str):
                try:
                    out = json.loads(out)
                except Exception:
                    try:
                        import ast as _ast
                        out = _ast.literal_eval(out)
                    except Exception:
                        out = {"raw": out[:150]}
            ok = isinstance(out, dict) and out.get("ok")
            self._send({"status": "success" if ok else "error", "connector": connector, "result": out})
            return

        if self.path in ("/x/source_test", "/x/source_pull"):
            # B3: эксперт-источник на ХОСТИНГЕ тянет данные из CRM/БД/Sheets. test=validate, pull=выгрузка в стор.
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            kind = re.sub(r"[^a-z0-9_]", "", str(body.get("kind", "src_gsheets")).lower())[:30]
            if self.path == "/x/source_test":
                out = _run_source(kind, "validate")
                ok = isinstance(out, dict) and out.get("ok")
                self._send({"status": "success" if ok else "error", "kind": kind, "result": out})
                return
            sid = str(body.get("session_id", ""))
            if not SAFE_ID.match(sid or ""):
                self._send({"status": "error", "message": "bad session_id"}, 400)
                return
            skey = _file_key(sid, _source_basename(kind))
            out = _run_source(kind, "pull", sid, skey)
            ok = isinstance(out, dict) and out.get("ok")
            self._send({"status": "success" if ok else "error", "kind": kind, "source_key": skey, "result": out})
            return

        if self.path == "/x/source_bind":
            # B3: привязать источник к процессу ВМЕСТО загруженного файла (kind='off' — отвязать).
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            sid = str(body.get("session_id", ""))
            sp = SESS_DIR / (sid + ".json")
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            s = json.loads(sp.read_text(encoding="utf-8"))
            kind = re.sub(r"[^a-z0-9_]", "", str(body.get("kind", "src_gsheets")).lower())[:30]
            if kind == "off":
                s.pop("source", None)
                s["updated_at"] = datetime.now(timezone.utc).isoformat()
                sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
                self._send({"status": "success", "source": None})
                return
            if not (s.get("builds") or []):
                self._send({"status": "error", "message": "нет построенного процесса"}, 400)
                return
            basename = _source_basename(kind)
            skey = _file_key(sid, basename)
            src_path = str(SESS_DIR / (sid + "_files") / basename)
            # первый pull — наполнить стор данными до первого прогона (доказывает доступ к источнику)
            out = _run_source(kind, "pull", sid, skey)
            if not (isinstance(out, dict) and out.get("ok")):
                self._send({"status": "error", "message": "источник не отдал данные: " + str((out or {}).get("err") or out)[:170], "result": out}, 502)
                return
            s["builds"][-1]["source_file"] = src_path
            s["source"] = {"kind": kind, "basename": basename, "source_key": skey,
                           "refresh": "per_run", "set_at": datetime.now(timezone.utc).isoformat()}
            s["updated_at"] = datetime.now(timezone.utc).isoformat()
            sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
            self._send({"status": "success", "source": s["source"], "rows": (out or {}).get("rows")})
            return

        if self.path == "/x/onboard":
            # A2: одно-кликовый онбординг устройства клиента — оркестратор wz_onboard_device
            # (мост+библиотека+vault+автозапуск), пиннингом на <target>. Токен подставляем сервером.
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            target = str(body.get("target", "")).strip()
            if not target:
                self._send({"status": "error", "message": "нужен target (id устройства клиента в Extella)"}, 400)
                return
            params = {"api_token": CONFIG["auth_token"], "target": target,
                      "client": str(body.get("client", CLIENT_ID)),
                      "pin": str(body.get("pin", "")),
                      "llm_api_key": CONFIG.get("llm_api_key", ""),
                      "llm_base_url": CONFIG.get("llm_base_url", "https://api.openai.com/v1"),
                      "llm_model": CONFIG.get("llm_model", "gpt-4o"),
                      "port": int(body.get("port", 8765) or 8765),
                      "seed_library": bool(body.get("seed_library", True)),
                      "autostart": bool(body.get("autostart", True))}
            if body.get("app_dir"):
                params["app_dir"] = str(body["app_dir"])
            if body.get("label"):
                params["label"] = str(body["label"])
            rr = api("/api/expert/run", {"expert_name": "wz_onboard_device", "global": True, "params": params}, 900)
            out = rr.get("result", rr)
            if isinstance(out, str):
                try:
                    out = json.loads(out)
                except Exception:
                    try:
                        import ast as _a
                        out = _a.literal_eval(out)
                    except Exception:
                        out = {"raw": out[:200]}
            ready = isinstance(out, dict) and out.get("ready")
            self._send({"status": "success" if ready else "error", "result": out})
            return

        if self.path == "/x/tg_login_start":
            # вход Telegram через аккаунт (MTProto): шлём код на телефон. Telethon на этом устройстве.
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            phone = str(body.get("phone", "")).strip()
            target = str(body.get("target", "me")).strip() or "me"
            aid, ah = _tg_api_creds()
            if not aid or not ah:
                self._send({"status": "error", "message": "нет api_id/api_hash Telegram (config.tg_api_id/tg_api_hash)"}, 400)
                return
            if not phone:
                self._send({"status": "error", "message": "нужен телефон (+7...)"}, 400)
                return
            try:
                import asyncio
                from telethon import TelegramClient
                from telethon.sessions import StringSession
                async def _st():
                    cl = TelegramClient(StringSession(), aid, ah)
                    await cl.connect()
                    try:
                        sent = await cl.send_code_request(phone)
                        return sent.phone_code_hash, cl.session.save()
                    finally:
                        await cl.disconnect()
                pch, ss = asyncio.run(_st())
            except Exception as e:
                self._send({"status": "error", "message": "не удалось отправить код: " + str(e)[:150]}, 500)
                return
            lid = uuid.uuid4().hex[:12]
            _TG_LOGIN[lid] = {"phone": phone, "hash": pch, "ss": ss, "target": target, "aid": aid, "ah": ah}
            self._send({"status": "success", "login_id": lid, "message": "код отправлен в Telegram"})
            return

        if self.path == "/x/tg_login_complete":
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            lid = str(body.get("login_id", ""))
            code = str(body.get("code", "")).strip()
            pw = str(body.get("password", ""))
            st = _TG_LOGIN.get(lid)
            if not st:
                self._send({"status": "error", "message": "сессия входа не найдена/истекла — начните заново"}, 404)
                return
            try:
                import asyncio
                from telethon import TelegramClient
                from telethon.sessions import StringSession
                from telethon.errors import SessionPasswordNeededError
                async def _cp():
                    cl = TelegramClient(StringSession(st["ss"]), st["aid"], st["ah"])
                    await cl.connect()
                    try:
                        try:
                            await cl.sign_in(st["phone"], code, phone_code_hash=st["hash"])
                        except SessionPasswordNeededError:
                            if not pw:
                                return {"need_password": True}
                            await cl.sign_in(password=pw)
                        me = await cl.get_me()
                        return {"ok": True, "acct": (me.username or me.first_name), "ss": cl.session.save()}
                    finally:
                        await cl.disconnect()
                r = asyncio.run(_cp())
            except Exception as e:
                self._send({"status": "error", "message": "вход не удался: " + str(e)[:150]}, 500)
                return
            if r.get("need_password"):
                self._send({"status": "need_password", "message": "включена 2FA — введите пароль облака Telegram"})
                return
            if not r.get("ok"):
                self._send({"status": "error", "message": "вход не удался"}, 500)
                return
            secret = json.dumps({"mode": "mtproto", "api_id": st["aid"], "api_hash": st["ah"],
                                 "session": r["ss"], "target": st["target"]})
            ok = _store_client_secret(CLIENT_ID, "telegram", secret)
            _TG_LOGIN.pop(lid, None)
            self._send({"status": "success" if ok else "error", "acct": r.get("acct"), "stored": ok,
                        "message": "аккаунт подключён" if ok else "не удалось сохранить в vault"})
            return

        if self.path == "/x/vault_provision":
            # онбординг без SSH: из PIN выводим vault-ключ и на маке, и на хостинге (одинаково) — файл-ключ не раздаём.
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            import os as _os, hashlib as _hl
            pin = str(body.get("pin", ""))
            if len(pin) < 6:
                self._send({"status": "error", "message": "PIN слишком короткий (мин. 6 символов; рекомендуется фраза)"}, 400)
                return
            key = _derive_vault_key(pin)
            # расшифровывает ли выведенный ключ уже сохранённые секреты? (тот же PIN, что шифровал?)
            decrypts = None
            try:
                idx = json.loads((api("/api/kv/get", {"key": _secidx_key(CLIENT_ID)}) or {}).get("value") or "{}")
                if idx:
                    from cryptography.fernet import Fernet as _F
                    ct = (api("/api/kv/get", {"key": _secret_kvkey(CLIENT_ID, sorted(idx)[0])}) or {}).get("value")
                    try:
                        _F(key).decrypt(ct.encode()); decrypts = True
                    except Exception:
                        decrypts = False
            except Exception:
                pass
            kp = APP_DIR / "vault.key"
            kp.write_bytes(key)
            try:
                _os.chmod(kp, 0o600)
            except Exception:
                pass
            mac_sha = _hl.sha256(key).hexdigest()[:16]
            # провижининг хостинга тем же PIN (эксперт wz_vault_provision, пиннинг на HOST_TARGET)
            host = {}
            try:
                rr = api("/api/expert/run", {"expert_name": "wz_vault_provision", "global": True, "target": HOST_TARGET,
                                             "params": {"pin": pin, "client": CLIENT_ID}}, 60)
                out = rr.get("result", rr)
                if isinstance(out, str):
                    try:
                        out = json.loads(out)
                    except Exception:
                        try:
                            import ast as _ast
                            out = _ast.literal_eval(out)
                        except Exception:
                            out = {"raw": out[:150]}
                host = out if isinstance(out, dict) else {"raw": str(out)[:150]}
            except Exception as e:
                host = {"err": str(e)[:120]}
            match = host.get("key_sha256") == mac_sha
            self._send({"status": "success", "provisioned_mac": True, "mac_key_sha256": mac_sha,
                        "decrypts_existing": decrypts, "host": host, "keys_match": match})
            return

        if self.path == "/x/secret_set":
            # сохранить секрет клиента (токен бота/CRM/БД) ШИФРОВАННО. Арендатор = ЭТОТ мост (CLIENT_ID),
            # client_id из запроса игнорируем (иначе можно писать в чужой namespace). Значение НЕ логируем/НЕ возвращаем.
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            client = CLIENT_ID
            connector = str(body.get("connector", "")).strip()
            value = str(body.get("value", ""))
            if not connector or not value:
                self._send({"status": "error", "message": "connector и value обязательны"}, 400)
                return
            try:
                # конверт привязан к (client, connector) — защита от cut-and-paste шифротекста между namespace
                env = json.dumps({"c": client, "k": connector, "v": value}, ensure_ascii=False)
                ct = _vault_fernet().encrypt(env.encode("utf-8")).decode()
            except Exception as e:
                self._send({"status": "error", "message": "vault error: " + str(e)[:150]}, 500)
                return
            r1 = api("/api/kv/set", {"key": _secret_kvkey(client, connector), "value": ct, "description": "secret " + connector})
            if r1.get("status") != "success":
                self._send({"status": "error", "message": "KV write failed", "stored": False})
                return
            # индекс коннекторов — только ПОСЛЕ успешной записи секрета (без «призраков»)
            now = datetime.now(timezone.utc).isoformat()
            try:
                idx = json.loads((api("/api/kv/get", {"key": _secidx_key(client)}) or {}).get("value") or "{}")
            except Exception:
                idx = {}
            idx[connector] = now
            api("/api/kv/set", {"key": _secidx_key(client), "value": json.dumps(idx, ensure_ascii=False), "description": "secidx"})
            self._send({"status": "success", "connector": connector, "stored": True})
            return

        if self.path == "/x/secret_remove":
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            client = CLIENT_ID
            connector = str(body.get("connector", "")).strip()
            r = api("/api/kv/remove", {"key": _secret_kvkey(client, connector)})
            removed = r.get("status") == "success"
            try:
                idx = json.loads((api("/api/kv/get", {"key": _secidx_key(client)}) or {}).get("value") or "{}")
                idx.pop(connector, None)
                api("/api/kv/set", {"key": _secidx_key(client), "value": json.dumps(idx, ensure_ascii=False), "description": "secidx"})
            except Exception:
                pass
            self._send({"status": "success" if removed else "error", "connector": connector, "removed": removed})
            return

        if self.path == "/x/expert":
            expert = str(body.get("expert_name", ""))
            params = body.get("params") or {}
            if expert not in ("wz_session", "wz_generate_blueprint", "wz_project_spec", "wz_data_reality_check"):
                self._send({"status": "error", "message": "expert not allowed via bridge"}, 403)
                return
            if expert in ("wz_generate_blueprint", "wz_data_reality_check"):
                params.setdefault("api_key", CONFIG.get("llm_api_key", ""))
                params.setdefault("base_url", CONFIG.get("llm_base_url", "https://api.openai.com/v1"))
                params.setdefault("model", CONFIG.get("llm_model", "gpt-4o"))
                params.setdefault("api_token", CONFIG.get("auth_token", ""))            # платформенная модель, если api_key пуст (клиенту OpenAI-ключ не нужен)
                params.setdefault("agent_id", qwen_agent())  # канонический Qwen, НЕ Claude-дефолт
            self._send(run_expert(expert, params, glob=True))   # эксперты визарда — global; без флага платформа их не находит ("Expert not found")

        elif self.path == "/x/demo_run":
            sid = str(body.get("session_id", ""))
            if sid and not SAFE_ID.match(sid):
                self._send({"status": "error", "message": "bad session_id"}, 400)
                return
            run_id = "demo_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M") + "_" + uuid.uuid4().hex[:4]
            industry = str(body.get("industry", "insurance"))
            params = {
                "api_token": CONFIG["auth_token"],
                "llm_api_key": CONFIG.get("llm_api_key", ""),
                "llm_base_url": CONFIG.get("llm_base_url", "https://api.openai.com/v1"),
                "run_id": run_id,
                "session_id": sid,
                "industry": industry,
                "n_dialogues": int(body.get("n_dialogues", 40)),
                "sample_n": int(body.get("sample_n", 10)),
            }
            # If this industry has a seeded library, run the demo on its checklist
            # (matrix "processes x industries" + regulatory criteria) instead of the
            # generic 9-criterion default embedded in wz_run_demo.
            _clp = _lib_checklist_path(industry)
            if _clp:
                params["checklist_path"] = _clp
            # pre-create the run dir so progress polling works immediately
            (RUNS_DIR / run_id).mkdir(parents=True, exist_ok=True)
            threading.Thread(target=run_expert,
                             args=("wz_run_demo", params),
                             kwargs={"wait": 3600}, daemon=True).start()
            self._send({"status": "success", "run_id": run_id})

        elif self.path == "/x/build":
            sid = str(body.get("session_id", ""))
            if not SAFE_ID.match(sid or "") or not (SESS_DIR / (sid + ".json")).exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            if not (SESS_DIR / (sid + "_blueprint.json")).exists():
                self._send({"status": "error", "message": "сначала соберите план процесса"}, 400)
                return
            build_id = "build_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M") + "_" + uuid.uuid4().hex[:4]
            (RUNS_DIR / build_id).mkdir(parents=True, exist_ok=True)
            (RUNS_DIR / build_id / "build_progress.json").write_text(
                json.dumps({"build_id": build_id, "session_id": sid, "status": "running", "stages": []},
                           ensure_ascii=False), encoding="utf-8")
            threading.Thread(target=_run_build, args=(sid, build_id), daemon=True).start()
            self._send({"status": "success", "build_id": build_id})

        elif self.path == "/x/deploy":
            sid = str(body.get("session_id", ""))
            sp = SESS_DIR / (sid + ".json")
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            s = json.loads(sp.read_text(encoding="utf-8"))
            builds = s.get("builds", [])
            if not builds:
                self._send({"status": "error", "message": "процесс ещё не построен"}, 400)
                return
            verdict = (builds[-1].get("audit") or {}).get("verdict", "")
            if verdict == "escalate":
                self._send({"status": "error", "message": "аудит требует эскалации — запуск заблокирован"}, 403)
                return
            if not body.get("confirmed"):
                self._send({"status": "need_confirm", "verdict": verdict,
                            "issues": (builds[-1].get("audit") or {}).get("issues", [])})
                return
            # Планирование расписания оркестратора — честный последний шаг self-serve.
            # Полноценный продовый агент (Qwen) = UI-копия руками (ограничение платформы),
            # поэтому здесь фиксируем готовность к запуску и отдаём инструкции.
            s["stage"] = "launched"
            s.setdefault("log", []).append({"ts": datetime.now(timezone.utc).isoformat(),
                                            "event": "process approved for launch by owner"})
            s["updated_at"] = datetime.now(timezone.utc).isoformat()
            sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
            self._send({"status": "success", "experts": builds[-1].get("experts", [])})

        elif self.path == "/x/run_process":
            sid = str(body.get("session_id", ""))
            sp = SESS_DIR / (sid + ".json")
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            s = json.loads(sp.read_text(encoding="utf-8"))
            builds = s.get("builds") or []
            if not builds or not builds[-1].get("orchestrator"):
                self._send({"status": "error", "message": "у процесса нет оркестратора — соберите процесс"}, 400)
                return
            orch = builds[-1]["orchestrator"]
            src = body.get("source_file") or builds[-1].get("source_file")
            if not src:
                self._send({"status": "error", "message": "нет исходного файла для прогона"}, 400)
                return
            if s.get("source"):
                # процесс-на-источнике: свежий pull → прогон на хостинге с source_key (резолвер материализует)
                _si = s["source"]
                _skey = _si.get("source_key") or _file_key(sid, _si.get("basename", ""))
                _pout = _run_source(_si.get("kind"), "pull", sid, _skey)
                if not (isinstance(_pout, dict) and _pout.get("ok")):
                    self._send({"status": "error", "message": "источник не отдал данные: " + str((_pout or {}).get("err") or _pout)[:170]})
                    return
                res = run_expert(orch, {"api_token": CONFIG["auth_token"], "source_file": src,
                                        "source_key": _skey, "target": HOST_TARGET},
                                 wait=900, target=HOST_TARGET, glob=True)
            else:
                res = run_expert(orch, {"api_token": CONFIG["auth_token"], "source_file": src}, wait=900)
            summ = res.get("summary") if isinstance(res, dict) else None
            run_rec = {"at": datetime.now(timezone.utc).isoformat(),
                       "status": (res or {}).get("status", "unknown"),
                       "summary": summ, "total_count": (res or {}).get("total_count"),
                       "total_sum": (res or {}).get("total_sum"),
                       "report_md": (res or {}).get("report_md"), "report_xlsx": (res or {}).get("report_xlsx"),
                       "source_file": src}
            s.setdefault("runs", []).append(run_rec)
            s["updated_at"] = datetime.now(timezone.utc).isoformat()
            sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
            # доставка результата в канал (как по расписанию) — чтобы РУЧНОЙ запуск тоже слал, а не только тик
            delivered = None
            deliver = str((s.get("schedule") or {}).get("deliver") or "").strip().lower()
            if deliver and re.match(r"^[a-z0-9_]+$", deliver) and (res or {}).get("status") == "success":
                tc = (res or {}).get("total_count"); ts = (res or {}).get("total_sum")
                msg = "✅ Extella: процесс отработал (ручной запуск)."
                if tc is not None:
                    msg += "\nПозиций: " + str(tc)
                if ts is not None:
                    msg += "\nСумма: " + format(ts, ",").replace(",", " ") + " ₸"
                dr = api("/api/expert/run", {"expert_name": "wz_connector_" + deliver, "global": True, "target": HOST_TARGET,
                                             "params": {"api_token": CONFIG["auth_token"], "client": CLIENT_ID, "mode": "send", "text": msg}}, 60)
                dout = dr.get("result", dr)
                if isinstance(dout, str):
                    try:
                        dout = json.loads(dout)
                    except Exception:
                        try:
                            import ast as _a
                            dout = _a.literal_eval(dout)
                        except Exception:
                            dout = {}
                delivered = {"channel": deliver, "ok": bool(isinstance(dout, dict) and dout.get("ok")),
                             "err": ((dout or {}).get("err") if isinstance(dout, dict) else None)}
            self._send({"status": "success", "run": run_rec, "delivered": delivered})

        elif self.path == "/x/schedule":
            sid = str(body.get("session_id", ""))
            sp = SESS_DIR / (sid + ".json")
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            s = json.loads(sp.read_text(encoding="utf-8"))
            period = str(body.get("period", ""))[:40]
            interval_min = int(body.get("interval_min", 0) or 0)
            lb = (s.get("builds") or [{}])[-1]
            orch = lb.get("orchestrator")
            src = lb.get("source_file")
            kvkey = "sched:" + sid
            if not period:
                s.pop("schedule", None)
                api("/api/kv/remove", {"key": kvkey})
                _sched_index_update(remove=sid)   # снять sid с индекса активных расписаний
            else:
                _bn = Path(src).name if src else ""
                _skey = _file_key(sid, _bn) if _bn else ""
                # синк файла на хостинг в ФОНЕ (если исходник ещё на этом устройстве) — байты в общий стор
                if src and Path(src).exists():
                    threading.Thread(target=_sync_file_to_store, args=(sid, src), daemon=True).start()
                _tgt = str(body.get("target", HOST_TARGET))  # процесс по расписанию гоняем на хостинге 24/7
                s["schedule"] = {"period": period, "interval_min": interval_min,
                                 "set_at": datetime.now(timezone.utc).isoformat(), "orchestrator": orch,
                                 "target": _tgt}
                _deliver = str(body.get("deliver", "") or "")   # напр. "telegram" — куда слать результат прогона
                # общий стор для тика планировщика на always-on устройстве
                kvval = {"session_id": sid, "period": period, "interval_min": interval_min,
                         "orchestrator": orch, "source_file": src, "source_basename": _bn,
                         "source_key": _skey, "target": _tgt, "active": True,
                         "deliver": _deliver, "client": CLIENT_ID,
                         "next_due_ts": datetime.now(timezone.utc).isoformat(),
                         "runs": ((api("/api/kv/get", {"key": kvkey}) or {}).get("value") and
                                  _safe_runs(api("/api/kv/get", {"key": kvkey}))) or []}
                if s.get("source"):
                    kvval["source"] = s["source"]   # B3: тик сделает свежий pull источника ПЕРЕД прогоном
                api("/api/kv/set", {"key": kvkey, "value": json.dumps(kvval, ensure_ascii=False),
                                    "description": "schedule " + sid})
                _sched_index_update(add=sid)      # внести sid в индекс активных расписаний
            s["updated_at"] = datetime.now(timezone.utc).isoformat()
            sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
            self._send({"status": "success", "schedule": s.get("schedule")})

        elif self.path == "/x/inbound":
            # B2: приём входящих сообщений процессом. Развилка mode: poll | webhook | off.
            sid = str(body.get("session_id", ""))
            sp = SESS_DIR / (sid + ".json")
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            s = json.loads(sp.read_text(encoding="utf-8"))
            mode = str(body.get("mode", "poll")).strip().lower()   # poll | webhook | off
            channel = str(body.get("channel", "telegram")).strip().lower()
            ikey = "inbound:" + sid
            if mode == "off":
                # снять приём: удалить конфиг, индекс и (если был) hookmap
                prev = api("/api/kv/get", {"key": ikey})
                try:
                    pj = json.loads(prev.get("value") or "{}") if isinstance(prev, dict) else {}
                except Exception:
                    pj = {}
                if pj.get("route_token"):
                    api("/api/kv/remove", {"key": "hookmap:" + pj["route_token"]})
                api("/api/kv/remove", {"key": ikey})
                _inbound_index_update(remove=sid)
                s.pop("inbound", None)
                s["updated_at"] = datetime.now(timezone.utc).isoformat()
                sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
                self._send({"status": "success", "inbound": None})
                return
            if channel.replace("_", "").isalnum() is False or mode not in ("poll", "webhook"):
                self._send({"status": "error", "message": "bad mode/channel"}, 400)
                return
            lb = (s.get("builds") or [{}])[-1]
            orch = lb.get("orchestrator")
            src = lb.get("source_file")
            if not orch:
                self._send({"status": "error", "message": "нет построенного процесса (orchestrator)"}, 400)
                return
            _bn = Path(src).name if src else ""
            _skey = _file_key(sid, _bn) if _bn else ""
            if src and Path(src).exists():
                threading.Thread(target=_sync_file_to_store, args=(sid, src), daemon=True).start()
            _tgt = str(body.get("target", HOST_TARGET))   # приём и запуск — на хостинге 24/7
            cfg = {"session_id": sid, "channel": channel, "mode": mode, "client": CLIENT_ID,
                   "orchestrator": orch, "source_file": src, "source_basename": _bn,
                   "source_key": _skey, "target": _tgt, "active": True,
                   "offset": 0, "seen": [],
                   "set_at": datetime.now(timezone.utc).isoformat()}
            hook_url = None
            if mode == "webhook":
                # непрозрачный route_token в URL шлюза; hookmap:<token> → (client, sid, channel).
                # setWebhook в канале выполнится, КОГДА поднят публичный шлюз (2 шага в PS.kz/DNS).
                route_token = uuid.uuid4().hex + uuid.uuid4().hex[:8]  # непрозрачный 160-бит токен пути
                cfg["route_token"] = route_token
                cfg["gateway_status"] = "pending_gateway"   # шлюз ещё не поднят
                api("/api/kv/set", {"key": "hookmap:" + route_token,
                                    "value": json.dumps({"client": CLIENT_ID, "sid": sid,
                                                         "channel": channel, "active": True,
                                                         "created_at": datetime.now(timezone.utc).isoformat()},
                                                        ensure_ascii=False),
                                    "description": "hookmap"})
                hook_url = "https://gw.dronor.ai/hook/" + route_token
            api("/api/kv/set", {"key": ikey, "value": json.dumps(cfg, ensure_ascii=False),
                                "description": "inbound " + sid})
            _inbound_index_update(add=sid)
            s["inbound"] = {"mode": mode, "channel": channel, "target": _tgt,
                            "set_at": cfg["set_at"], "hook_url": hook_url,
                            "gateway_status": cfg.get("gateway_status")}
            s["updated_at"] = datetime.now(timezone.utc).isoformat()
            sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
            self._send({"status": "success", "inbound": s["inbound"]})

        elif self.path == "/x/deploy_agent":
            # перепрошивка UI-копии Qwen на оркестратор процесса (агент как сервис)
            sid = str(body.get("session_id", ""))
            agent_id = str(body.get("agent_id", "")).strip()
            sp = SESS_DIR / (sid + ".json")
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            if not agent_id.startswith("agent_"):
                self._send({"status": "error", "message": "нужен agent_id UI-копии (Qwen). Создайте копию базового агента в Extella (2 клика) и вставьте её id"}, 400)
                return
            s = json.loads(sp.read_text(encoding="utf-8"))
            builds = s.get("builds") or []
            orch = (builds[-1] if builds else {}).get("orchestrator")
            if not orch:
                self._send({"status": "error", "message": "у процесса нет оркестратора"}, 400)
                return
            bpp = SESS_DIR / (sid + "_blueprint.json")
            pname = s.get("client_name", "процесс")
            try:
                pname = json.loads(bpp.read_text(encoding="utf-8")).get("blueprint", {}).get("process_name", pname)
            except Exception:
                pass
            src = (builds[-1] or {}).get("source_file") or ""
            instr = ("Ты — рабочий агент процесса «" + pname + "» на платформе Extella.\n\n"
                     "# Как запускать процесс\nВесь процесс — один оркестратор. Чтобы сформировать результат, вызови "
                     "run_expert: expert_name=\"" + orch + "\", global=true, params={\"source_file\": \"" + src + "\"}. "
                     "Он проходит всю цепочку и возвращает summary + отчёт .md/.xlsx.\n"
                     "# Результат\nЦитируй ФАКТИЧЕСКИЕ числа из summary (total_count, total_sum, разбивки by_). Не выдумывай. "
                     "Ошибку оркестратора покажи как есть.\n"
                     "# Дисциплина\nОдин инструмент за ход; цитируй фактический результат; без псевдо-вызовов.\n"
                     "# Границы\nТолько чтение; наружу ничего не пишешь/не отправляешь. Заморожен (F2): не меняешь эксперты/правила. "
                     "Изменения процесса — через Строителя (сессия " + sid + ").\n# Стиль\nДеловой русский, кратко, с цифрами.")
            upd = api("/api/agent/update", {"agent_id": agent_id, "instructions": instr})
            if isinstance(upd, dict) and upd.get("id") == agent_id:
                s["production_agent"] = {"agent_id": agent_id, "name": upd.get("name"),
                                         "orchestrator": orch, "deployed_at": datetime.now(timezone.utc).isoformat()}
                s["updated_at"] = datetime.now(timezone.utc).isoformat()
                sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
                self._send({"status": "success", "agent_id": agent_id, "name": upd.get("name"), "orchestrator": orch})
            else:
                self._send({"status": "error", "message": "не удалось перепрошить агента: " + str(upd)[:200]})

        elif self.path == "/x/upload":
            sid = str(body.get("session_id", ""))
            fname = re.sub(r"[^\w .\-()Ѐ-ӿ]", "_", str(body.get("filename", "file")))[:120]
            sp = SESS_DIR / (sid + ".json")
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            try:
                import base64 as _b64
                content = _b64.b64decode(str(body.get("content_base64", "")))
            except Exception:
                self._send({"status": "error", "message": "bad base64"}, 400)
                return
            if len(content) > 25 * 1024 * 1024:
                self._send({"status": "error", "message": "file too large (>25MB)"}, 400)
                return
            fdir = SESS_DIR / (sid + "_files")
            fdir.mkdir(parents=True, exist_ok=True)
            fpath = fdir / fname
            fpath.write_bytes(content)
            # синк на хостинг в ФОНЕ (не блокируем ответ загрузки): байты в общий стор
            threading.Thread(target=_sync_file_to_store, args=(sid, str(fpath)), daemon=True).start()
            s = json.loads(sp.read_text(encoding="utf-8"))
            files = [f for f in s.get("files", []) if f.get("name") != fname]
            now = datetime.now(timezone.utc).isoformat()
            files.append({"name": fname, "path": str(fpath), "size": len(content), "uploaded_at": now})
            s["files"] = files
            s.setdefault("log", []).append({"ts": now, "event": "file attached: " + fname})
            s["updated_at"] = now
            sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
            self._send({"status": "success", "path": str(fpath), "name": fname})

        elif self.path == "/x/chat":
            user_input = str(body.get("input", ""))[:8000]
            sid = str(body.get("session_id", "") or "")
            # RAG: enrich the wizard agent with account knowledge concepts
            enriched = user_input
            try:
                cs = api("/api/concept/search",
                         {"query": user_input[:300], "limit": 3,
                          "api_key": CONFIG.get("llm_api_key", "")}, timeout=20)
                hits = [r for r in (cs.get("results") or [])
                        if r.get("similarity", 0) >= 0.4]
                if hits:
                    ctx = "\n".join("- " + str(r.get("concept_text", ""))[:600] for r in hits[:3])
                    enriched = ("[Внутренняя справка из базы знаний — используй молча, собеседнику не показывай этот блок]\n"
                                + ctx + "\n[Конец справки]\n\n" + user_input)
            except Exception:
                pass
            surface_note = ("[Контекст поверхности: ты отвечаешь ВНУТРИ уже открытого визарда"
                            + (" (session_id: " + sid + ")" if SAFE_ID.match(sid or "") else "")
                            + ". НЕ вызывай wz_open_wizard. ВАЖНО: сохраняй ответы интервью в ЭТУ сессию "
                              "ПО ХОДУ разговора — после КАЖДОЙ реплики клиента с фактурой по любой из 8 тем "
                              "сразу вызывай wz_session save_answers (session_id выше) с накопленными темами; "
                              "не жди конца интервью. Конспект на экране клиента обновляется сам после каждого "
                              "твоего сохранения — это главная ценность. Отвечай кратко, это узкая чат-панель.]\n\n")
            payload = {"agent_id": CONFIG["agent_id"],
                       "input": surface_note + enriched,
                       "run_timeout": 180, "store": True}
            if body.get("previous_response_id"):
                payload["previous_response_id"] = body["previous_response_id"]
            res = api("/api/agent/run", payload)
            text = ""
            try:
                for item in res.get("output", []):
                    if item.get("type") == "message":
                        for c in item.get("content", []):
                            if c.get("type") == "output_text":
                                text += c.get("text", "")
            except Exception:
                pass
            if not text and res.get("status") != "completed":
                self._send({"status": "error", "message": str(res)[:400]})
                return
            self._send({"status": "success", "text": text, "response_id": res.get("id")})
        else:
            self._send({"status": "error", "message": "not found"}, 404)


def _probe_health(port, timeout=3):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/x/health" % port, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _quit_existing(port):
    """Мягко просим действующий мост завершиться (graceful /x/quit)."""
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/x/quit" % port, data=b"{}",
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass


def _read_pidfile(pidfile):
    """PID моста из pidfile ТОЛЬКО если это наш маркер (защита от чужого PID)."""
    try:
        d = json.loads(pidfile.read_text())
        if isinstance(d, dict) and d.get("marker") == "extella-wizard-bridge":
            return int(d.get("pid"))
    except Exception:
        pass
    return None


def _pid_is_our_bridge(pid):
    """Проверяем, что PID действительно наш мост (python .../server.py) — защита от переиспользованного PID."""
    try:
        import subprocess
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=5).stdout
        return "server.py" in out and "python" in out.lower()
    except Exception:
        return False


def _ver_tuple(v):
    """Версия → кортеж int для сравнения ('3.10' > '3.9')."""
    try:
        return tuple(int(x) for x in re.split(r"[.\-]", str(v)) if x.isdigit())
    except Exception:
        return (0,)


def _release_trusted_manifest():
    """Читает rel:bridge:meta и ПРОВЕРЯЕТ подпись Ed25519 нашим публичным ключом.
    Возвращает ДОВЕРЕННЫЙ manifest {version, files:{name:{sha256,chunks,bytes}}, ...} или None.
    Только подписанный нами манифест признаётся — общий KV не даёт подсунуть чужой код."""
    try:
        rm = json.loads((api("/api/kv/get", {"key": REL_PREFIX + ":meta"}) or {}).get("value") or "{}")
        man, sig = rm.get("manifest"), rm.get("sig")
        if not man or not sig:
            return None
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        import base64 as _b64
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(RELEASE_PUBKEY_HEX)).verify(_b64.b64decode(sig), man.encode("utf-8"))
        m = json.loads(man)
        # валиден либо полный релиз (files), либо подписанный стоп-кран (disabled)
        if isinstance(m, dict) and m.get("version") and (isinstance(m.get("files"), dict) or m.get("disabled")):
            return m
        return None
    except Exception:
        return None   # нет подписи / подпись невалидна / повреждено → релиз не доверяем


def _release_download(name, expected_sha256, chunks):
    """Скачивает файл релиза из KV чанками и сверяет с ДОВЕРЕННЫМ sha256 из подписанного манифеста. bytes или None."""
    import base64 as _b64
    import hashlib
    try:
        if int(chunks) <= 0:
            return None
        buf = ""
        for i in range(int(chunks)):
            v = (api("/api/kv/get", {"key": REL_PREFIX + ":" + name + ":" + str(i)}) or {}).get("value")
            if v is None:
                return None
            buf += v
        raw = _b64.b64decode(buf)
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            return None   # содержимое не соответствует подписанному манифесту
        return raw
    except Exception:
        return None


if __name__ == "__main__":
    import os
    import signal
    import sys
    import shutil
    port = int(CONFIG.get("port", 8765))
    pidfile = APP_DIR / "server.pid"

    # режим SMOKE: поднять НОВЫЙ код на переданном эфемерном порту (проверка апдейта в temp-каталоге), без single-instance/маркера
    if "--smoke" in sys.argv:
        _sport = int(sys.argv[sys.argv.index("--smoke") + 1])
        _START_TS = time.time()
        ThreadingHTTPServer(("127.0.0.1", _sport), Handler).serve_forever()
        raise SystemExit(0)

    # режим самопроверки (импорты/модульный код ок) → выход 0 без bind/serve
    if "--selfcheck" in sys.argv:
        print("selfcheck ok v" + BRIDGE_VERSION)
        raise SystemExit(0)

    # launchd — единоличный владелец сервиса (env EXTELLA_BRIDGE_OWNER=launchd); только он самоперезапускается.
    owner = os.environ.get("EXTELLA_BRIDGE_OWNER") == "launchd"
    _OWNER = owner   # модульный флаг для /x/update_apply (обновление применяет только владелец)

    # пост-обновление: ТОЛЬКО владелец ведёт счётчик/откат (не-owner старты уступают порт, маркер не трогают)
    _um = APP_DIR / ".update_state"
    if owner and _um.exists():
        try:
            _st = json.loads(_um.read_text())
        except Exception:
            _st = {}
        _st["attempts"] = int(_st.get("attempts", 0)) + 1
        if _st["attempts"] > MAX_UPDATE_ATTEMPTS:
            _bad = _st.get("to")
            try:
                if (APP_DIR / "server.py.prev").exists():
                    shutil.copy2(str(APP_DIR / "server.py.prev"), str(APP_DIR / "server.py"))
                if (APP_DIR / "wizard.html.prev").exists():
                    shutil.copy2(str(APP_DIR / "wizard.html.prev"), str(APP_DIR / "wizard.html"))
            except Exception:
                pass
            try:
                if _bad:
                    (APP_DIR / ".update_poison").write_text(json.dumps({"version": _bad}))   # не применять эту версию повторно
            except Exception:
                pass
            try:
                _um.unlink()
            except Exception:
                pass
            print("UPDATE ROLLBACK: откат после %d неуспешных стартов (проблемная версия: %s)" % (_st["attempts"], _bad))
            os._exit(4)   # НЕнулевой → launchd респавнит восстановленный код
        else:
            _um.write_text(json.dumps(_st))
            _mypid = os.getpid()
            # подтверждение: через 10с health отвечает ИМЕННО этот процесс (pid) новой версии → снимаем маркер
            def _confirm_update(_target=_st.get("to"), _pid=_mypid):
                time.sleep(10)
                h = _probe_health(port)
                if h and str(h.get("version")) == str(_target) and int(h.get("pid", -1)) == _pid:
                    try:
                        (APP_DIR / ".update_state").unlink()
                    except Exception:
                        pass
            threading.Thread(target=_confirm_update, daemon=True).start()

    # ЕДИНСТВЕННЫЙ ИНСТАНС + ВЛАДЕНИЕ:
    existing = _probe_health(port)
    if existing:
        same_ver = str(existing.get("version")) == str(BRIDGE_VERSION)
        if same_ver and not owner:
            # ручной/тулбарный запуск той же версии — уступаем (безопасный no-op)
            print("bridge v%s уже работает (pid %s) — выходим без конфликта" % (BRIDGE_VERSION, existing.get("pid")))
            raise SystemExit(0)
        # владелец (launchd) ИЛИ другая версия — перенимаем порт (launchd всегда владеет; новый код вытесняет старый)
        print("перенимаю порт у моста pid %s версии %s (owner=%s)" % (existing.get("pid"), existing.get("version"), owner))
        _quit_existing(port)

    # занимаем порт с ретраями; TOCTOU-safe: перед kill повторно проверяем health и личность процесса
    srv = None
    for _ in range(24):  # ~12с
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            h = _probe_health(port)
            if h:
                # на порту ЖИВОЙ мост (возможно, выиграл гонку старта) — НЕ убиваем его
                if str(h.get("version")) == str(BRIDGE_VERSION) and not owner:
                    print("живой мост той же версии занял порт — выходим")
                    raise SystemExit(0)
                _quit_existing(port)   # владелец/другая версия — просим уступить
            else:
                # порт держит НЕотвечающий процесс: гасим ТОЛЬКО если это наш залипший мост
                oldpid = _read_pidfile(pidfile)
                if oldpid and oldpid != os.getpid() and _pid_is_our_bridge(oldpid):
                    try:
                        os.kill(oldpid, signal.SIGTERM)
                    except Exception:
                        pass
            time.sleep(0.5)
    if srv is None:
        # порт держит чужой процесс — НЕ уходим в exit(1) (иначе launchd зациклит респавн)
        print("порт %d занят чужим процессом — выходим без респавн-петли" % port)
        raise SystemExit(0)

    _START_TS = time.time()
    pidfile.write_text(json.dumps({"pid": os.getpid(), "marker": "extella-wizard-bridge", "start_ts": _START_TS}))
    # чистое завершение по сигналу → exit 0 → launchd (SuccessfulExit:false) НЕ респавнит намеренную остановку
    def _graceful(*_):
        threading.Thread(target=srv.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)

    # периодический авто-чек обновлений: только владелец (launchd), если auto_update включён.
    # Дёргает СВОЙ же /x/update_apply (там подпись+smoke+owner-gate+mutex+откат) — без дублирования логики.
    if owner and bool(CONFIG.get("auto_update", True)):
        _au_interval = int(CONFIG.get("auto_update_interval_s", 21600))   # по умолчанию 6ч

        def _auto_update_loop():
            while True:
                time.sleep(max(60, _au_interval))
                try:
                    urllib.request.urlopen(urllib.request.Request(
                        "http://127.0.0.1:%d/x/update_apply" % port, data=b"{}",
                        headers={"Content-Type": "application/json"}, method="POST"), timeout=180)
                except Exception:
                    pass
        threading.Thread(target=_auto_update_loop, daemon=True).start()

    print("Extella Adoption Wizard bridge v%s on http://127.0.0.1:%d (owner=%s)" % (BRIDGE_VERSION, port, owner))
    srv.serve_forever()
