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
import hashlib
import json
import os
import re
import threading
import time
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
# Фаза 1, шов #1: платформенный слой вынесен в wz_platform.py (тот же каталог, деплоится рядом)
from wz_platform import CONFIG, BASE, HEADERS, _scrub, api, parse_expert_result, run_expert, qwen_agent, qwen_agents


# Фаза 1, шов #2: LLM/агент-роутинг (run_llm_expert/_gen_identity/design_agent) вынесен в wz_llm.py
from wz_llm import run_llm_expert, _gen_identity, design_agent
SESS_DIR = Path.home() / "extella_wizard" / "sessions"
RUNS_DIR = Path.home() / "extella_wizard" / "runs"
_CAT_DIR = Path.home() / "extella_wizard" / "catalog"


def _catalog_usable(path):
    """Каталог должен быть не просто JSON-файлом, а рабочим контрактом Строителя."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        caps = doc.get("capabilities") if isinstance(doc, dict) else None
        archetypes = doc.get("process_archetypes") if isinstance(doc, dict) else None
        return (isinstance(caps, list) and bool(caps)
                and all(isinstance(x, dict) and str(x.get("id", "")).strip() for x in caps)
                and isinstance(archetypes, list) and bool(archetypes)
                and all(isinstance(x, dict) and str(x.get("id", "")).strip() for x in archetypes))
    except Exception:
        return False


def _ensure_catalog_path():
    """Найти каталог возможностей и восстановить canonical-копию из app bundle.

    Чистые Mac раньше получали только ui/*.py: план падал с catalog_v1.json not found.
    Установщик теперь кладёт две локальные копии; app/catalog.json служит резервом, если
    пользователь случайно удалил ~/extella_wizard/catalog/catalog.json.
    """
    canonical = _CAT_DIR / "catalog.json"
    legacy = _CAT_DIR / "catalog_v1.json"
    if _catalog_usable(canonical):
        return canonical
    bundled = APP_DIR / "catalog.json"
    # Резерв из текущего релиза важнее legacy: Помощник/старая установка могли оставить
    # синтаксически валидный, но неполный catalog_v1.json (на тесте — 203 байта без архетипов).
    source = bundled if _catalog_usable(bundled) else legacy if _catalog_usable(legacy) else None
    if source:
        try:
            import shutil as _shutil
            _CAT_DIR.mkdir(parents=True, exist_ok=True)
            _shutil.copy2(source, canonical)
            return canonical
        except Exception:
            return source
    return canonical


def _blueprint_doc_usable(doc):
    """Не пропускать формальный success без сохранённого полноценного плана."""
    if not isinstance(doc, dict) or not isinstance(doc.get("blueprint"), dict):
        return False
    bp = doc["blueprint"]
    stages = bp.get("stages")
    suitability = bp.get("suitability")
    if not str(bp.get("process_name", "")).strip() or not isinstance(stages, list) or not stages:
        return False
    if not all(isinstance(x, dict) and (x.get("id") or x.get("title")) for x in stages):
        return False
    if not isinstance(suitability, dict):
        return False
    try:
        score = float(suitability.get("score"))
    except (TypeError, ValueError):
        return False
    return 0 <= score <= 100


CATALOG_PATH = _ensure_catalog_path()
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
FILE_CHUNK = 8000            # размер чанка base64 в KV (крупные значения KV нестабильны → чанкуем)
HOST_TARGET = "85800354-f7b7-449f-b526-9357cd91f780"  # managed-хостинг VPS (PS.kz) — куда пиннить процессы 24/7
SCHED_INDEX_KEY = "sched:__index__"  # индекс активных расписаний (список sid) — тик читает его вместо прохода по всему KV
INBOUND_INDEX_KEY = "inbound:__index__"  # индекс процессов с включённым приёмом входящих (B2) — тик читает его
BRIDGE_VERSION = "5.04"       # версия моста; /x/health отдаёт её, single-instance по ней решает «свежий/старый»
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
# HEADERS, qwen_agent — в wz_platform.py (Фаза 1, шов #1)


def _bridge_csrf_token():
    """#12 double-submit CSRF-токен: секрет моста, доступен ТОЛЬКО своему UI (GET /x/csrf; чужая
    localhost-страница ответ не прочитает через SOP — CORS-заголовков мост не шлёт). Персистим в файл,
    чтобы токен пережил рестарт (иначе кэш UI протухал бы после апдейта моста)."""
    import secrets as _sec
    p = APP_DIR / ".csrf_token"
    try:
        t = p.read_text(encoding="utf-8").strip()
        if t:
            return t
    except Exception:
        pass
    t = _sec.token_urlsafe(24)
    try:
        _um = os.umask(0o077)
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            try:
                os.write(fd, t.encode("utf-8"))
            finally:
                os.close(fd)
        finally:
            os.umask(_um)
    except Exception:
        pass
    return t


BRIDGE_CSRF = _bridge_csrf_token()


# _gen_identity, design_agent — в wz_llm.py (Фаза 1, шов #2)


# _scrub, api — в wz_platform.py (Фаза 1, шов #1)


# ── Память разговора: платформа НЕ держит контекст (previous_response_id/conversation_id
#    игнорируются — проверено; в api.extella.ai нет эндпоинта «продолжить чат»). Поэтому
#    стенограмму каждой сессии храним САМИ (сайдкар <sid>_chat.json) и подаём агенту целиком. ──
def _chat_file(sid):
    return SESS_DIR / (str(sid) + "_chat.json")

def _chat_load(sid):
    if not sid or not SAFE_ID.match(str(sid)):
        return []
    f = _chat_file(sid)
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8")).get("turns", []) or []
    except Exception:
        return []

def _chat_add_exchange(sid, user_text, assistant_text):
    """Дописать пару реплик (клиент+помощник); держим хвост (последние 20 обменов), запись атомарна."""
    if not sid or not SAFE_ID.match(str(sid)):
        return
    turns = _chat_load(sid)
    turns.append({"role": "user", "text": str(user_text)[:8000]})
    turns.append({"role": "assistant", "text": str(assistant_text)[:8000]})
    turns = turns[-40:]
    try:
        import os as _os
        f = _chat_file(sid)
        tmp = Path(str(f) + ".tmp")
        tmp.write_text(json.dumps({"turns": turns}, ensure_ascii=False), encoding="utf-8")
        _os.replace(str(tmp), str(f))
    except Exception:
        pass


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


REPORTS_DIR = Path.home() / "extella_wizard" / "reports"
_REPORT_KEYS = ("report_xlsx", "report_pdf", "report_docx", "report_pptx", "report_md")


def _persist_run_reports(sid, res):
    """Отчёт прогона должен ПЕРЕЖИТЬ /tmp. Оркестратор рендерит .md/.xlsx в /tmp/<ns>_run — а его
    macOS чистит и перезаписывает следующим прогоном, поэтому через часы «Скачать отчёт» ловит
    «файл недоступен» (Гульжан, 20.07). Копируем каждый существующий артефакт в ДОЛГОВЕЧНУЮ папку
    ~/extella_wizard/reports/<sid>/ (разрешённый корень скачивания, не чистится), переписываем пути
    в res на копии и best-effort синкаем в общий стор — чтобы отчёт можно было забрать и с другого
    устройства (расписание на VPS). Меняет res на месте; ошибки глушим — доставка важнее."""
    if not isinstance(res, dict) or not sid:
        return res
    try:
        dst = REPORTS_DIR / _ns(str(sid))
        dst.mkdir(parents=True, exist_ok=True)
    except Exception:
        return res
    for k in _REPORT_KEYS:
        p = res.get(k)
        if not (isinstance(p, str) and p.startswith("/")):
            continue
        try:
            src = Path(p)
            if not (src.is_file() and src.stat().st_size > 0):
                continue
            tgt = dst / src.name
            if str(src.resolve()) != str(tgt.resolve()):
                import shutil as _sh
                _sh.copy2(str(src), str(tgt))
            res[k] = str(tgt)
            try:
                _sync_file_to_store(sid, str(tgt))   # для скачивания с другого устройства
            except Exception:
                pass
        except Exception:
            continue
    return res


def _materialize_from_store(sid, basename, dest_dir):
    """Обратно к _sync_file_to_store: собрать файл из чанков общего стора на ЭТОМ устройстве.
    Нужно, когда отчёт сформирован на другом устройстве (прогон по расписанию на VPS), а скачать
    хотят локально. Возвращает путь к материализованному файлу или None."""
    try:
        base = _file_key(sid, basename)
        meta = json.loads((api("/api/kv/get", {"key": base + ":meta"}) or {}).get("value") or "{}")
        n = int(meta.get("chunks", 0) or 0)
        if n <= 0:
            return None
        payload = ""
        for i in range(n):
            payload += (api("/api/kv/get", {"key": base + ":" + str(i)}) or {}).get("value") or ""
        if not payload:
            return None
        import base64 as _b64
        if meta.get("enc") is True:
            raw = _vault_fernet(allow_create=False).decrypt(payload.encode())
        else:
            raw = _b64.b64decode(payload)
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        outp = Path(dest_dir) / (meta.get("name") or basename)
        outp.write_bytes(raw)
        return str(outp)
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
    _secidx_mark(client, connector, set_at=datetime.now(timezone.utc).isoformat(), dest=_dest_hint(connector, value))
    return True


def _load_client_secret(client, connector):
    """Прочитать и расшифровать секрет коннектора (конверт {c,k,v}). None — нет секрета или
    расшифровать нечем. Значение НИКОГДА не отдаём наружу: используется только внутри моста."""
    g = api("/api/kv/get", {"key": _secret_kvkey(client, connector)})
    if not (isinstance(g, dict) and g.get("value")):
        return None
    try:
        env = json.loads(_vault_fernet().decrypt(str(g["value"]).encode("utf-8")).decode("utf-8"))
    except Exception:
        return None
    if env.get("k") != connector or env.get("c") != client:
        return None
    v = env.get("v")
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return {"raw": v}
    return v if isinstance(v, dict) else None


def _secret_kvkey(client, connector):
    """Namespace секрета в общем сторе: sec:<client>:<connector> (в KV — только шифротекст)."""
    return "sec:" + _ns(client) + ":" + _ns(connector)


def _secidx_key(client):
    """Индекс подключённых коннекторов клиента (одна KV-запись — листинг без скана всего стора)."""
    return "secidx:" + _ns(client)


def _secidx_entry(v):
    """Нормализует запись индекса: legacy-строка (только set_at) → dict {set_at,validated_at,last_ok,last_err}.
    #10/#15: карточка подключения показывает честный статус проверки, а не «записано==подключено»."""
    if isinstance(v, dict):
        return {"set_at": v.get("set_at"), "validated_at": v.get("validated_at"),
                "last_ok": v.get("last_ok"), "last_err": v.get("last_err"), "dest": v.get("dest")}
    return {"set_at": v if isinstance(v, str) else None, "validated_at": None, "last_ok": None, "last_err": None, "dest": None}


def _dest_hint(connector, value):
    """#2 несекретный «куда шлём» из значения секрета (чтобы в UI не было «Telegram — а кому?»).
    chat_id/адрес получателя — не credential; показываем только владельцу через локальный мост."""
    try:
        v = json.loads(value)
    except Exception:
        return None
    if not isinstance(v, dict):
        return None
    if connector == "telegram":
        return (str(v.get("chat_id") or "").strip() or None)
    if connector == "email":
        return (str(v.get("to") or v.get("from") or "").strip() or None)
    if connector in ("whatsapp", "sms"):
        return (str(v.get("to") or "").strip() or None)
    if connector == "slack":
        return "hooks.slack.com/…" if v.get("webhook_url") else None
    return None


def _secidx_mark(client, connector, set_at=None, validated_ok=None, err=None, dest=None):
    """Обновляет запись индекса: set_at — при записи секрета; validated_at/last_ok/last_err — при тесте/отправке;
    dest — несекретный адрес получателя. Терпит legacy-строки. НЕ создаёт запись для несуществующего коннектора."""
    if not connector:
        return
    try:
        idx = json.loads((api("/api/kv/get", {"key": _secidx_key(client)}) or {}).get("value") or "{}")
    except Exception:
        idx = {}
    if connector not in idx and set_at is None:
        return
    e = _secidx_entry(idx.get(connector))
    if set_at is not None:
        e["set_at"] = set_at
    if validated_ok is not None:
        e["validated_at"] = datetime.now(timezone.utc).isoformat()
        e["last_ok"] = bool(validated_ok)
        e["last_err"] = (str(err)[:120] if (err and not validated_ok) else None)
    if dest is not None:
        e["dest"] = dest
    idx[connector] = e
    api("/api/kv/set", {"key": _secidx_key(client), "value": json.dumps(idx, ensure_ascii=False), "description": "secidx"})


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
        # #3 гонка прав: write_bytes создаёт файл с umask-правами (обычно world-readable) ДО chmod —
        # окно, где ключ читаем другими локальными юзерами. Создаём атомарно сразу с 0600 (O_EXCL — не
        # перезаписать чужой файл в гонке); umask на всякий случай сузим на время открытия.
        _key = Fernet.generate_key()
        _um = _os.umask(0o077)
        try:
            _fd = _os.open(str(kp), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY, 0o600)
            try:
                _os.write(_fd, _key)
            finally:
                _os.close(_fd)
        finally:
            _os.umask(_um)
        try:
            _os.chmod(kp, 0o600)   # если umask/файловая система проигнорировали mode при open
        except Exception:
            pass
    return Fernet(kp.read_bytes())


# parse_expert_result, run_expert — в wz_platform.py (Фаза 1, шов #1)


def _load_expert_fn(name):
    """Тянет код зарегистрированного эксперта (/api/expert/get) и exec'ит его ЛОКАЛЬНО в процессе моста.
    Нужно для publish: git/gh/файлы живут на устройстве Анвара (тут крутится server.py), а не на VPS HOST_TARGET."""
    r = api("/api/expert/get", {"name": name, "global": True})
    code = (r or {}).get("expert_code") or ""
    if not code:
        raise RuntimeError("expert code empty: " + name)
    ns = {}
    exec(compile(code, name + ".py", "exec"), ns)
    fn = ns.get(name)
    if not callable(fn):
        raise RuntimeError("no callable " + name)
    return fn
    return parse_expert_result(res)


DEFAULT_MSG_TEMPLATE = "✅ Extella: {name} — процесс отработал.\nПозиций: {count}\nСумма: {sum} ₸"


def _render_msg(template, name, count, total):
    """Собрать текст доставки из шаблона автоматизации (кабина «Шаблон сообщения»). Плейсхолдеры:
    {name} — имя процесса, {count} — число позиций, {sum} — сумма (с разрядкой), {date} — дата-время UTC.
    Пустой шаблон → дефолт. Отсутствующие значения → «—» (без битых плейсхолдеров в сообщении)."""
    def _fnum(x):
        return format(x, ",").replace(",", " ") if isinstance(x, (int, float)) else "—"
    t = template if (isinstance(template, str) and template.strip()) else DEFAULT_MSG_TEMPLATE
    return (t.replace("{name}", str(name or "процесс"))
             .replace("{count}", str(count) if count is not None else "—")
             .replace("{sum}", _fnum(total) if total is not None else "—")
             .replace("{date}", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")))


_SESS_LOCKS = {}
_SESS_LOCKS_GUARD = threading.Lock()


def _sess_lock(sid):
    """Пер-сессионный лок: сериализует read-modify-write файла сессии (ThreadingHTTPServer)."""
    with _SESS_LOCKS_GUARD:
        lk = _SESS_LOCKS.get(sid)
        if lk is None:
            lk = _SESS_LOCKS[sid] = threading.Lock()
        return lk


def _update_session(sid, mutate):
    """Атомарная правка сессии под пер-сид локом: read→mutate(s)→write. Закрывает lost-update при
    конкурентных коротких запросах. Для ДОЛГИХ операций (прогон, до минут) НЕ держать лок —
    там re-read сессии прямо перед записью и merge (append), чтобы не блокировать правки владельца."""
    sp = SESS_DIR / (sid + ".json")
    with _sess_lock(sid):
        _lf = None
        try:
            import fcntl as _fcntl   # #16: + МЕЖПРОЦЕССНЫЙ flock — координация с wz_session (листенер) на том же файле сессии
            _lf = open(str(sp) + ".lock", "w")
            _fcntl.flock(_lf, _fcntl.LOCK_EX)
        except Exception:
            _lf = None
        try:
            s = json.loads(sp.read_text(encoding="utf-8"))
            mutate(s)
            s["updated_at"] = datetime.now(timezone.utc).isoformat()
            sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
            return s
        finally:
            if _lf is not None:
                try:
                    _fcntl.flock(_lf, _fcntl.LOCK_UN)
                    _lf.close()
                except Exception:
                    pass


def _human_missing(m):
    """Разбор технической строки «чего не хватило» Композитора в человеческую (без in_dir/{{}}/id)."""
    s = str(m)
    # шаг X: нужен существующий Y  → «шагу «X» нужен вход: <человеческое Y>»
    mo = re.match(r"шаг\s+(\S+):\s*нужен существующий\s+(\S+)", s)
    if mo:
        step, param = mo.group(1), mo.group(2)
        human = {"in_dir": "папка с входными файлами", "out_dir": "папка для результата",
                 "path": "путь к файлу", "file": "файл", "source": "источник данных"}.get(param, param)
        return "шагу «%s» нужен вход: %s — укажите его словами (напр. «файлы в ~/Папка») или уберите шаг" % (step, human)
    mo = re.match(r"шаг\s+(\S+)\s+ссылается на\s+\{\{(\S+?)\}\}", s)
    if mo:
        return "шаг «%s» ждёт данные «%s» с предыдущего шага — их нет; уточните порядок словами" % (mo.group(1), mo.group(2))
    mo = re.match(r"unknown block:\s*(.+)", s)
    if mo:
        return "блок «%s» не в вашей библиотеке — доустановите или достройте Мастером" % mo.group(1).strip()
    if "не установилась" in s:
        return re.sub(r"локальная модель не установилась:\s*", "не удалось поставить модель: ", s)
    if "params не объект" in s:
        return None   # тех. деталь — не показываем пользователю
    # английские фразы-пробелы (delivery block, web search API…) — оставляем как есть
    return s


def _task_lang(s):
    """Язык задачи для Композитора: платформенный Qwen по умолчанию тянет в русский,
    поэтому для англ. ввода (US-демо) мосту нужно ЯВНО задать язык вывода карточки."""
    s = str(s or "")
    cyr = len(re.findall(r"[А-Яа-яЁё]", s))
    lat = len(re.findall(r"[A-Za-z]", s))
    return "en" if lat > cyr else "ru"


def _compose_directive(lang):
    """Явная директива языка вывода для wz_auto_compose (name/description карточки).
    Решение Анвара 16.07: язык следует за ПОСЛЕДНЕЙ репликой пользователя, поэтому директива
    нужна ЯВНАЯ в обе стороны (иначе Qwen тянет язык исходной задачи/своего сис-промпта)."""
    if lang == "ru":
        return "\n\nОтвечай по-русски: имя (name) и описание (description) автоматизации ДОЛЖНЫ быть на русском языке."
    return "\n\nRespond in English: the automation name and description MUST be in English."


def _runs_unified(s, skv):
    """F3: ЕДИНАЯ история прогонов процесса. Ручные живут в s.runs (мост), по расписанию — в
    sched:<sid>.runs (тик). Раньше три читателя склеивали по-своему и без dedup. Контракт записи:
    {at, status, trigger: manual|schedule|inbound, ...counts}. Старые записи без trigger = manual."""
    merged = (s.get("runs") or []) + ((skv or {}).get("runs") or [])
    seen, out = set(), []
    for r in merged:
        if not isinstance(r, dict):
            continue
        k = str(r.get("at", ""))[:19]
        if k in seen:
            continue
        seen.add(k)
        if not r.get("trigger"):
            r = dict(r); r["trigger"] = "manual"
        out.append(r)
    out.sort(key=lambda r: r.get("at", ""))
    return out


def _janitor_orphan_bindings():
    """Джанитор обвязки при старте моста: сирота = KV-привязка (inbound/sched) без живой сессии
    (сессия удалена/в архиве). Класс бага: автоматизация «ИИ-анализ договоров» была заархивирована
    ДО фикса C1 «удаление снимает входящие» — тик неделю опрашивал Telegram впустую. Гасим той же
    тройкой, что штатный delete: hookmap → inbound:<sid> → индекс; для расписаний — sched:<sid> → индекс.
    Возвращает число вычищенных сирот."""
    cleaned = 0
    try:
        live = {p.stem for p in SESS_DIR.glob("wz_*.json")
                if not p.name.endswith(("_blueprint.json", "_build_plan.json", "_chat.json", "_build_manifest.json"))}
        # входящие
        try:
            g = api("/api/kv/get", {"key": "inbound:__index__"})
            sids = (json.loads(g.get("value") or "{}") or {}).get("sids") or []
            keep = []
            for sid in sids:
                if sid in live:
                    keep.append(sid)
                    continue
                try:
                    ig = api("/api/kv/get", {"key": "inbound:" + sid})
                    icj = json.loads(ig.get("value") or "{}")
                    if icj.get("route_token"):
                        api("/api/kv/remove", {"key": "hookmap:" + str(icj["route_token"])})
                    api("/api/kv/remove", {"key": "inbound:" + sid})
                except Exception:
                    pass
                cleaned += 1
                print("janitor: осиротевший inbound снят:", sid)
            if len(keep) != len(sids):
                api("/api/kv/set", {"key": "inbound:__index__", "value": json.dumps({"sids": keep}),
                                    "description": "inbound index"})
        except Exception:
            pass
        # расписания (легаси-ключи вида sched:ci без сессии не трогаем — только wz_-сироты)
        try:
            g = api("/api/kv/get", {"key": "sched:__index__"})
            sids = (json.loads(g.get("value") or "{}") or {}).get("sids") or []
            keep = []
            for sid in sids:
                if (sid in live) or not str(sid).startswith("wz_"):
                    keep.append(sid)
                    continue
                try:
                    api("/api/kv/remove", {"key": "sched:" + sid})
                except Exception:
                    pass
                cleaned += 1
                print("janitor: осиротевшее расписание снято:", sid)
            if len(keep) != len(sids):
                api("/api/kv/set", {"key": "sched:__index__", "value": json.dumps({"sids": keep}),
                                    "description": "schedule index"})
        except Exception:
            pass
    except Exception:
        pass
    return cleaned


def _unstick_sessions():
    """Снять флаг «идёт стройка» с сессий, чья стройка на самом деле давно кончилась.

    Зачем отдельно от _recover_orphan_builds: тот чинит сессию ТОЛЬКО в момент перехода
    running → orphaned, и запись обёрнута в молчаливый except. Одна неудачная запись —
    и процесс заперт НАВСЕГДА: статус уже orphaned, условие больше не сработает, ни один
    следующий перезапуск не поможет. Владелец при этом видит вечный спиннер «стройка идёт».
    Поэтому лечение должно быть идемпотентным: смотрим на ФАКТ (жива ли стройка), а не на
    момент перехода. 18.07: так заперся процесс Анвара после того, как я убил стройку деплоем."""
    n = 0
    try:
        for sp in SESS_DIR.glob("wz_*.json"):
            if sp.name.endswith(("_blueprint.json", "_build_plan.json", "_chat.json",
                                 "_spec.json", "_build_manifest.json")):
                continue
            try:
                s = json.loads(sp.read_text(encoding="utf-8"))
            except Exception:
                continue
            bid = str(s.get("building") or "")
            if not bid or not SAFE_ID.match(bid):
                continue
            bp = RUNS_DIR / bid / "build_progress.json"
            alive = False
            if bp.exists():
                try:
                    alive = json.loads(bp.read_text(encoding="utf-8")).get("status") == "running"
                except Exception:
                    alive = False
            if alive:
                continue
            sid = str(s.get("session_id") or sp.stem)
            try:
                _update_session(sid, lambda sx: sx.pop("building", None))
                n += 1
            except Exception:
                pass
    except Exception:
        pass
    return n


def _recover_orphan_builds():
    """F1 (фундамент): мост перезапустился — треды строек мертвы. Любой build_progress со status=running
    в этот момент — сирота: (1) честно пометить orphaned (UI перестанет ждать и скажет «повторите»),
    (2) разблокировать сессию (снять building — иначе C6-гард запер бы её навсегда),
    (3) вернуть авто-паузнутое на стройку расписание (resume_sched из журнала работы)."""
    n = 0
    try:
        for bp in RUNS_DIR.glob("build_*/build_progress.json"):
            try:
                prog = json.loads(bp.read_text(encoding="utf-8"))
            except Exception:
                continue
            if prog.get("status") != "running":
                continue
            prog["status"] = "orphaned"
            prog["orphaned_at"] = datetime.now(timezone.utc).isoformat()
            prog["orphan_reason"] = "мост перезапущен во время стройки — повторите правку"
            try:
                bp.write_text(json.dumps(prog, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                continue
            sid = str(prog.get("session_id") or "")
            if sid and SAFE_ID.match(sid) and (SESS_DIR / (sid + ".json")).exists():
                try:
                    _update_session(sid, lambda sx: sx.pop("building", None))
                except Exception:
                    pass
                if prog.get("resume_sched"):
                    try:
                        gv = api("/api/kv/get", {"key": "sched:" + sid})
                        cv = gv.get("value") if isinstance(gv, dict) else None
                        if cv:
                            cfg = json.loads(cv)
                            if not cfg.get("active", True):
                                cfg["active"] = True
                                _iv = int(cfg.get("interval_min", 0) or 0)
                                if _iv:
                                    cfg["next_due_ts"] = (datetime.now(timezone.utc) + timedelta(minutes=_iv)).isoformat()
                                api("/api/kv/set", {"key": "sched:" + sid, "value": json.dumps(cfg, ensure_ascii=False),
                                                    "description": "schedule " + sid})
                    except Exception:
                        pass
            n += 1
    except Exception:
        pass
    return n


def _run_digest(res):
    """Единый человекочитаемый отчёт прогона (markdown) для виджета «Последний результат»,
    кнопки «Открыть отчёт» и расписания. Приоритет: готовый digest в ответе → содержимое
    локального файла report_md → синтез из summary (total_count/total_sum/by_*).
    Чинит пробел: ветка процессов-на-файле digest не сохраняла — результат «пропадал» из UI."""
    if not isinstance(res, dict):
        return ""
    dm = res.get("digest_md") or res.get("digest")
    if isinstance(dm, str) and dm.strip() and not dm.strip().startswith("/"):
        return dm[:12000]
    rp = res.get("report_md")   # оркестраторы-на-файле кладут сюда ПУТЬ к report.md, не текст
    if isinstance(rp, str) and rp.startswith("/"):
        try:
            p = Path(rp)
            if p.is_file() and p.stat().st_size < 400000:
                txt = p.read_text(encoding="utf-8", errors="replace").strip()
                if txt:
                    return txt[:12000]
        except Exception:
            pass
    summ = res.get("summary")
    if isinstance(summ, str) and summ.strip():
        return summ[:12000]
    if isinstance(summ, dict):
        out = ["## Результат прогона", ""]
        tc, ts = summ.get("total_count"), summ.get("total_sum")
        if tc is not None:
            line = "**Позиций:** %s" % tc
            if ts not in (None, 0, 0.0):
                line += "  ·  **Сумма:** %s" % ts
            out += [line, ""]
        for k, v in summ.items():
            if k.startswith("by_") and isinstance(v, dict) and 0 < len(v) <= 25:
                lbl = k[3:].replace("_", " ")
                out.append("### " + lbl.capitalize())
                out += ["| %s | Кол-во |" % lbl, "|---|---|"]
                out += ["| %s | %s |" % (kk, vv) for kk, vv in list(v.items())[:25]]
                out.append("")
        body = "\n".join(out).strip()
        if body and body != "## Результат прогона":
            return body[:12000]
    return ""


def _web_junk_reason(res):
    """РАНТАЙМ-детектор «мусорного веб-обогащения». Сгенерённый шаг процесса полез в веб-поиск по
    ЧИСЛАМ/суммам/заголовкам колонок и вернул результаты не по теме (словарь VINDICATE, вход в Zoom,
    доставка цветов — Гульжан, 20.07). Новый смысловой гейт `_stage_sanity` блокирует публикацию
    такого шага на СБОРКЕ; этот рантайм-детектор остаётся защитой для уже собранных старых процессов,
    которые иначе могли выдать «success · 60 000 000» поверх мусора. Смотрим в готовый
    отчёт (report_md/xlsx — мусор живёт в теле отчёта, не в сжатой сводке res) и, если видим
    сигнатуру веб-результата с числовым search_query, возвращаем причину словами. Пусто = чисто.

    Дёшево и консервативно: сначала текстовый report_md, иначе строковые ячейки xlsx (read_only,
    с потолком). Ложное срабатывание маловероятно — нужна И сигнатура веб-выдачи, И ≥2 числовых
    запроса."""
    if not isinstance(res, dict):
        return ""
    texts = []
    rp = res.get("report_md")
    if isinstance(rp, str) and rp.startswith("/"):
        try:
            p = Path(rp)
            if p.is_file() and p.stat().st_size < 2_000_000:
                texts.append(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            pass
    xp = res.get("report_xlsx")
    if not texts and isinstance(xp, str) and xp.startswith("/"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(xp, read_only=True, data_only=True)
            buf = []
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    for c in row:
                        if isinstance(c, str) and len(c) > 20:
                            buf.append(c)
                    if len(buf) > 400:
                        break
                if len(buf) > 400:
                    break
            wb.close()
            texts.append("\n".join(buf))
        except Exception:
            pass
    blob = "\n".join(texts)
    if not blob or not any(m in blob for m in ("'source':", '"source":', "'snippet':", '"snippet":')):
        return ""   # нет сигнатуры веб-выдачи — не наш случай
    numq = len(re.findall(r"""['"]search_query['"]\s*:\s*['"]\s*[\d.,\s%+-]+['"]""", blob))
    if numq >= 2:
        return ("шаг веб-обогащения искал по числам/суммам, а не по названиям — найденное не относится "
                "к задаче (словари, случайные сайты). Уберите или поправьте этот шаг в чате доводки.")
    return ""


def _save_digest(sid, digest):
    """C3: последний дайджест прогона → KV digest:<sid> (перезапись; читает виджет «Последний
    результат» кабинета). Раньше дайджест жил только в HTTP-ответе и терялся с закрытием модалки."""
    if not digest or not sid:
        return
    try:
        api("/api/kv/set", {"key": "digest:" + sid,
                            "value": json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                                                 "digest": str(digest)[:12000]}, ensure_ascii=False),
                            "description": "last digest"})
    except Exception:
        pass


def _record_blocked(sid, code, message, trigger="manual"):
    """A3 Инбокс исключений: ЗАБЛОКИРОВАННЫЙ прогон (дрифт источника/устройства, преflight) обязан
    оставить след в истории — иначе оператор видит тишину и думает, что всё хорошо. Пишем run-record
    со статусом blocked; монитор красит процесс и показывает причину словами в инбоксе."""
    if not sid:
        return
    rec = {"trigger": trigger, "at": datetime.now(timezone.utc).isoformat(),
           "status": "blocked", "blocked_code": code, "needs_review_reason": str(message)[:200]}
    try:
        _update_session(sid, lambda s: s.setdefault("runs", []).append(rec))
    except Exception:
        pass
    return rec


def _run_expert_resilient(expert, params, wait=240, glob=True, tries=3):
    """Надёжный прогон оркестратора: ТРАНЗИЕНТНЫЙ сбой (нет result / HTTP 5xx / timeout / пустой синтез /
    обрыв связи) — не провал, а повод ПЕРЕИГРАТЬ (канон: 500/timeout ≠ провал). Ретраим с нарастающей
    паузой; ДЕТЕРМИНИРОВАННЫЙ отказ (нет оркестратора, битые параметры) не ретраим. Возвращает (res, attempts)."""
    last = None
    tries = max(1, int(tries or 1))
    params = dict(params or {})
    for attempt in range(1, tries + 1):
        res = run_expert(expert, params, wait=wait, glob=glob)
        last = res
        if isinstance(res, dict) and str(res.get("status", "")) in ("success", "partial"):
            return res, attempt
        # A2 ЧЕКПОИНТЫ: оркестратор упал на стадии и умеет продолжить → следующая попытка идёт с тем же
        # run_id и переигрывает ТОЛЬКО с упавшей стадии (пройденные в ЭТОМ прогоне не повторяем)
        if isinstance(res, dict) and res.get("resumable") and res.get("run_id"):
            params["run_id"] = res["run_id"]
        blob = (json.dumps(res, ensure_ascii=False) if isinstance(res, dict) else str(res))[:500].lower()
        transient = (not isinstance(res, dict)) or (not res.get("status")) or any(
            k in blob for k in ("timeout", "timed out", " 500", " 502", " 503", " 504",
                                "temporar", "empty", "none", "connection", "reset", "unavailable"))
        if not transient or attempt >= tries:
            return res, attempt
        time.sleep(min(2 * attempt, 8))   # backoff: 2s, 4s, 6s… (ограничен 8с)
    return last, tries


def _deliver_once(key):
    """Идемпотентность доставки: не слать один и тот же логический результат дважды (защита от повторного
    тика/ретрая). KV check-and-set по dedup-ключу. True — первый раз (слать), False — уже доставлено.
    При сбое дедупа возвращаем True (лучше доставить, чем молча потерять отчёт)."""
    kk = "delivered:" + _ns(str(key))
    try:
        g = api("/api/kv/get", {"key": kk}, 15)
        if isinstance(g, dict) and g.get("value"):
            return False
        api("/api/kv/set", {"key": kk, "value": json.dumps({"at": datetime.now(timezone.utc).isoformat()},
                            ensure_ascii=False), "description": "delivery dedup"}, 15)
        return True
    except Exception:
        return True


def _artifact_check(path):
    """Render-check артефакта: файл, который увидит клиент, ДЕЙСТВИТЕЛЬНО открывается (не 0 байт, не битый).
    OOXML (xlsx/docx/pptx) = валидный zip с [Content_Types].xml + профильной частью; pdf = %PDF…%%EOF;
    json — парсится; текст/csv/md — непустой. Возвращает {ok, kind, reason, bytes}."""
    import zipfile
    try:
        p = Path(path)
        if not p.is_file():
            return {"ok": False, "kind": "?", "reason": "файла нет", "bytes": 0}
        size = p.stat().st_size
        ext = p.suffix.lower().lstrip(".")
        if size == 0:
            return {"ok": False, "kind": ext, "reason": "пустой файл (0 байт)", "bytes": 0}
        if ext in ("xlsx", "docx", "pptx"):
            if not zipfile.is_zipfile(str(p)):
                return {"ok": False, "kind": ext, "reason": "не распаковывается (битый OOXML/обрезан)", "bytes": size}
            with zipfile.ZipFile(str(p)) as z:
                names = set(z.namelist())
                need = {"xlsx": "xl/workbook.xml", "docx": "word/document.xml", "pptx": "ppt/presentation.xml"}[ext]
                if "[Content_Types].xml" not in names or need not in names:
                    return {"ok": False, "kind": ext, "reason": "нет обязательных частей документа", "bytes": size}
                if z.testzip() is not None:
                    return {"ok": False, "kind": ext, "reason": "внутренняя часть архива повреждена (CRC)", "bytes": size}
            return {"ok": True, "kind": ext, "reason": "", "bytes": size}
        if ext == "pdf":
            head = p.open("rb").read(1024)
            tail = b""
            with p.open("rb") as f:
                f.seek(max(0, size - 2048))
                tail = f.read()
            if not head.startswith(b"%PDF-"):
                return {"ok": False, "kind": ext, "reason": "нет сигнатуры %PDF (не PDF/битый)", "bytes": size}
            if b"%%EOF" not in tail:
                return {"ok": False, "kind": ext, "reason": "нет маркера конца %%EOF (файл обрезан)", "bytes": size}
            return {"ok": True, "kind": ext, "reason": "", "bytes": size}
        if ext == "json":
            try:
                json.loads(p.read_text(encoding="utf-8"))
                return {"ok": True, "kind": ext, "reason": "", "bytes": size}
            except Exception as e:
                return {"ok": False, "kind": ext, "reason": "JSON не парсится: " + str(e)[:80], "bytes": size}
        # csv/md/txt/прочее — непустой текст
        return {"ok": True, "kind": ext or "bin", "reason": "", "bytes": size}
    except Exception as e:
        return {"ok": False, "kind": "?", "reason": _scrub(str(e)[:100]), "bytes": 0}


def _tourvisor_days():
    """Дней до истечения JWT Tourvisor (из config.tourvisor_jwt) или None, если нет/не читается.
    Для монитора: креды с датой должны желтеть ЗАРАНЕЕ, а не молчать до отказа процесса."""
    jwt = CONFIG.get("tourvisor_jwt")
    if not jwt:
        return None
    try:
        import base64 as _b64x
        p = str(jwt).split(".")[1]
        p += "=" * (-len(p) % 4)
        exp = json.loads(_b64x.urlsafe_b64decode(p)).get("exp")
        if not exp:
            return None
        return int((datetime.fromtimestamp(int(exp), tz=timezone.utc) - datetime.now(timezone.utc)).total_seconds() // 86400)
    except Exception:
        return None


def _target_passport(slug):
    """Мультитаргет: паспорт устройства из KV (None, если не снят)."""
    try:
        g = api("/api/kv/get", {"key": "target:passport:" + str(slug)})
        return json.loads(g.get("value") or "{}") or None
    except Exception:
        return None


def _device_fingerprint(pp):
    """Отпечаток устройства из паспорта — чтобы поймать ДРИФТ (устройство изменилось с момента привязки).
    Преflight ловит только ОБЪЯВЛЕННЫЕ требования; дрифт ловит то, что не объявляли: пропало приложение
    или локальная модель, кончается диск, сменилось окружение."""
    pp = pp if isinstance(pp, dict) else {}
    return {"apps": sorted(str(a) for a in (pp.get("apps") or [])),
            "models": sorted(str(m) for m in (pp.get("ollama_models") or [])),
            "os_version": str(pp.get("os_version") or ""), "python": str(pp.get("python") or ""),
            "disk_free_gb": pp.get("disk_free_gb")}


def _device_drift(base, cur, disk_min_gb=2.0):
    """Дрифт устройства: что ИСЧЕЗЛО с момента привязки. Пропавшие приложения/модели — ЛОМАЮЩИЙ дрифт
    (процесс на них опирался, когда его привязывали); мало диска и смена окружения — мягкий сигнал."""
    if not isinstance(base, dict) or not isinstance(cur, dict):
        return {"drift": False}
    gone_apps = sorted(set(base.get("apps") or []) - set(cur.get("apps") or []))
    gone_models = sorted(set(base.get("models") or []) - set(cur.get("models") or []))
    soft = []
    try:
        free = cur.get("disk_free_gb")
        if free is not None and float(free) < float(disk_min_gb):
            soft.append("на устройстве мало места: %s ГБ" % free)
    except Exception:
        pass
    for f, human in (("os_version", "версия ОС"), ("python", "python")):
        if base.get(f) and cur.get(f) and base[f] != cur[f]:
            soft.append("%s изменилась: %s → %s" % (human, base[f], cur[f]))
    breaking = bool(gone_apps or gone_models)
    return {"drift": bool(breaking or soft), "breaking": breaking,
            "gone_apps": gone_apps, "gone_models": gone_models, "soft": soft}


_DEVICES_CACHE = {"at": 0.0, "data": None}
_TARGET_REFS = {}   # ref → настоящий target id; наружу уходит только ref (device id = доступ к устройству)


def _target_by_ref(ref):
    """Разворот непрозрачной ссылки в настоящий target (только внутри моста).
    ref = sha256(target)[:12] — детерминирован, поэтому переживает рестарт: если карта
    пуста (холодный мост), переснимаем список устройств и разворачиваем снова."""
    ref = str(ref or "")
    if not ref:
        return ""
    if ref not in _TARGET_REFS:
        try:
            _targets_live()
        except Exception:
            pass
    return _TARGET_REFS.get(ref, "")


# ─────────── A1: КАРТА РАЗМЕЩЕНИЯ (какая стадия на каком устройстве) ───────────
# До сих пор процесс целиком жил на ОДНОМ устройстве. В жизни это неверно: чтение 1С —
# только на машине с 1С, отчёт и доставка — на хостинге 24/7. Карта хранится в сессии
# ССЫЛКАМИ (ref), настоящий device id в сессию не пишется и наружу не уходит.
_PLACE_LOCAL_HINTS = ("1c", "1с", "excel", "файл", "file", "local", "локал", "папк", "outlook", "winrm", "scan")
_PLACE_HOST_HINTS = ("report", "отчет", "отчёт", "deliver", "достав", "send", "письм", "mail", "telegram", "summary", "сводк")


def _placement_stages(s):
    """Имена стадий последней сборки — по ним и строится карта."""
    lb = (s.get("builds") or [{}])[-1]
    return [str(e) for e in (lb.get("experts") or []) if isinstance(e, str)]


def _placement_stage_labels(s):
    """Техническое имя эксперта → человеческое название шага для карты размещения.

    Канон карты хранит expert_name (по нему оркестратор выбирает target), но владельцу процесса
    это имя ничего не говорит. Главный источник ярлыка — purpose/title той же задачи build-plan;
    для старых сборок остаётся безопасный читаемый фолбэк без изменения исполняемой карты.
    """
    stages = _placement_stages(s)
    labels = {}
    sid = str(s.get("session_id") or "")
    if sid and SAFE_ID.match(sid):
        try:
            doc = json.loads((SESS_DIR / (sid + "_build_plan.json")).read_text(encoding="utf-8"))
            plan = doc.get("plan", doc) if isinstance(doc, dict) else {}
            for task in (plan.get("tasks") or []):
                if not isinstance(task, dict):
                    continue
                name = str(task.get("expert_name") or "")
                label = str(task.get("title") or task.get("purpose") or "").strip().rstrip(".")
                if name in stages and label:
                    labels[name] = label[:120]
        except Exception:
            pass
    lb = (s.get("builds") or [{}])[-1]
    human = lb.get("components_human") or []
    if isinstance(human, list) and len(human) == len(stages):
        for name, label in zip(stages, human):
            if str(label).strip():
                labels.setdefault(name, str(label).strip()[:120])
    for name in stages:
        if name not in labels:
            parts = [p for p in name.split("_") if p]
            if len(parts) > 2 and len(parts[0]) <= 8:   # namespace процесса (eur_, dz_, …)
                parts = parts[1:]
            labels[name] = (" ".join(parts).strip().capitalize() or "Шаг процесса")[:120]
    return labels


def _placement_plan(s, devices=None):
    """Предложение карты: не догадка «на глазок», а объяснимое правило.
    1) local_only — весь процесс на устройстве данных (наружу данные не выпускаем);
    2) стадии чтения/локальных приложений — на устройстве данных;
    3) отчёт и доставка — на хостинге (он живёт 24/7, ноутбук закрывают);
    4) остальное — как раньше, на устройстве по умолчанию.
    Возвращает список строк {stage, ref, label, why} — ЧЕЛОВЕК подтверждает."""
    devs = devices if devices is not None else _targets_live()
    host = next((d for d in devs if d.get("is_host")), None)
    local = next((d for d in devs if d.get("is_local")), None)
    req = s.get("target_requirements") or {}
    local_only = bool(req.get("local_only"))
    out = []
    for i, name in enumerate(_placement_stages(s)):
        low = name.lower()
        if local_only and local:
            d, why = local, "данные помечены local_only — наружу не выпускаем"
        elif any(h in low for h in _PLACE_LOCAL_HINTS) and local:
            d, why = local, "стадия работает с локальными данными/приложением"
        elif any(h in low for h in _PLACE_HOST_HINTS) and host:
            d, why = host, "отчёт и доставка должны работать, когда ноутбук закрыт"
        elif i == 0 and s.get("source") and host:
            d, why = host, "источник тянется по сети — хостинг доступен всегда"
        else:
            d, why = (host or local), "устройство по умолчанию"
        if not d:
            continue
        out.append({"stage": name, "ref": d.get("ref"), "label": d.get("label"), "why": why})
    return out


def _placement_get(s):
    """Карта из сессии: {stage: ref}. Пусто = старое поведение (весь процесс на одном таргете)."""
    pl = s.get("placement") or {}
    m = pl.get("map") if isinstance(pl, dict) else None
    return m if isinstance(m, dict) else {}


def _placement_resolve(s):
    """ref → настоящий target, только для передачи оркестратору. Возвращает {stage: target}.
    Неподтверждённая карта не исполняется: пока человек не сказал «да», работает как раньше."""
    pl = s.get("placement") or {}
    if not (isinstance(pl, dict) and pl.get("confirmed")):
        return {}
    res = {}
    for stage, ref in _placement_get(s).items():
        tgt = _target_by_ref(ref)
        if tgt:
            res[str(stage)] = tgt
    return res


def _placement_preflight(s, mode="manual"):
    """Честный отказ ДО прогона: если устройство стадии оффлайн, процесс не запускаем в никуда.
    Отдельно ловим расписание на локальной машине — тик живёт на хостинге."""
    lb = (s.get("builds") or [{}])[-1]
    plan = _placement_get(s)
    if (not plan or not (s.get("placement") or {}).get("confirmed")
            or int(lb.get("placement_contract", 0) or 0) < 1):   # старая сборка карту не исполняет — и не блокируем ею прогон
        return {"ok": True}
    devs = _targets_live()
    by_ref = {d.get("ref"): d for d in devs}
    dead, offline_names = [], []
    for stage, ref in plan.items():
        d = by_ref.get(ref)
        if not d:
            dead.append(stage)
        elif not d.get("online"):
            offline_names.append(stage + " → " + str(d.get("label") or "?"))
    if dead:
        return {"ok": False, "code": "placement_unknown",
                "message": "у стадий " + ", ".join(dead[:4]) + " устройство больше не зарегистрировано — "
                           "откройте «Где исполняется» и назначьте заново"}
    if offline_names:
        return {"ok": False, "code": "placement_offline",
                "message": "устройство стадии оффлайн: " + "; ".join(offline_names[:4]) +
                           ". Включите устройство или перенесите стадию на другое."}
    return {"ok": True}


def _probe_target(tgt, timeout=25):
    """A1: ЖИВОСТЬ устройства. Платформа НЕ отдаёт онлайн-статус (в /api/targets/list только
    id/target/description) — единственный честный сигнал — попытка запуска с пиннингом: живое
    устройство отвечает, мёртвое даёт «Target ... is unavailable» за ~2с. Эксперт-проба должен быть
    сверхлёгким (wz_ping): wz_target_passport для этого не годится — делает реальную работу.
    ⚠ Проба несуществующим экспертом НЕ работает: платформа проверяет наличие эксперта ДО доступности."""
    raw = ""
    try:
        rq = urllib.request.Request(BASE + "/api/expert/run", method="POST", headers=HEADERS,
                                    data=json.dumps({"expert_name": "wz_ping", "global": True,
                                                     "target": tgt, "params": {}}).encode())
        with urllib.request.urlopen(rq, timeout=timeout) as r:
            raw = r.read().decode()
    except urllib.error.HTTPError as e:
        # платформа отдаёт «Target ... is unavailable» ТЕЛОМ ответа с кодом 500 — читаем тело,
        # иначе честный «оффлайн» выглядел бы как «HTTP Error 500» (непонятно человеку)
        try:
            raw = e.read().decode()
        except Exception:
            raw = str(e)
    except Exception as e:
        return {"online": False, "why": _scrub(str(e)[:100]), "host": ""}
    low = raw.lower()
    if "unavailable" in low or "not available" in low:
        return {"online": False, "why": "устройство не отвечает (листенер выключен или машина спит)", "host": ""}
    host = ""
    try:
        _b = json.loads(raw)
        _r = _b.get("result", _b)
        if isinstance(_r, str):
            try:
                _r = json.loads(_r)
            except Exception:
                import ast as _a
                _r = _a.literal_eval(_r)
        host = str((_r or {}).get("host") or "")
    except Exception:
        host = ""
    return {"online": True, "why": "", "host": host}


def _targets_live(force=False, ttl=120):
    """A1: устройства аккаунта = список платформы + ЖИВОСТЬ (проба) + СПОСОБНОСТИ (паспорт).
    Пробы стоят секунды, поэтому короткий кэш; force=True обновляет принудительно."""
    now_ts = time.time()
    if not force and _DEVICES_CACHE["data"] and (now_ts - _DEVICES_CACHE["at"]) < ttl:
        return _DEVICES_CACHE["data"]
    try:
        lst = api("/api/targets/list", {}, 25).get("results") or []
    except Exception:
        lst = []
    # ⚠ СПИСОК ПЛАТФОРМЫ БЫВАЕТ УСТАРЕВШИМ: target_id листенера НЕ стабилен между переустановками —
    # старая запись остаётся («Anvar's device - main Listener» = мёртвый id), а ТЕКУЩАЯ регистрация
    # в списке отсутствует. Поэтому добавляем id ЭТОГО устройства из ~/.extella/device.txt (его пишет
    # живой листенер) как отдельного кандидата. Доказано вживую: старый id молчит, новый отвечает.
    _known = {str(r.get("target")) for r in lst}
    try:
        _dev_local = (Path.home() / ".extella" / "device.txt").read_text(encoding="utf-8").strip()
    except Exception:
        _dev_local = ""
    if _dev_local and _dev_local not in _known:
        lst = list(lst) + [{"target": _dev_local, "description": "Это устройство (текущая регистрация)",
                            "_local": True}]
    out, threads = [], []

    def _one(rec):
        tgt = rec.get("target")
        pr = _probe_target(tgt)
        # паспорт ищем по РЕАЛЬНОМУ hostname из пробы (slug паспорта = санитизированный hostname),
        # а не гаданием по описанию устройства
        pp = None
        _cands = []
        if pr.get("host"):
            _h = str(pr["host"]).lower()
            _cands += [_h, "".join(c if c.isalnum() else "-" for c in _h)[:40].strip("-")]
        _cands.append(tgt)
        for slug in filter(None, _cands):
            pp = _target_passport(slug) or pp
            if pp:
                break
        age = None
        if isinstance(pp, dict) and pp.get("passport_at"):
            try:
                age = round((datetime.now(timezone.utc) - datetime.fromisoformat(
                    str(pp["passport_at"]).replace("Z", "+00:00"))).total_seconds() / 3600, 1)
            except Exception:
                age = None
        # ⚠ БЕЗОПАСНОСТЬ (замечание Анвара): device id — это ДОСТУП к устройству («по нему можно
        # подключиться к кому угодно»), платформа его прячет намеренно. Наружу отдаём только маску
        # и непрозрачную ссылку ref; настоящий id живёт ТОЛЬКО в памяти моста и подставляется
        # server-side при пиннинге прогона.
        _ref = hashlib.sha256(str(tgt).encode("utf-8")).hexdigest()[:12]
        _TARGET_REFS[_ref] = tgt
        out.append({"ref": _ref, "target_masked": (str(tgt)[:8] + "…") if tgt else "",
                    "label": rec.get("description") or (str(tgt)[:8] + "…"),
                    "online": pr["online"], "why": pr["why"],
                    "is_local": bool(rec.get("_local")) or (tgt == _dev_local),
                    "stale_hint": (not pr["online"]) and bool(_dev_local) and tgt != _dev_local
                                  and "device" in str(rec.get("description", "")).lower(),
                    "is_host": tgt == HOST_TARGET,   # сам id наружу НЕ отдаём
                    "apps": (pp or {}).get("apps") or [], "models": (pp or {}).get("ollama_models") or [],
                    "disk_free_gb": (pp or {}).get("disk_free_gb"), "passport_age_h": age})

    _errs = []

    def _one_safe(rec):
        try:
            _one(rec)
        except Exception as e:   # сбой одного устройства не должен молча обнулять весь список
            _errs.append(_scrub(str(e)[:120]))

    for rec in lst:
        th = threading.Thread(target=_one_safe, args=(rec,), daemon=True)
        th.start()
        threads.append(th)
    for th in threads:
        th.join(timeout=30)
    out.sort(key=lambda d: (not d["online"], not d["is_host"], d["label"]))
    if _errs and not out:
        out = [{"ref": "", "label": "не удалось опросить устройства", "online": False,
                "why": "; ".join(_errs[:2]), "apps": [], "models": []}]
    _DEVICES_CACHE["at"], _DEVICES_CACHE["data"] = now_ts, out
    return out


def _target_preflight(s, mode="manual"):
    """Мультитаргет T2 (паттерн WZ-07): проверка требований процесса к устройству ДО прогона.
    Требования — s['target_requirements'] {apps: [...], local_only: bool, device: <slug>|null}.
    Честный отказ с remediation вместо падения внутри прогона. mode: manual | schedule.
    T3-связка: свежесть паспорта = heartbeat (тик переснимает паспорта); протухший паспорт
    требуемого устройства = устройство молчит → не запускаем в никуда."""
    req = s.get("target_requirements") or {}
    if not req:
        return {"ok": True}
    if mode == "schedule" and req.get("local_only"):
        return {"ok": False, "code": "local_only",
                "message": "данные процесса помечены local_only — расписание исполняется на хостинге, "
                           "это запрещено правилом. Запускайте вручную на устройстве данных или снимите пометку."}
    dev = str(req.get("device") or "")
    if not dev and mode == "manual":
        import platform as _pl
        dev = "".join(c if c.isalnum() else "-" for c in (_pl.node() or "").lower())[:40].strip("-")
    apps_need = [a for a in (req.get("apps") or []) if isinstance(a, str) and a.strip()]
    if not dev:
        if apps_need:
            return {"ok": True, "warning": "требуются приложения " + ", ".join(apps_need) +
                    ", но устройство исполнения не сопоставлено с паспортом — проверка пропущена"}
        return {"ok": True}
    pp = _target_passport(dev)
    if not pp:
        return {"ok": False, "code": "no_passport",
                "message": "у устройства «" + dev + "» нет паспорта — снимите его "
                           "(эксперт wz_target_passport на устройстве), иначе требования не проверить"}
    try:
        age_h = (datetime.now(timezone.utc) -
                 datetime.fromisoformat(str(pp.get("passport_at", "")).replace("Z", "+00:00"))).total_seconds() / 3600
    except Exception:
        age_h = 1e9
    if age_h > 48:
        return {"ok": False, "code": "stale_passport",
                "message": "паспорт устройства «" + (pp.get("label") or dev) + "» старше 48 часов — "
                           "устройство молчит (оффлайн?). Прогон не запускаю в никуда; проверьте устройство."}
    missing = [a for a in apps_need if a not in (pp.get("apps") or [])]
    if missing:
        return {"ok": False, "code": "missing_apps",
                "message": "на устройстве «" + (pp.get("label") or dev) + "» нет требуемых приложений: " +
                           ", ".join(missing) + ". Установите их или уберите требование."}
    # A5 ДРИФТ УСТРОЙСТВА: устройство изменилось с момента привязки (пропало приложение/модель — процесс
    # на них опирался; мало диска / сменилось окружение — мягкий сигнал). Первый прогон запоминает базу.
    cur_fp = _device_fingerprint(pp)
    base_fp = s.get("device_baseline")
    sid = s.get("session_id")
    if not isinstance(base_fp, dict):
        if sid:
            try:
                _update_session(sid, lambda ss: ss.__setitem__("device_baseline", cur_fp))
            except Exception:
                pass
        return {"ok": True}
    dr = _device_drift(base_fp, cur_fp)
    if dr.get("breaking"):
        lost = ", ".join((dr.get("gone_apps") or []) + (dr.get("gone_models") or []))
        return {"ok": False, "code": "device_drift",
                "message": "устройство «" + (pp.get("label") or dev) + "» изменилось с момента привязки: "
                           "пропало — " + lost + ". Верните на место или пересоберите процесс под текущее устройство.",
                "drift": dr}
    if dr.get("soft"):
        return {"ok": True, "warning": "устройство изменилось: " + "; ".join(dr["soft"]), "drift": dr}
    return {"ok": True}


def _cspl_compile_derived(spec, program, records, action="compile"):
    """CSPL Studio: компиляция derived-языка. Язык = декларативная СПЕЦИФИКАЦИЯ поверх ядра
    report_dsl: merge дефолтов, доменная валидация (required_columns), замороженные поля
    (locked_fields принудительно из дефолта — пользователь их не переопределит). Кода у
    derived-языка нет — исполняет проверенное ядро, детерминизм и вет наследуются."""
    dp = dict(spec.get("default_program") or {})
    prog = dict(dp)
    if isinstance(program, dict):
        prog.update(program)
    for lf in (spec.get("locked_fields") or []):
        if lf in dp:
            prog[lf] = dp[lf]
        else:
            prog.pop(lf, None)
    req = [c for c in (spec.get("required_columns") or []) if isinstance(c, str)]
    missing = [c for c in req if c not in (prog.get("columns") or [])]
    if missing:
        return {"status": "invalid",
                "errors": [{"field": "columns", "message": "обязательные колонки языка отсутствуют: " + ", ".join(missing)}]}
    params = {"action": action, "program_json": json.dumps(prog, ensure_ascii=False),
              "output_dir": "/tmp/cspl_derived"}
    if records:
        params["records_json"] = json.dumps(records, ensure_ascii=False)
    res = run_expert("cspl_report_dsl", params, wait=180, glob=True)
    if isinstance(res, str):
        try:
            res = json.loads(res)
        except Exception:
            try:
                import ast as _a
                res = _a.literal_eval(res)   # платформа отдаёт result питоновским repr
            except Exception:
                res = {"status": "error", "message": str(res)[:200]}
    return res if isinstance(res, dict) else {"status": "error", "message": str(res)[:200]}


def _registry_refresh_async():
    """Capability Registry: фоновый ПОЛНЫЙ пересбор после событий, меняющих состав возможностей
    (публикация, установка, удаление, сохранение композиции). Полный вместо инкремента —
    устойчив к гонкам шардов (последний победил). Best-effort: событие ответа не ждёт."""
    def _go():
        try:
            run_expert("wz_registry_rebuild", {}, wait=240, glob=True)
        except Exception:
            pass
    threading.Thread(target=_go, daemon=True).start()


# ─────────── ПОЧЕМУ ПРОГОН УПАЛ: по-человечески и с действием ───────────
# Владелец видел «Ошибка: прогон не дал результата» и не понимал, что делать: чинить
# логику? данные? звать нас? В записи прогона причина не сохранялась вовсе.
# Тот же принцип, что у ошибок стройки (AC-06): что случилось + ЧТО ДЕЛАТЬ.
RUN_ERRORS = [
    ("input_path is missing", "no_input",
     "Первый шаг не получил данные",
     "Источник не отдал файл. Откройте «Настройка» → «Источник данных» и привяжите источник заново — "
     "проверьте, что указан лист С ДАННЫМИ, а не с инструкцией."),
    ("не восстановлен из стора", "no_input",
     "Данные не доехали до устройства, где идёт процесс",
     "Проверьте привязку источника в «Настройка» → «Источник данных»; если файл загружается вручную — "
     "приложите его заново."),
    ("источник не отдал данные", "source_dead",
     "Источник не ответил",
     "Проверьте доступ: таблица открыта по ссылке? токен не истёк? Раздел «Подключения» вверху страницы."),
    ("Embedding error", "platform",
     "Платформа не приняла данные",
     "Это сбой на стороне платформы, а не в вашем процессе. Повторите через несколько минут."),
]


# ─────────── ОБЁРТКА НАД УСТАНОВЛЕННОЙ ПРОГРАММОЙ ───────────
# Разрыв, найденный с Анваром: программа из «Программ» РЕАЛЬНО ставится на устройство
# (figlet лежит в /opt/homebrew/bin), но Композитор её не видит — он запускает блоки
# ПО ИМЕНИ ЭКСПЕРТА, а эксперта для новой программы никто не делает. Цепочка рвалась:
#   поставил → бинарь на месте → (пусто) → каталог → Композитор
# Здесь закрываем «пусто»: модель проектирует СПЕКУ вызова, код обёртки генерируем МЫ
# по проверенному шаблону (принцип CSPL: LLM проектирует, а не пишет исполняемый код),
# затем ОБЯЗАТЕЛЬНО прогоняем вживую — и только успешный прогон открывает путь в каталог.

_WRAP_TEMPLATE = (
    '# expert: %(NAME)s\n'
    '# description: %(DESC)s\n'
    '\n'
    'def %(NAME)s(%(SIG)s) -> str:\n'
    '    import os, json, shutil, subprocess\n'
    '    def binpath():\n'
    '        f = os.path.expanduser("~/.extella_cli/%(TOOL)s")\n'
    '        if os.path.exists(f):\n'
    '            p = open(f).read().strip()\n'
    '            if p and os.path.exists(p):\n'
    '                return p\n'
    '        p = shutil.which("%(TOOL)s")\n'
    '        if p:\n'
    '            return p\n'
    '        for c in ["/opt/homebrew/bin/%(TOOL)s", "/usr/local/bin/%(TOOL)s", "/usr/bin/%(TOOL)s"]:\n'
    '            if os.path.exists(c):\n'
    '                return c\n'
    '        return None\n'
    '    b = binpath()\n'
    '    if not b:\n'
    '        return json.dumps({"status": "error",\n'
    '                           "message": "программа %(TOOL)s не установлена на этом устройстве — '
    'поставьте её в разделе «Программы»"}, ensure_ascii=False)\n'
    '    SUB = {%(SUBS)s}\n'
    '    DEF = %(DEFAULTS)s\n'
    '    for k in list(SUB.keys()):\n'
    '        v = SUB[k]\n'
    '        if v is None or (isinstance(v, str) and (not v or v.startswith("{{"))):\n'
    '            SUB[k] = DEF.get(k, "")\n'
    '    argv = [b]\n'
    '    for tok in %(ARGV)s:\n'
    '        for k, v in SUB.items():\n'
    '            tok = tok.replace("{" + k + "}", str(v))\n'
    '        if tok != "":\n'
    '            argv.append(tok)\n'
    '    try:\n'
    '        r = subprocess.run(argv, capture_output=True, text=True, timeout=120)\n'
    '    except Exception as e:\n'
    '        return json.dumps({"status": "error", "message": "вызов не удался: " + str(e)[:120]}, ensure_ascii=False)\n'
    '    if r.returncode != 0:\n'
    '        return json.dumps({"status": "error", "message": (r.stderr or r.stdout or "программа вернула ошибку")[:300]}, ensure_ascii=False)\n'
    '    return json.dumps({"status": "success", "output": (r.stdout or "")[:20000], "tool": "%(TOOL)s"}, ensure_ascii=False)\n'
)


def _cleanup_failed_wrap(name):
    """Обёртку сохраняем на платформу ДО живого прогона — иначе её нельзя запустить. Если прогон
    не прошёл, эксперт-полуфабрикат остаётся висеть и потом путается с настоящими способностями.
    (Из-за такого «висяка» я 20.07 чуть не спутал мусор пробы с рабочими pandoc-способностями.)
    Прибираем за собой сразу — но ТОЛЬКО если этого имени нет ни в одном каталоге, чтобы случайно
    не снести уже используемый блок с тем же именем."""
    try:
        for key in ("composer:catalog",):
            g = api("/api/kv/get", {"key": key, "global": True})
            cur = json.loads(g.get("value")) if isinstance(g, dict) and g.get("value") else {}
            for b in (cur.get("blocks") or []):
                if isinstance(b, dict) and b.get("id") == name:
                    return   # имя уже используется как блок — не трогаем
        api("/api/expert/delete", {"name": name, "global": True})
    except Exception:
        pass   # не удалось прибрать — это не повод ронять и без того неуспешный поток


def _tool_probe_reason(tool, raw):
    """Сырую ошибку программы — в человеческий ответ. Business-пользователь не должен видеть
    Haskell-трейс pandoc или «withBinaryFile: does not exist»: у Гульжан ровно такой сырой текст
    из другого места вызвал панику. Отдельно ловим САМЫЙ ЧАСТЫЙ случай: программе нужен реальный
    входной файл, а живая проба его не даёт — это не поломка, а «нечем проверить автоматически»."""
    low = str(raw).lower()
    if any(s in low for s in ("no such file", "does not exist", "not found", "cannot open",
                              "нет такого файла", "input", "withbinaryfile")):
        return ("«%s» работает с вашим файлом, поэтому автоматически проверить её на пустом месте "
                "нельзя. Такую программу удобнее подключать прямо в сборке процесса, указав реальный "
                "файл. Сделать блоком из этого экрана пока не получится." % str(tool))
    return _scrub(str(raw)[:200])


def _cli_wrap_flow(tool, purpose=""):
    """Полный путь «программа → рабочий блок»: спека вызова → генерация обёртки по шаблону →
    сохранение эксперта → ЖИВАЯ ПРОВЕРКА → каталог Композитора.
    Каждый этап отвечает честно: где сорвалось и почему. В каталог попадает только то,
    что реально отработало на устройстве."""
    sp = _wrap_spec(tool, purpose)
    if not sp.get("spec"):
        return {"ok": False, "stage": "spec", "why": sp.get("why") or "не удалось спроектировать вызов"}
    spec = sp["spec"]
    name, code = _wrap_render(tool, spec)
    sv = api("/api/expert/save", {"name": name, "code": code, "description": spec["description"][:200],
                                  "kwargs": {p["name"]: p["default"] for p in spec["params"]},
                                  "cspl": "fython", "global": True})
    if not (isinstance(sv, dict) and (sv.get("status") == "success" or sv.get("id"))):
        return {"ok": False, "stage": "save", "expert": name, "why": _scrub(str(sv)[:150])}
    probe = run_expert(name, {p["name"]: p["default"] for p in spec["params"]}, wait=120, glob=True)
    if isinstance(probe, str):
        try:
            probe = json.loads(probe)
        except Exception:
            try:
                import ast as _a
                probe = _a.literal_eval(probe)
            except Exception:
                probe = {"status": "error", "message": str(probe)[:200]}
    if not (isinstance(probe, dict) and probe.get("status") == "success" and str(probe.get("output", "")).strip()):
        raw_why = str((probe or {}).get("message") or "программа ничего не вернула")
        _cleanup_failed_wrap(name)   # проба не прошла — эксперт-полуфабрикат не оставляем на платформе
        return {"ok": False, "stage": "probe", "expert": name, "spec": spec,
                "why": _tool_probe_reason(tool, raw_why)}
    added = _composer_catalog_add({
        "id": name, "kind": "tool",
        "what": spec["description"] or ("вызов программы " + str(tool)),
        "params": {p["name"]: p["desc"] or p["default"] for p in spec["params"]},
        "defaults": {p["name"]: p["default"] for p in spec["params"]},
        "kw": " ".join([str(tool), spec["action"], spec["description"]])[:400],
        # Из ЧЕГО сделан блок. Без этого нельзя честно ответить «что установлено, но ещё не
        # используется» — приходилось бы угадывать по имени эксперта.
        "origin": {"kind": "cli", "ref": str(tool)},
        "source": "installed"})
    return {"ok": True, "expert": name, "spec": spec, "catalog": added,
            "sample": str(probe.get("output", ""))[:300]}


def _composer_catalog_add(block):
    """Пополнить каталог Композитора проверенным блоком. Каталог перестаёт быть статичным
    файлом из поставки: у каждого клиента он свой — из того, что ОН установил (решение Анвара:
    установленное пользователем работает наравне с нашим ядром)."""
    try:
        g = api("/api/kv/get", {"key": "composer:catalog", "global": True})
        cur = json.loads(g.get("value")) if isinstance(g, dict) and g.get("value") else {}
    except Exception:
        cur = {}
    blocks = cur.get("blocks") if isinstance(cur, dict) else None
    if not isinstance(blocks, list):
        blocks = []
    blocks = [b for b in blocks if isinstance(b, dict) and b.get("id") != block.get("id")]
    blocks.append(block)
    cur = dict(cur if isinstance(cur, dict) else {})
    cur["blocks"] = blocks
    r = api("/api/kv/set", {"key": "composer:catalog", "value": json.dumps(cur, ensure_ascii=False),
                            "description": "composer catalog", "global": True})
    ok = not (isinstance(r, dict) and r.get("status") == "error")
    return {"ok": ok, "blocks": len(blocks), "why": "" if ok else _scrub(str(r)[:120])}


def _wrap_spec(tool, purpose, agent_id=None):
    """Модель проектирует СПЕКУ вызова программы (не код): какие параметры и как складывается argv."""
    ag = agent_id or qwen_agent()
    if not ag:
        return {"spec": None, "why": "нет агента для проектирования"}
    prompt = ("Ты проектируешь вызов консольной программы для автоматизации. Верни ТОЛЬКО JSON:\n"
              '{"action":"<короткое_имя_действия_латиницей>","description":"<что делает, по-русски, одной фразой>",'
              '"params":[{"name":"<имя_латиницей>","default":"<значение по умолчанию>","desc":"<что это>"}],'
              '"argv":["<токен>","{имя_параметра}"]}\n\n'
              "ПРАВИЛА:\n"
              "- argv — аргументы БЕЗ самой программы (её подставим сами);\n"
              "- подстановки пиши как {имя_параметра}, имена только из params;\n"
              "- параметров не больше четырёх, у каждого разумный default, чтобы вызов работал без настройки;\n"
              "- никаких пайпов, редиректов, кавычек-обёрток и sh -c — только чистые аргументы;\n"
              "- результат программа должна писать в stdout.\n\n"
              "Программа: " + str(tool)[:40] + "\n"
              "Что от неё нужно: " + (str(purpose)[:200] or "типовое применение этой программы"))
    try:
        res = api("/api/agent/run", {"agent_id": ag, "input": prompt, "run_timeout": 60,
                                     "store": False, "temperature": 0}, timeout=70)
    except Exception as e:
        return {"spec": None, "why": "модель не ответила: " + _scrub(str(e)[:120])}
    text = ""
    for it in (res or {}).get("output", []):
        if isinstance(it, dict) and it.get("type") == "message":
            for c in it.get("content", []):
                if isinstance(c, dict) and c.get("type") == "output_text":
                    text += c.get("text", "")
    text = text or (res or {}).get("output_text", "")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {"spec": None, "why": _llm_error_human(res) or "модель не вернула спеку вызова"}
    try:
        raw = json.loads(m.group(0))
    except Exception:
        return {"spec": None, "why": "спека от модели не разобралась"}

    act = re.sub(r"[^a-z0-9_]", "", str(raw.get("action") or "run").lower())[:24] or "run"
    params, seen = [], set()
    for p in (raw.get("params") or [])[:4]:
        if not isinstance(p, dict):
            continue
        nm = re.sub(r"[^a-z0-9_]", "", str(p.get("name") or "").lower())[:24]
        if not nm or nm in seen:
            continue
        seen.add(nm)
        params.append({"name": nm, "default": str(p.get("default") or "")[:120],
                       "desc": str(p.get("desc") or "")[:80]})
    argv = [str(a)[:200] for a in (raw.get("argv") or []) if isinstance(a, (str, int, float))]
    # Обёртка запускает программу НАПРЯМУЮ (без shell), но мусор в аргументах отсекаем на входе:
    # пайпы и редиректы означают, что модель поняла задачу как shell-команду — такое не принимаем.
    bad = [a for a in argv if any(ch in a for ch in ("|", ">", "<", "&", ";", "`", "$("))]
    if bad:
        return {"spec": None, "why": "модель предложила небезопасные аргументы: " + bad[0][:40]}
    unknown = [mm for a in argv for mm in re.findall(r"\{(\w+)\}", a) if mm not in seen]
    if unknown:
        return {"spec": None, "why": "в аргументах есть параметр, которого нет в списке: " + str(unknown[0])}
    return {"spec": {"action": act, "description": str(raw.get("description") or "")[:200],
                     "params": params, "argv": argv}, "why": ""}


def _wrap_render(tool, spec):
    """Код обёртки собираем МЫ по проверенному шаблону — LLM его не пишет."""
    tool_id = re.sub(r"[^a-z0-9_]", "", str(tool).lower())[:24]
    name = "cap_" + tool_id + "_" + spec["action"]
    params = spec["params"]
    sig = ", ".join('%s: str = ""' % p["name"] for p in params)
    subs = ", ".join('"%s": %s' % (p["name"], p["name"]) for p in params)
    defaults = json.dumps({p["name"]: p["default"] for p in params}, ensure_ascii=False)
    desc = (spec.get("description") or ("вызов программы " + tool_id))
    desc = desc.replace("\n", " ") + " Зови ЭТОТ эксперт, не пиши shell-команду."
    code = _WRAP_TEMPLATE % {"NAME": name, "DESC": desc, "SIG": sig, "TOOL": tool_id,
                             "SUBS": subs, "DEFAULTS": defaults,
                             "ARGV": json.dumps(spec["argv"], ensure_ascii=False)}
    return name, code


# ─────────── ОБЁРТКА НАД ИНСТРУМЕНТОМ MCP ───────────
# Тот же разрыв, что был у программ, только у MCP: сервер подключается, эксперт mcp_call на
# платформе есть — а Композитор им пользоваться НЕ МОЖЕТ. Каталог блоков это белый список: чего
# в нём нет, того процесс не возьмёт. Значит инструмент MCP нужно объявить блоком.
#
# Отличие от программ принципиальное и в лучшую сторону: MCP-сервер САМ отдаёт схему аргументов
# (после правки mcp_call 19.07 — до неё схема выбрасывалась). Поэтому структуру вызова модель
# больше НЕ ПРОЕКТИРУЕТ: имена полей, типы и обязательность берём из схемы дословно. Модели
# оставлено только человеческое — описание по-русски, значения для пробного прогона и слова для
# поиска. Угадывать имена аргументов больше нечем.
_MCP_WRAP_TEMPLATE = (
    '# expert: %(NAME)s\n'
    '# description: %(DESC)s\n'
    '\n'
    'def %(NAME)s(%(SIG)s) -> str:\n'
    '    import json, urllib.request\n'
    '    from pathlib import Path\n'
    '    SUB = {%(SUBS)s}\n'
    '    DEF = %(DEFAULTS)s\n'
    '    KEYS = %(KEYMAP)s\n'
    '    TYPES = %(TYPES)s\n'
    '    args = {}\n'
    '    for k, v in SUB.items():\n'
    '        if v is None or (isinstance(v, str) and (not v or v.startswith("{{"))):\n'
    '            v = DEF.get(k, "")\n'
    '        if v == "":\n'
    '            continue\n'
    '        k2 = KEYS.get(k, k)\n'
    '        t = TYPES.get(k, "string")\n'
    '        if t in ("integer", "number"):\n'
    '            try:\n'
    '                v = int(v) if t == "integer" else float(v)\n'
    '            except Exception:\n'
    '                return json.dumps({"status": "error",\n'
    '                                   "message": "поле " + k2 + " должно быть числом, получено: " + str(v)[:40]},\n'
    '                                  ensure_ascii=False)\n'
    '        elif t == "boolean":\n'
    '            v = str(v).strip().lower() in ("1", "true", "да", "yes", "on")\n'
    '        elif t in ("array", "object") and isinstance(v, str):\n'
    '            try:\n'
    '                v = json.loads(v)\n'
    '            except Exception:\n'
    '                return json.dumps({"status": "error",\n'
    '                                   "message": "поле " + k2 + " должно быть JSON, получено: " + str(v)[:60]},\n'
    '                                  ensure_ascii=False)\n'
    '        args[k2] = v\n'
    '    miss = [k for k in %(REQUIRED)s if k not in args]\n'
    '    if miss:\n'
    '        return json.dumps({"status": "error",\n'
    '                           "message": "не заполнено обязательное поле: " + ", ".join(miss)},\n'
    '                          ensure_ascii=False)\n'
    '    cfg = Path.home() / "extella_wizard" / "app" / "config.json"\n'
    '    try:\n'
    '        tok = json.loads(cfg.read_text(encoding="utf-8")).get("auth_token", "") if cfg.exists() else ""\n'
    '    except Exception:\n'
    '        tok = ""\n'
    '    if not tok:\n'
    '        return json.dumps({"status": "error",\n'
    '                           "message": "нет токена Extella (~/extella_wizard/app/config.json)"},\n'
    '                          ensure_ascii=False)\n'
    '    body = {"expert_name": "mcp_call", "global": True,\n'
    '            "params": {"server": "%(SERVER)s", "tool": "%(TOOL)s",\n'
    '                       "args_json": json.dumps(args, ensure_ascii=False)}}\n'
    '    rq = urllib.request.Request("https://api.extella.ai/api/expert/run",\n'
    '                                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),\n'
    '                                headers={"X-Auth-Token": tok, "Content-Type": "application/json",\n'
    '                                         "X-Profile-Id": "default",\n'
    '                                         "X-Agent-Id": "agent_extella_default"}, method="POST")\n'
    '    try:\n'
    '        raw = json.loads(urllib.request.urlopen(rq, timeout=240).read().decode("utf-8"))\n'
    '    except Exception as e:\n'
    '        return json.dumps({"status": "error", "message": "вызов MCP не прошёл: " + str(e)[:140]},\n'
    '                          ensure_ascii=False)\n'
    '    res = raw.get("result")\n'
    '    if isinstance(res, str):\n'
    '        try:\n'
    '            res = json.loads(res)\n'
    '        except Exception:\n'
    '            import ast\n'
    '            try:\n'
    '                res = ast.literal_eval(res)\n'
    '            except Exception:\n'
    '                res = {"status": "error", "message": str(res)[:200]}\n'
    '    if not isinstance(res, dict) or res.get("status") != "success":\n'
    '        return json.dumps({"status": "error",\n'
    '                           "message": str((res or {}).get("message") or "MCP вернул ошибку")[:250]},\n'
    '                          ensure_ascii=False)\n'
    '    if res.get("is_error"):\n'
    '        return json.dumps({"status": "error", "message": str(res.get("result"))[:250]},\n'
    '                          ensure_ascii=False)\n'
    '    return json.dumps({"status": "success", "output": str(res.get("result", ""))[:20000],\n'
    '                       "server": "%(SERVER)s", "tool": "%(TOOL)s"}, ensure_ascii=False)\n'
)


def _expert_exists(name):
    """Есть ли эксперт на ЭТОМ аккаунте. `global: true` у платформы означает «глобально в пределах
    аккаунта», между аккаунтами ничего не разъезжается — проверено на втором живом аккаунте 19.07.
    Отсюда целый класс «работает у нас, молча не работает у клиента».
    Отсутствие определяем по ЛЮБОЙ неудаче чтения: платформа на несуществующего эксперта отвечает
    HTTP 500, а не 404, — ловить 404 бесполезно."""
    try:
        r = api("/api/expert/get", {"name": str(name), "global": True}, timeout=45)
        return isinstance(r, dict) and r.get("status") == "success"
    except Exception:
        return False


_WRITE_VERBS = ("write", "create", "delete", "remove", "move", "rename", "put", "post", "patch",
                "update", "set", "insert", "append", "upload", "modify", "edit", "drop", "truncate",
                "send", "push", "commit", "mkdir", "rmdir", "chmod", "kill", "exec", "run", "command",
                # 20.07: «apply_spreadsheet_updates» (запись в Google Sheets) проскочил — «updates»
                # это не «update», а «apply»/«copy» вообще не были в списке. Добавлены + стемминг.
                "apply", "copy", "duplicate", "sync", "replace", "clear", "add", "import", "share",
                "publish", "revoke", "grant", "assign", "cancel", "close", "merge", "batch", "save",
                "enable", "disable", "start", "stop", "restart", "trigger", "schedule")


def _mcp_tool_writes(tool_name):
    """Меняет ли инструмент данные — по глаголу в имени. Огрубление осознанное: ложно отсеять
    безобидное чтение дешевле, чем пустить в автопилот запись, которую живой гейт ПООЩРЯЕТ (у
    записи «успех» на любых входах). Сверяем стем каждого слова (снимаем хвостовые -s/-es), иначе
    «updates» ≠ «update» и запись проскакивает — так и утекло 20.07."""
    toks = re.sub(r"[^a-z0-9]+", "_", str(tool_name or "").lower()).split("_")
    stems = set(toks)
    for t in toks:
        if t.endswith("es"):
            stems.add(t[:-2])
        elif t.endswith("s"):
            stems.add(t[:-1])
    return any(v in stems for v in _WRITE_VERBS)


def _mcp_tools(server):
    """Инструменты сервера СО СХЕМОЙ аргументов. Молчать нельзя: пустой список и недоступный
    сервер — разные вещи, и разбирать их придётся человеку."""
    # Мост к MCP — отдельный эксперт, и его может НЕ БЫТЬ на аккаунте клиента: до 19.07 он не
    # ставился ни одним установщиком и жил только там, где его создали руками. Без этой проверки
    # человек получал бы «Expert not found» — сообщение, которое ему ничего не говорит.
    if not _expert_exists("mcp_call"):
        return {"tools": [], "no_schema": [],
                "why": "на этом аккаунте нет моста к MCP (эксперт mcp_call). Он ставится "
                       "установщиком Extella — обновитесь и повторите; если не поможет, напишите нам"}
    res = run_expert("mcp_call", {"server": str(server), "tool": "__list__"}, wait=240, glob=True)
    if isinstance(res, str):
        try:
            res = json.loads(res)
        except Exception:
            try:
                import ast as _a
                res = _a.literal_eval(res)
            except Exception:
                res = {"status": "error", "message": str(res)[:200]}
    if not (isinstance(res, dict) and res.get("status") == "success"):
        return {"tools": [], "why": str((res or {}).get("message") or "сервер не ответил")[:200]}
    tools = [t for t in (res.get("tools") or []) if isinstance(t, dict) and t.get("name")]
    if not tools:
        return {"tools": [], "why": "сервер подключён, но не объявил ни одного инструмента"}
    no_schema = [t["name"] for t in tools if not (t.get("schema") or {}).get("properties")]
    return {"tools": tools, "why": "",
            # Инструмент без схемы обернуть НЕЛЬЗЯ честно — угадывать поля мы отказались.
            "no_schema": no_schema}


def _mcp_wrap_spec(server, tool, agent_id=None):
    """Модель описывает инструмент ПО-ЧЕЛОВЕЧЕСКИ и подбирает значения для пробного прогона.
    Структуру вызова она не трогает — та берётся из схемы сервера."""
    props = (tool.get("schema") or {}).get("properties") or {}
    req = (tool.get("schema") or {}).get("required") or []
    ag = agent_id or qwen_agent()
    if not ag:
        return {"spec": None, "why": "нет агента для описания инструмента"}
    fields = "\n".join("- %s (%s%s): %s" % (k, v.get("type") or "string",
                                            ", обязательное" if k in req else "",
                                            str(v.get("desc") or "")[:90])
                       for k, v in props.items())
    prompt = ("Опиши инструмент внешнего сервиса для каталога бизнес-автоматизаций. Верни ТОЛЬКО JSON:\n"
              '{"action":"<короткое_имя_действия_латиницей>",'
              '"description":"<что делает, по-русски, одной фразой для неспециалиста>",'
              '"defaults":{"<имя_поля>":"<значение для ПРОБНОГО запуска>"},'
              '"kw":"<слова для поиска, по-русски и по-английски>"}\n\n'
              "ПРАВИЛА:\n"
              "- имена полей в defaults бери ТОЛЬКО из списка ниже, новых не выдумывай;\n"
              "- у КАЖДОГО обязательного поля должно быть значение, иначе проверка не пройдёт;\n"
              "- значения безобидные и заведомо рабочие (пример для веб-запроса: https://example.com);\n"
              "- описание деловое, без технического жаргона.\n\n"
              "Сервис: " + str(server)[:40] + "\nИнструмент: " + str(tool.get("name"))[:60] +
              "\nЧто о нём известно: " + str(tool.get("desc") or "")[:200] +
              "\nПоля:\n" + (fields or "(без полей)"))
    try:
        res = api("/api/agent/run", {"agent_id": ag, "input": prompt, "run_timeout": 60,
                                     "store": False, "temperature": 0}, timeout=70)
    except Exception as e:
        return {"spec": None, "why": "модель не ответила: " + _scrub(str(e)[:120])}
    text = ""
    for it in (res or {}).get("output", []):
        if isinstance(it, dict) and it.get("type") == "message":
            for c in it.get("content", []):
                if isinstance(c, dict) and c.get("type") == "output_text":
                    text += c.get("text", "")
    text = text or (res or {}).get("output_text", "")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {"spec": None, "why": _llm_error_human(res) or "модель не описала инструмент"}
    try:
        raw = json.loads(m.group(0))
    except Exception:
        return {"spec": None, "why": "описание от модели не разобралось"}

    act = re.sub(r"[^a-z0-9_]", "", str(raw.get("action") or tool.get("name") or "call").lower())[:24] or "call"
    # Значения — только для полей, которые СУЩЕСТВУЮТ в схеме. Выдуманные молча не берём.
    defaults = {k: str(v)[:200] for k, v in (raw.get("defaults") or {}).items()
                if isinstance(raw.get("defaults"), dict) and k in props}
    missing = [k for k in req if not defaults.get(k)]
    if missing:
        return {"spec": None, "why": "нечем проверить: модель не дала значения обязательных полей " + ", ".join(missing)}
    return {"spec": {"action": act, "description": str(raw.get("description") or "")[:200],
                     "defaults": defaults, "kw": str(raw.get("kw") or "")[:300],
                     "properties": props, "required": [r for r in req if isinstance(r, str)]}, "why": ""}


def _mcp_wrap_render(server, tool_name, spec):
    """Код обёртки собираем МЫ по шаблону — тот же принцип, что у программ."""
    srv = re.sub(r"[^a-z0-9_]", "", str(server).lower())[:20]
    tl = re.sub(r"[^a-z0-9_]", "", str(tool_name).lower())[:28]
    name = "cap_" + srv + "_" + (tl or spec["action"])
    # Имя поля схемы может не быть годным именем python-параметра (дефисы, регистр) — держим карту.
    keymap, sig_parts, subs_parts, types = {}, [], [], {}
    for k, meta in spec["properties"].items():
        py = re.sub(r"[^a-z0-9_]", "_", str(k).lower())[:32] or "arg"
        while py in keymap:
            py += "_"
        keymap[py] = k
        # Тип поля из схемы. Параметры эксперта всегда приходят строками, а MCP-сервер типы
        # проверяет: у fetch поле max_length целочисленное, и строка "0" отлетала с
        # «'0' is not of type 'integer'». Приводим по схеме — это ровно то, ради чего мы её и
        # научили приходить, а не догадка.
        types[py] = str((meta or {}).get("type") or "string")
        sig_parts.append('%s: str = ""' % py)
        subs_parts.append('"%s": %s' % (py, py))
    rev = {v: k for k, v in keymap.items()}
    desc = (spec.get("description") or ("вызов " + str(tool_name))).replace("\n", " ")
    desc += " Зови ЭТОТ эксперт — он сам обратится к сервису %s." % server
    # ЗНАЧЕНИЯ ДЛЯ ПРОВЕРКИ ≠ ЗНАЧЕНИЯ ДЛЯ РАБОТЫ. Модель придумывает безобидные примеры, чтобы
    # прогнать живую пробу («https://example.com», «Asia/Almaty»). Зашить их как рабочие дефолты
    # ОБЯЗАТЕЛЬНЫХ полей нельзя: тогда шаг с незаполненным полем молча посчитает демо-данные и
    # вернёт успех — правдоподобный, но ложный ответ. Поймано на проверке 19.07.
    # Поэтому в код обёртки уходят дефолты ТОЛЬКО необязательных полей; проба получает всё явно.
    req_keys = set(spec["required"])
    runtime_def = {rev[k]: v for k, v in spec["defaults"].items() if k in rev and k not in req_keys}
    code = _MCP_WRAP_TEMPLATE % {
        "NAME": name, "DESC": desc[:400], "SIG": ", ".join(sig_parts) or "",
        "SUBS": ", ".join(subs_parts),
        "DEFAULTS": json.dumps(runtime_def, ensure_ascii=False),
        "KEYMAP": json.dumps(keymap, ensure_ascii=False),
        "TYPES": json.dumps(types, ensure_ascii=False),
        # Проверяем обязательность ПО ИМЕНИ ПОЛЯ СЕРВЕРА — args собираются уже под его именами.
        "REQUIRED": json.dumps([k for k in spec["required"]], ensure_ascii=False),
        "SERVER": server, "TOOL": tool_name}
    return name, code, keymap, runtime_def


def _mcp_wrap_one(server, tool):
    """Один инструмент → рабочий блок. В каталог попадает ТОЛЬКО то, что реально отработало."""
    if not (tool.get("schema") or {}).get("properties"):
        return {"ok": False, "tool": tool.get("name"), "stage": "schema",
                "why": "инструмент не объявил схему аргументов — угадывать поля не будем"}
    # ТОЛЬКО ЧТЕНИЕ. У программ и приложений это ограничение стоит, у MCP я его упустил — и живой
    # гейт по построению отобрал самое опасное: из 14 инструментов filesystem пробу прошли ровно
    # write_file и create_directory (у них «успех» на любых демо-данных, а чтение отпадает без
    # реального пути). Пишущий блок в каталоге страшнее его отсутствия: процесс на подставных
    # значениях создаст/перезапишет файл. Отсекаем по имени инструмента — до сохранения и пробы.
    if _mcp_tool_writes(tool.get("name")):
        return {"ok": False, "tool": tool.get("name"), "stage": "write",
                "why": "инструмент изменяет данные (запись/удаление) — в блок делаем только чтение"}
    sp = _mcp_wrap_spec(server, tool)
    if not sp.get("spec"):
        return {"ok": False, "tool": tool.get("name"), "stage": "spec", "why": sp.get("why")}
    spec = sp["spec"]
    name, code, keymap, runtime_def = _mcp_wrap_render(server, tool["name"], spec)
    rev = {v: k for k, v in keymap.items()}
    probe_args = {rev[k]: v for k, v in spec["defaults"].items() if k in rev}   # всё, включая обязательные
    sv = api("/api/expert/save", {"name": name, "code": code,
                                  "description": spec["description"][:200],
                                  "kwargs": runtime_def,   # обязательные пустыми — иначе демо-значения уедут в прод
                                  "cspl": "fython", "global": True})
    if not (isinstance(sv, dict) and (sv.get("status") == "success" or sv.get("id"))):
        return {"ok": False, "tool": tool["name"], "stage": "save", "expert": name,
                "why": _scrub(str(sv)[:150])}
    probe = run_expert(name, probe_args, wait=300, glob=True)
    if isinstance(probe, str):
        try:
            probe = json.loads(probe)
        except Exception:
            try:
                import ast as _a
                probe = _a.literal_eval(probe)
            except Exception:
                probe = {"status": "error", "message": str(probe)[:200]}
    if not (isinstance(probe, dict) and probe.get("status") == "success"
            and str(probe.get("output", "")).strip()):
        _cleanup_failed_wrap(name)
        return {"ok": False, "tool": tool["name"], "stage": "probe", "expert": name,
                "why": str((probe or {}).get("message") or "инструмент ничего не вернул")[:200]}
    # В каталоге обязательные поля помечены словом — Композитор должен взять их ИЗ ЗАДАЧИ клиента,
    # а не из наших примеров, иначе процесс посчитает демо-данные и никто этого не заметит.
    req_keys = set(spec["required"])
    added = _composer_catalog_add({
        "id": name, "kind": "mcp",
        "what": spec["description"] or ("вызов " + tool["name"]),
        "params": {rev[k]: ((v.get("desc") or v.get("type") or "") +
                            (" (обязательно)" if k in req_keys else ""))
                   for k, v in spec["properties"].items() if k in rev},
        "defaults": runtime_def,
        "kw": " ".join([str(server), tool["name"], spec["kw"], spec["description"]])[:400],
        "origin": {"kind": "mcp", "ref": str(server)},
        "source": "installed"})
    return {"ok": True, "tool": tool["name"], "expert": name, "catalog": added,
            "sample": str(probe.get("output", ""))[:200]}


def _mcp_wrap_flow(server, only=None):
    """Полный путь «сервер MCP → рабочие блоки». Оборачиваем не всё подряд: у filesystem
    четырнадцать инструментов, и блок «прочитать файл как base64» только засорит каталог.
    Правило: берём то, что просили (only) либо всё со схемой; в каталог пускает ЖИВОЙ прогон.
    Отчёт по каждому инструменту отдельно — что вошло, что нет и почему."""
    t = _mcp_tools(server)
    if not t["tools"]:
        return {"ok": False, "server": server, "why": t["why"], "wrapped": [], "skipped": []}
    tools = t["tools"]
    if only:
        want = {str(x).strip() for x in only}
        tools = [x for x in tools if x.get("name") in want]
        if not tools:
            return {"ok": False, "server": server, "wrapped": [], "skipped": [],
                    "why": "у сервера нет таких инструментов: " + ", ".join(sorted(want))}
    wrapped, skipped = [], []
    for tool in tools[:12]:   # предохранитель: сервер с сотней инструментов не выжрет каталог
        r = _mcp_wrap_one(server, tool)
        (wrapped if r.get("ok") else skipped).append(r)
    return {"ok": bool(wrapped), "server": server, "wrapped": wrapped, "skipped": skipped,
            "why": "" if wrapped else "ни один инструмент не прошёл живую проверку"}


# ─────────── ОБЁРТКА НАД УСТАНОВЛЕННЫМ ПРИЛОЖЕНИЕМ ───────────
# Последний из пяти типов способностей. Приложение ставится (app_install) и запускается
# (app_start), но нигде не сказано, ЧТО ОНО УМЕЕТ ДЛЯ ПРОЦЕССА — поэтому Композитор его не берёт.
#
# Отличие от программ и MCP: у приложения нет ни `--help`, ни схемы инструментов. Проверено на
# живом SearXNG: openapi.json/swagger.json отсутствуют. Значит спеку вызова предлагает модель по
# документации приложения — а решает ЖИВОЙ ПРОГОН, и гейт здесь строже, чем у программ: мало кода
# возврата, ответ обязан быть непустым и осмысленным.
#
# И главная особенность: ПОРТ У ПРИЛОЖЕНИЯ МЕНЯЕТСЯ ОТ ЗАПУСКА К ЗАПУСКУ (лаунчер берёт свободный).
# Зашить адрес в обёртку нельзя — она читает подтверждённый адрес из реестра при КАЖДОМ вызове.
_APP_WRAP_TEMPLATE = (
    '# expert: %(NAME)s\n'
    '# description: %(DESC)s\n'
    '\n'
    'def %(NAME)s(%(SIG)s) -> str:\n'
    '    import json, os, urllib.request, urllib.parse\n'
    '    SUB = {%(SUBS)s}\n'
    '    DEF = %(DEFAULTS)s\n'
    '    KEYS = %(KEYMAP)s\n'
    '    FIXED = %(FIXED)s\n'
    '    reg = os.path.expanduser("~/extella-plugins/_registry/%(REGFILE)s.json")\n'
    '    if not os.path.exists(reg):\n'
    '        return json.dumps({"status": "error",\n'
    '                           "message": "приложение %(APP)s не установлено на этом устройстве — '
    'поставьте его в разделе «Программы»"}, ensure_ascii=False)\n'
    '    try:\n'
    '        base = ((json.load(open(reg, encoding="utf-8")).get("ui") or {}).get("url") or "").rstrip("/")\n'
    '    except Exception as e:\n'
    '        return json.dumps({"status": "error", "message": "запись приложения не читается: " + str(e)[:110]},\n'
    '                          ensure_ascii=False)\n'
    '    if not base:\n'
    '        return json.dumps({"status": "error",\n'
    '                           "message": "приложение %(APP)s не запущено — запустите его и повторите"},\n'
    '                          ensure_ascii=False)\n'
    '    args = dict(FIXED)   # технические константы приложения — человек их не заполняет\n'
    '    for k, v in SUB.items():\n'
    '        if v is None or (isinstance(v, str) and (not v or v.startswith("{{"))):\n'
    '            v = DEF.get(k, "")\n'
    '        if v != "":\n'
    '            args[KEYS.get(k, k)] = v\n'
    '    miss = [k for k in %(REQUIRED)s if k not in args]\n'
    '    if miss:\n'
    '        return json.dumps({"status": "error",\n'
    '                           "message": "не заполнено обязательное поле: " + ", ".join(miss)},\n'
    '                          ensure_ascii=False)\n'
    '    url = base + "%(PATH)s"\n'
    '    data = None\n'
    '    if "%(METHOD)s" == "GET":\n'
    '        if args:\n'
    '            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(args)\n'
    '    else:\n'
    '        data = json.dumps(args, ensure_ascii=False).encode("utf-8")\n'
    '    rq = urllib.request.Request(url, data=data, method="%(METHOD)s",\n'
    '                                headers={"Accept": "application/json",\n'
    '                                         "Content-Type": "application/json",\n'
    '                                         "User-Agent": "Extella/1.0"})\n'
    '    try:\n'
    '        raw = urllib.request.urlopen(rq, timeout=120).read().decode("utf-8", "replace")\n'
    '    except Exception as e:\n'
    '        return json.dumps({"status": "error",\n'
    '                           "message": "приложение %(APP)s не ответило: " + str(e)[:130] +\n'
    '                                      " (проверьте, что оно запущено)"}, ensure_ascii=False)\n'
    '    if not raw.strip():\n'
    '        return json.dumps({"status": "error", "message": "приложение вернуло пустой ответ"},\n'
    '                          ensure_ascii=False)\n'
    '    LIMIT = 20000\n'
    '    try:\n'
    '        parsed = json.loads(raw)\n'
    '    except Exception:\n'
    '        parsed = None\n'
    '    if parsed is None:\n'
    '        return json.dumps({"status": "success", "output": raw[:LIMIT], "app": "%(APP)s",\n'
    '                           "truncated": len(raw) > LIMIT}, ensure_ascii=False)\n'
    '    # JSON режем ПО СТРУКТУРЕ, а не по символам: обрезанный по символу ответ — это сломанные\n'
    '    # данные, которые выглядят целыми, и дальше по процессу их никто не перепроверит.\n'
    '    dropped = 0\n'
    '    if isinstance(parsed, dict):\n'
    '        for k in ("results", "items", "data", "hits", "entries"):\n'
    '            if isinstance(parsed.get(k), list):\n'
    '                while len(json.dumps(parsed, ensure_ascii=False)) > LIMIT and parsed[k]:\n'
    '                    parsed[k].pop()\n'
    '                    dropped += 1\n'
    '                break\n'
    '    s = json.dumps(parsed, ensure_ascii=False)\n'
    '    if len(s) > LIMIT:\n'
    '        return json.dumps({"status": "error",\n'
    '                           "message": "ответ приложения слишком велик (" + str(len(s)) +\n'
    '                                      " символов) и не поддаётся сокращению без порчи данных"},\n'
    '                          ensure_ascii=False)\n'
    '    return json.dumps({"status": "success", "output": s, "app": "%(APP)s",\n'
    '                       "dropped_items": dropped}, ensure_ascii=False)\n'
)


def _app_registry_file(app_id):
    """Имя файла реестра — ПЛОСКОЕ, посимвольное зеркало тулбарного _safeIdOf. Идентификатор
    приложения содержит владельца через слэш (cocktailpeanut/searxng.pinokio), и вложенный путь
    здесь был отдельным дефектом (найден 19.07, починен контуром тулбара)."""
    return re.sub(r"[^a-zA-Z0-9]", "_", str(app_id))


def _app_info(app_id):
    """Что мы знаем о приложении: где лежит, по какому адресу отвечает, чем себя описывает.
    Никаких догадок: если не установлено или не запущено — так и говорим."""
    reg = Path.home() / "extella-plugins" / "_registry" / (_app_registry_file(app_id) + ".json")
    if not reg.exists():
        return {"ok": False, "why": "приложение не установлено на этом устройстве"}
    try:
        man = json.loads(reg.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "why": "запись приложения не читается: " + str(e)[:120]}
    url = ((man.get("ui") or {}).get("url") or "").rstrip("/")
    root = (man.get("app") or {}).get("root") or ""
    if not url:
        return {"ok": False, "why": "приложение установлено, но не запущено — запустите его и повторите",
                "root": root}
    # Документация рецепта — вход для проектирования спеки (у приложений нет ни --help, ни схемы).
    doc = ""
    for fn in ("README.md", "README.rst", "readme.md"):
        f = Path(root) / fn
        if root and f.exists():
            try:
                doc = f.read_text(encoding="utf-8", errors="ignore")[:2500]
                break
            except Exception:
                pass
    return {"ok": True, "url": url, "root": root, "repo": (man.get("app") or {}).get("repo") or "",
            "doc": doc, "why": ""}


def _app_wrap_spec(app_id, info, purpose, agent_id=None):
    """Модель проектирует HTTP-вызов приложения. Это САМОЕ слабое звено из трёх типов: схемы нет,
    опереться можно только на документацию. Поэтому здесь всё отсекает живой прогон, а не доверие."""
    ag = agent_id or qwen_agent()
    if not ag:
        return {"spec": None, "why": "нет агента для проектирования вызова"}
    prompt = ("Ты проектируешь обращение к локальному приложению по HTTP для бизнес-автоматизации. "
              "Верни ТОЛЬКО JSON:\n"
              '{"action":"<короткое_имя_действия_латиницей>",'
              '"description":"<что делает, по-русски, одной фразой для неспециалиста>",'
              '"method":"GET|POST","path":"/<путь>",'
              '"fixed":{"<техническое_поле>":"<постоянное значение>"},'
              '"params":[{"name":"<имя_латиницей>","default":"<значение для ПРОБНОГО запуска>",'
              '"required":true|false,"desc":"<что это>"}],'
              '"kw":"<слова для поиска, по-русски и по-английски>"}\n\n'
              "ПРАВИЛА:\n"
              "- путь начинается со слэша, БЕЗ адреса и порта — их подставим сами;\n"
              "- РАЗДЕЛЯЙ ДВА ВИДА ПОЛЕЙ. В fixed — технические, значение которых ОДНО И ТО ЖЕ при каждом "
              "вызове (например format=json): их подставим сами, человек их не увидит. В params — только "
              "то, что МЕНЯЕТСЯ от задачи к задаче и что человек осмысленно заполняет (например поисковый "
              "запрос). Технический флаг в params — ошибка: пользователь не должен вводить format=json;\n"
              "- параметров не больше пяти; у обязательных обязан быть рабочий default, иначе проверка не пройдёт;\n"
              "- нужен ответ, пригодный для машины — если у приложения есть такой формат, ставь его в fixed;\n"
              "- выбирай ЧТЕНИЕ, а не изменение: никаких удалений, настроек и записи.\n\n"
              "Приложение: " + str(app_id)[:60] + "\nИсточник: " + str(info.get("repo") or "")[:80] +
              "\nЧто от него нужно: " + (str(purpose)[:200] or "типовое применение этого приложения") +
              "\nДокументация:\n" + (info.get("doc") or "(нет)")[:1800])
    try:
        res = api("/api/agent/run", {"agent_id": ag, "input": prompt, "run_timeout": 60,
                                     "store": False, "temperature": 0}, timeout=70)
    except Exception as e:
        return {"spec": None, "why": "модель не ответила: " + _scrub(str(e)[:120])}
    text = ""
    for it in (res or {}).get("output", []):
        if isinstance(it, dict) and it.get("type") == "message":
            for c in it.get("content", []):
                if isinstance(c, dict) and c.get("type") == "output_text":
                    text += c.get("text", "")
    text = text or (res or {}).get("output_text", "")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {"spec": None, "why": _llm_error_human(res) or "модель не спроектировала вызов"}
    try:
        raw = json.loads(m.group(0))
    except Exception:
        return {"spec": None, "why": "спека от модели не разобралась"}

    method = str(raw.get("method") or "GET").upper()
    if method not in ("GET", "POST"):
        return {"spec": None, "why": "поддерживаются только GET и POST, модель предложила " + method[:12]}
    path = str(raw.get("path") or "/")
    if not path.startswith("/") or "://" in path:
        return {"spec": None, "why": "путь должен быть относительным и начинаться со слэша: " + path[:40]}
    act = re.sub(r"[^a-z0-9_]", "", str(raw.get("action") or "call").lower())[:24] or "call"
    params, seen = [], set()
    for p in (raw.get("params") or [])[:5]:
        if not isinstance(p, dict):
            continue
        nm = re.sub(r"[^a-z0-9_]", "", str(p.get("name") or "").lower())[:24]
        if not nm or nm in seen:
            continue
        seen.add(nm)
        params.append({"name": nm, "default": str(p.get("default") or "")[:160],
                       "required": bool(p.get("required")), "desc": str(p.get("desc") or "")[:80]})
    missing = [p["name"] for p in params if p["required"] and not p["default"]]
    if missing:
        return {"spec": None, "why": "нечем проверить: нет значений обязательных полей " + ", ".join(missing)}
    fixed = {re.sub(r"[^A-Za-z0-9_]", "", str(k))[:24]: str(v)[:120]
             for k, v in (raw.get("fixed") or {}).items()
             if isinstance(raw.get("fixed"), dict) and str(k).strip()}
    # Поле не может быть одновременно постоянным и заполняемым — иначе непонятно, что победит.
    params = [p for p in params if p["name"] not in fixed]
    return {"spec": {"action": act, "description": str(raw.get("description") or "")[:200],
                     "method": method, "path": path, "params": params, "fixed": fixed,
                     "kw": str(raw.get("kw") or "")[:300]}, "why": ""}


def _app_wrap_render(app_id, spec):
    """Код обёртки собираем МЫ по шаблону — как у программ и MCP."""
    slug = re.sub(r"[^a-z0-9_]", "_", str(app_id).split("/")[-1].lower())[:24].strip("_") or "app"
    name = "cap_" + slug + "_" + spec["action"]
    keymap, sig_parts, subs_parts = {}, [], []
    for p in spec["params"]:
        py = p["name"]
        keymap[py] = py
        sig_parts.append('%s: str = ""' % py)
        subs_parts.append('"%s": %s' % (py, py))
    # Как у MCP: демо-значения обязательных полей в рабочие дефолты НЕ уходят — иначе шаг с пустым
    # полем молча посчитает подставные данные и вернёт успех.
    runtime_def = {p["name"]: p["default"] for p in spec["params"] if not p["required"]}
    desc = (spec.get("description") or ("обращение к приложению " + str(app_id))).replace("\n", " ")
    desc += " Зови ЭТОТ эксперт — он сам обратится к приложению на этом устройстве."
    code = _APP_WRAP_TEMPLATE % {
        "NAME": name, "DESC": desc[:400], "SIG": ", ".join(sig_parts),
        "SUBS": ", ".join(subs_parts), "DEFAULTS": json.dumps(runtime_def, ensure_ascii=False),
        "KEYMAP": json.dumps(keymap, ensure_ascii=False),
        "REQUIRED": json.dumps([p["name"] for p in spec["params"] if p["required"]], ensure_ascii=False),
        "FIXED": json.dumps(spec.get("fixed") or {}, ensure_ascii=False),
        "REGFILE": _app_registry_file(app_id), "APP": str(app_id).replace('"', ""),
        "PATH": spec["path"], "METHOD": spec["method"]}
    return name, code, runtime_def


def _app_wrap_flow(app_id, purpose=""):
    """Полный путь «приложение → рабочий блок». В каталог пускает только живой прогон, и ответ
    обязан быть НЕПУСТЫМ: у приложения слишком легко получить вежливую страницу-заглушку вместо
    данных, а такой блок в процессе хуже, чем его отсутствие."""
    # Идемпотентность: если блок этого приложения уже есть — не гоняем модель и прогон повторно.
    # Триггер стоит на установке, а установку легко повторить (переустановил/перезапустил) — второй
    # прогон был бы платной тратой и лишним дублем.
    _slug = re.sub(r"[^a-z0-9_]", "_", str(app_id).split("/")[-1].lower())[:24].strip("_") or "app"
    try:
        g = api("/api/kv/get", {"key": "composer:catalog", "global": True})
        blocks = (json.loads(g.get("value")) if isinstance(g, dict) and g.get("value") else {}).get("blocks") or []

        def _same_app(b):
            if not isinstance(b, dict):
                return False
            o = b.get("origin") or {}
            if o.get("kind") == "app" and o.get("ref") == app_id:
                return True
            # блоки старше поля origin (searxng собран до backfill) узнаём по слагу в имени:
            # cap_<slug>_… — иначе повторная установка плодит дубль-блок того же приложения.
            return str(b.get("id") or "").startswith("cap_" + _slug + "_")
        exist = next((b for b in blocks if _same_app(b)), None)
        if exist:
            return {"ok": True, "app": app_id, "expert": exist.get("id"), "already": True,
                    "catalog": {"ok": True, "blocks": len(blocks)}}
    except Exception:
        pass
    info = _app_info(app_id)
    if not info.get("ok"):
        return {"ok": False, "stage": "app", "why": info.get("why")}
    sp = _app_wrap_spec(app_id, info, purpose)
    if not sp.get("spec"):
        return {"ok": False, "stage": "spec", "why": sp.get("why")}
    spec = sp["spec"]
    name, code, runtime_def = _app_wrap_render(app_id, spec)
    probe_args = {p["name"]: p["default"] for p in spec["params"] if p["default"]}
    sv = api("/api/expert/save", {"name": name, "code": code,
                                  "description": spec["description"][:200], "kwargs": runtime_def,
                                  "cspl": "fython", "global": True})
    if not (isinstance(sv, dict) and (sv.get("status") == "success" or sv.get("id"))):
        return {"ok": False, "stage": "save", "expert": name, "why": _scrub(str(sv)[:150])}
    probe = run_expert(name, probe_args, wait=240, glob=True)
    if isinstance(probe, str):
        try:
            probe = json.loads(probe)
        except Exception:
            try:
                import ast as _a
                probe = _a.literal_eval(probe)
            except Exception:
                probe = {"status": "error", "message": str(probe)[:200]}
    out = str((probe or {}).get("output", "")).strip() if isinstance(probe, dict) else ""
    if not (isinstance(probe, dict) and probe.get("status") == "success" and len(out) > 40):
        _cleanup_failed_wrap(name)
        return {"ok": False, "stage": "probe", "expert": name, "spec": spec,
                "why": str((probe or {}).get("message") or
                           ("приложение ответило пусто или слишком коротко: " + out[:80]))[:200]}
    added = _composer_catalog_add({
        "id": name, "kind": "app",
        "what": spec["description"] or ("обращение к " + str(app_id)),
        "params": {p["name"]: (p["desc"] or "") + (" (обязательно)" if p["required"] else "")
                   for p in spec["params"]},
        "defaults": runtime_def,
        "kw": " ".join([str(app_id), spec["action"], spec["kw"], spec["description"]])[:400],
        "origin": {"kind": "app", "ref": str(app_id)},
        "source": "installed"})
    return {"ok": True, "app": app_id, "expert": name, "spec": spec, "catalog": added,
            "sample": out[:300]}


_MCP_BUILTIN = {"fetch": "Загрузка веб-страницы в читаемый текст",
                "time": "Время и часовые пояса",
                "git": "Работа с git-репозиторием",
                "filesystem": "Чтение файлов в папке Загрузки"}


def _installed_inventory():
    """Что РЕАЛЬНО установлено на этом устройстве и что из этого уже стало блоком Композитора.

    Смысл: набор у каждого клиента свой, и человек не должен сам догадываться, почему
    поставленное приложение «не участвует» в автоматизациях. Разрыв показываем мы — и даём
    закрыть его в один клик. Отвечаем строго по факту с диска, догадок здесь быть не должно."""
    try:
        g = api("/api/kv/get", {"key": "composer:catalog", "global": True})
        blocks = (json.loads(g.get("value")) if isinstance(g, dict) and g.get("value") else {}).get("blocks") or []
    except Exception:
        blocks = []
    used = set()
    for b in blocks:
        if isinstance(b, dict):
            o = b.get("origin") or {}
            if o.get("kind") and o.get("ref"):
                used.add((o["kind"], str(o["ref"])))
    # Блоки, собранные ДО появления пометки origin, узнаём по имени эксперта — иначе предложим
    # сделать то, что уже сделано.
    legacy = " ".join(str(b.get("id") or "") for b in blocks if isinstance(b, dict))

    def _seen(kind, ref, slug):
        return (kind, str(ref)) in used or \
               ("cap_" + re.sub(r"[^a-z0-9_]", "_", str(slug).lower())[:24]) in legacy

    items, stale = [], []

    # ── приложения: реестр плагинов ──
    regdir = Path.home() / "extella-plugins" / "_registry"
    if regdir.is_dir():
        for f in sorted(regdir.glob("*.json")):
            try:
                man = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not (man.get("app") or {}).get("root"):
                continue                      # служебная карточка, а не установленное приложение
            aid = str(man.get("id") or f.stem)
            ui = man.get("ui") or {}
            items.append({"kind": "app", "ref": aid,
                          "title": str(man.get("name") or aid).split("/")[-1],
                          "note": "запущено" if ui.get("url")
                                  else "не запущено — запустите, чтобы сделать блок",
                          "ready": bool(ui.get("url")),
                          "used": _seen("app", aid, aid.split("/")[-1])})

    # ── MCP: встроенные (доступны всегда) + подключённые ──
    servers = dict(_MCP_BUILTIN)
    try:
        allow = json.loads((Path.home() / ".extella_mcp" / "allowlist.json").read_text(encoding="utf-8"))
        for k in (allow or {}):
            servers.setdefault(str(k), "подключённый MCP-сервер")
    except Exception:
        pass
    for sid, what in servers.items():
        items.append({"kind": "mcp", "ref": sid, "title": sid, "note": what, "ready": True,
                      "used": _seen("mcp", sid, sid)})

    # ── консольные программы: указатели, которые оставляют резолверы ──
    clidir = Path.home() / ".extella_cli"
    if clidir.is_dir():
        for f in sorted(clidir.iterdir()):
            if f.is_dir() or f.name.startswith(".") or f.suffix == ".log":
                continue
            # Указатель резолвера — это ПУТЬ К БИНАРНИКУ. В той же папке заводятся посторонние
            # файлы (логи докачки моделей), и без проверки они уезжали в список как «программы»,
            # а человеку предлагалось сделать блок из журнала.
            try:
                target = f.read_text(encoding="utf-8", errors="ignore").strip().split("\n")[0]
            except Exception:
                continue
            if not target or not os.path.exists(os.path.expanduser(target)):
                stale.append(f.name)   # указатель есть, программы нет — считаем, но не предлагаем
                continue
            items.append({"kind": "cli", "ref": f.name, "title": f.name,
                          "note": "программа на устройстве", "ready": True,
                          "used": _seen("cli", f.name, f.name)})

    return {"items": items, "unused": [i for i in items if not i["used"]],
            "blocks": len(blocks), "stale": stale}


def _make_one_block(kind, ref, purpose=""):
    """Один вход во все три цепочки обёртки — программа/сервис/приложение → блок."""
    if kind == "app":
        r = _app_wrap_flow(ref, purpose)
        return {"ok": r.get("ok"), "made": [r["expert"]] if r.get("ok") else [], "why": r.get("why", "")}
    if kind == "mcp":
        r = _mcp_wrap_flow(ref)
        return {"ok": r.get("ok"), "made": [w["expert"] for w in (r.get("wrapped") or [])],
                "why": r.get("why", "") or "; ".join("%s — %s" % (s.get("tool"), s.get("why"))
                                                     for s in (r.get("skipped") or [])[:3])}
    r = _cli_wrap_flow(ref, purpose)
    return {"ok": r.get("ok"), "made": [r["expert"]] if r.get("ok") else [], "why": r.get("why", "")}


def _binary_alive(path):
    p = os.path.expanduser(str(path or "").strip())
    return bool(p) and os.path.exists(p)


def _block_health(block):
    """Жива ли способность ПОД блоком. Смысл: блок может остаться в каталоге, а программа/сервер
    под ним — исчезнуть (у ghostscript brew снесли, а cap_ghostscript_compress_pdf_batch остался).
    Такой блок в процессе упадёт, и человек этого не ждёт.

    Проверяем по тому, на что эксперт ОПИРАЕТСЯ В КОДЕ, а не по имени — большинство блоков старше
    поля origin, и догадка по имени врёт. Возвращаем None, если блок не устройство-зависимый
    (наши поставочные wz_/kp_/svc_ — их наличие гарантирует установщик, здесь их не трогаем)."""
    bid = block.get("id") or ""
    o = block.get("origin") or {}
    devbound = bool(o.get("kind")) or block.get("source") == "installed" or bid.startswith("cap_")
    if not devbound:
        return None

    # Быстрый путь: у блока есть origin{kind,ref} — зависимость известна, код тянуть не нужно.
    # Новые блоки все с origin, так что проверка дешевеет со временем. Дорогой путь (чтение кода)
    # остаётся только для блоков старше поля origin.
    ok = o.get("kind")
    ref = o.get("ref")
    if ok == "cli" and ref:
        cands = ["/opt/homebrew/bin/" + ref, "/usr/local/bin/" + ref, "/usr/bin/" + ref]
        ptr = Path.home() / ".extella_cli" / ref
        if ptr.exists():
            try:
                cands.insert(0, ptr.read_text(encoding="utf-8", errors="ignore").strip().split("\n")[0])
            except Exception:
                pass
        alive = any(_binary_alive(c) for c in cands)
        return {"id": bid, "kind": "cli", "ref": ref, "alive": alive,
                "why": "" if alive else "программа «%s» больше не установлена на устройстве" % ref}
    if ok == "app" and ref:
        regf = re.sub(r"[^A-Za-z0-9]", "_", str(ref))
        reg = Path.home() / "extella-plugins" / "_registry" / (regf + ".json")
        return {"id": bid, "kind": "app", "ref": ref, "alive": reg.exists(),
                "why": "" if reg.exists() else "приложение удалено с устройства"}
    if ok == "mcp" and ref:
        if ref in _MCP_BUILTIN:
            return {"id": bid, "kind": "mcp", "ref": ref, "alive": True, "why": ""}
        try:
            allow = json.loads((Path.home() / ".extella_mcp" / "allowlist.json").read_text(encoding="utf-8"))
        except Exception:
            allow = {}
        alive = ref in (allow or {})
        return {"id": bid, "kind": "mcp", "ref": ref, "alive": alive,
                "why": "" if alive else "MCP-сервер «%s» больше не подключён" % ref}

    # Медленный путь: origin нет — определяем зависимость по коду обёртки.
    try:
        code = (api("/api/expert/get", {"name": bid, "global": True}) or {}).get("expert_code") or ""
    except Exception:
        return None
    if not code:
        # эксперта нет вовсе — блок ссылается в пустоту
        return {"id": bid, "kind": o.get("kind") or "?", "alive": False,
                "why": "эксперт-обёртка удалён — блок ссылается в пустоту"}

    # ── CLI: обёртка читает указатель ~/.extella_cli/<tool>; проверяем цель на диске ──
    m = re.search(r"\.extella_cli/([A-Za-z0-9_.-]+)", code)
    if m:
        tool = m.group(1)
        ptr = Path.home() / ".extella_cli" / tool
        target = ""
        if ptr.exists():
            try:
                target = ptr.read_text(encoding="utf-8", errors="ignore").strip().split("\n")[0]
            except Exception:
                target = ""
        # запасной путь резолвится по тем же каталогам, что и сама обёртка (PATH листенера урезан,
        # поэтому проверяем абсолютные пути, а не полагаемся на which).
        cands = [target, "/opt/homebrew/bin/" + tool, "/usr/local/bin/" + tool, "/usr/bin/" + tool]
        alive = any(_binary_alive(c) for c in cands)
        if not alive:
            return {"id": bid, "kind": "cli", "ref": tool, "alive": False,
                    "why": "программа «%s» больше не установлена на устройстве" % tool}
        return {"id": bid, "kind": "cli", "ref": tool, "alive": True, "why": ""}

    # ── приложение: обёртка читает _registry/<file>.json → ui.url ──
    m = re.search(r"_registry/([A-Za-z0-9_]+)\.json", code)
    if m:
        reg = Path.home() / "extella-plugins" / "_registry" / (m.group(1) + ".json")
        if not reg.exists():
            return {"id": bid, "kind": "app", "ref": m.group(1), "alive": False,
                    "why": "приложение удалено с устройства"}
        # установлено, но не запущено — это НЕ смерть блока, а рабочее состояние (запусти и пойдёт)
        return {"id": bid, "kind": "app", "ref": m.group(1), "alive": True, "why": ""}

    # ── MCP: встроенные всегда живы; подключённый — по аллоулисту ──
    m = re.search(r'"server":\s*"([A-Za-z0-9_.-]+)"', code)
    if m:
        srv = m.group(1)
        if srv in _MCP_BUILTIN:
            return {"id": bid, "kind": "mcp", "ref": srv, "alive": True, "why": ""}
        try:
            allow = json.loads((Path.home() / ".extella_mcp" / "allowlist.json").read_text(encoding="utf-8"))
        except Exception:
            allow = {}
        if srv not in (allow or {}):
            return {"id": bid, "kind": "mcp", "ref": srv, "alive": False,
                    "why": "MCP-сервер «%s» больше не подключён" % srv}
        return {"id": bid, "kind": "mcp", "ref": srv, "alive": True, "why": ""}

    # локальная модель и прочее без явной внешней зависимости — считаем живым (проверять нечем,
    # выдумывать не будем).
    return {"id": bid, "kind": o.get("kind") or "other", "alive": True, "why": ""}


def _catalog_broken():
    """Битые блоки каталога — те, под которыми пропала способность. Только чтение, ничего не
    удаляем: решение убирать принимает человек."""
    try:
        g = api("/api/kv/get", {"key": "composer:catalog", "global": True})
        blocks = (json.loads(g.get("value")) if isinstance(g, dict) and g.get("value") else {}).get("blocks") or []
    except Exception:
        blocks = []
    broken = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        h = _block_health(b)
        if h and not h["alive"]:
            h["what"] = str(b.get("what") or "")[:100]
            broken.append(h)
    return {"broken": broken, "checked": len(blocks)}


def _device_status():
    """ЕДИНАЯ картина устройства (по слову Анвара 20.07): свести «что установлено» (опись — читает
    реестр приложений тулбара + аллоулист MCP + указатели программ) и «что работает и не упадёт»
    (здоровье блоков). Раньше это были три разрозненных списка — установленное, битое, мёртвые
    указатели; человеку приходилось складывать их в голове.

    У каждой способности ОДНО состояние:
      working     — установлена, есть рабочий кубик;
      installable — установлена и готова, кубика ещё нет (сделать/подключить);
      not_running — приложение стоит, но не запущено;
      broken      — кубик есть, а способность под ним исчезла (нужно внимание);
      missing     — числится установленной, но на диске/в подключении её нет (переустановить).
    """
    inv = _installed_inventory()
    broken = _catalog_broken()["broken"]
    broken_by_ref = {(b.get("kind"), str(b.get("ref"))): b for b in broken}

    rows = []
    seen = set()
    for it in inv["items"]:
        key = (it["kind"], it["ref"])
        seen.add(key)
        if it["used"]:
            # кубик есть — рабочий или сломанный
            if key in broken_by_ref:
                rows.append({**it, "state": "broken", "detail": broken_by_ref[key]["why"],
                             "block": broken_by_ref[key]["id"]})
            else:
                rows.append({**it, "state": "working", "detail": "работает в автоматизациях"})
        elif not it["ready"]:
            rows.append({**it, "state": "not_running", "detail": it.get("note") or "не запущено"})
        else:
            rows.append({**it, "state": "installable", "detail": "можно подключить к автоматизациям"})

    # битые кубики, чья способность НЕ числится установленной (случай ghostscript: brew сняли,
    # в описи её уже нет, а кубик в каталоге остался) — их не покрыла опись, добавляем отдельно.
    for (kind, ref), b in broken_by_ref.items():
        if (kind, ref) in seen:
            continue
        rows.append({"kind": kind, "ref": ref, "title": ref, "used": True, "ready": False,
                     "state": "broken", "detail": b["why"], "block": b["id"]})

    # мёртвые указатели программ (есть ссылка, нет бинаря) без кубика — тихий шум, но честно считаем
    for name in inv.get("stale") or []:
        if ("cli", name) in seen:
            continue
        rows.append({"kind": "cli", "ref": name, "title": name, "used": False, "ready": False,
                     "state": "missing", "detail": "числится установленной, но на диске её нет"})

    summary = {}
    for r in rows:
        summary[r["state"]] = summary.get(r["state"], 0) + 1
    return {"rows": rows, "summary": summary, "blocks": inv["blocks"]}


def _block_remove(bid):
    """Убрать блок из каталога и его эксперта-обёртку. Только для устройство-зависимых обёрток
    (cap_*/origin/installed) — поставочные эксперты так не трогаем."""
    try:
        g = api("/api/kv/get", {"key": "composer:catalog", "global": True})
        cur = json.loads(g.get("value")) if isinstance(g, dict) and g.get("value") else {}
    except Exception:
        cur = {}
    blocks = cur.get("blocks") if isinstance(cur, dict) else None
    if not isinstance(blocks, list):
        return {"ok": False, "why": "каталог не прочитан"}
    tgt = next((b for b in blocks if isinstance(b, dict) and b.get("id") == bid), None)
    if not tgt:
        return {"ok": False, "why": "такого блока нет в каталоге"}
    o = tgt.get("origin") or {}
    if not (o.get("kind") or tgt.get("source") == "installed" or str(bid).startswith("cap_")):
        return {"ok": False, "why": "это поставочный блок, не устройство-зависимый — убирать не будем"}
    cur["blocks"] = [b for b in blocks if not (isinstance(b, dict) and b.get("id") == bid)]
    r = api("/api/kv/set", {"key": "composer:catalog", "value": json.dumps(cur, ensure_ascii=False),
                            "description": "composer catalog", "global": True})
    if isinstance(r, dict) and r.get("status") == "error":
        return {"ok": False, "why": _scrub(str(r)[:120])}
    # ЭКСПЕРТА НЕ УДАЛЯЕМ. Убрать блок = убрать ссылку из каталога, этого достаточно: Композитор
    # больше не предложит падающий блок. Сам эксперт-обёртка может быть ПОСТАВОЧНОЙ способностью
    # (cap_ghostscript_* лежит файлом в паке) — удаление снесло бы её, ровно как я по ошибке снёс
    # pandoc 20.07. Осиротевший эксперт без блока безвреден, а при возврате программы блок
    # пересобирается заново.
    return {"ok": True, "blocks": len(cur["blocks"])}


def _llm_error_human(res):
    """Ошибка модели → человеческий ответ. Business-пользователь НИКОГДА не должен видеть сырой
    JSON платформы с ссылками на langchain — так у Гульжан на тесте вылезло «401 Incorrect API key».
    Ей это не говорит ничего, а вопрос решается в два клика."""
    blob = json.dumps(res, ensure_ascii=False) if isinstance(res, dict) else str(res)
    low = blob.lower()
    if "401" in low and ("api key" in low or "apikey" in low or "authentication" in low):
        return ("Помощник не может обратиться к модели: у выбранного агента нет действующего ключа. "
                "Нажмите «🤖 Выбрать агента» вверху и выберите агента, который работает на вашем аккаунте "
                "(или создайте нового — это копия платформенной Qwen). Если агентов нет, "
                "обратитесь к администратору Extella.")
    if "429" in low or "rate limit" in low or "quota" in low:
        return "Модель сейчас перегружена или исчерпан лимит запросов. Попробуйте через минуту."
    if "timeout" in low or "timed out" in low:
        return "Модель не ответила вовремя. Повторите вопрос — обычно помогает."
    if "not found" in low and "agent" in low:
        return ("Выбранный агент не найден на вашем аккаунте. Нажмите «🤖 Выбрать агента» "
                "и выберите существующего.")
    return ""


def _agent_output_text(res):
    """Единообразно достать текст агента из Responses-подобного ответа платформы."""
    text = ""
    try:
        for item in (res or {}).get("output", []):
            if isinstance(item, dict) and item.get("type") == "message":
                for c in (item.get("content") or []):
                    if isinstance(c, dict) and c.get("type") == "output_text":
                        text += str(c.get("text") or "")
    except Exception:
        pass
    return text or (str((res or {}).get("output_text") or "") if isinstance(res, dict) else "")


def _chat_should_retry(res, text=""):
    """Один повтор только для временного/пустого сбоя, но не для ключей, прав и отсутствующего агента."""
    if text:
        return False
    if not isinstance(res, dict):
        return True
    low = str(res).lower()
    terminal = (("401" in low and ("api key" in low or "authentication" in low))
                or ("not found" in low and "agent" in low)
                or "does not belong" in low or "forbidden" in low or "permission denied" in low)
    return not terminal


def _run_chat_agent(payload):
    """Чат не должен падать от единичного флапа платформы; повтор ограничен двумя попытками."""
    res, text = {}, ""
    for attempt in range(2):
        raw = api("/api/agent/run", payload)
        res = raw if isinstance(raw, dict) else {"status": "error", "message": "empty platform response"}
        text = _agent_output_text(res)
        if not _chat_should_retry(res, text):
            return res, text, attempt + 1
    return res, text, 2


def _run_error_human(res):
    """Ответ оркестратора → {code, message, remedy, stage}. Возвращает None, если ошибки нет."""
    if not isinstance(res, dict) or str(res.get("status", "")) not in ("error", "failed"):
        return None
    blob = json.dumps(res, ensure_ascii=False)
    stage = str(res.get("failed_stage") or "")
    for needle, code, msg, remedy in RUN_ERRORS:
        if needle.lower() in blob.lower():
            out = {"code": code, "message": msg, "remedy": remedy}
            if stage:
                out["stage"] = stage
            return out
    detail = str(res.get("detail") or res.get("message") or "")[:200]
    return {"code": "stage_failed", "stage": stage,
            "message": ("Шаг «" + stage + "» не отработал") if stage else "Процесс не дал результата",
            "remedy": "Загляните в «Историю» — там видно, на каком шаге остановилось. "
                      "Если данные и настройки в порядке, опишите правку в чате внизу: "
                      "Строитель пересоберёт этот шаг.",
            "detail": detail}


def _rules_payload(s):
    """F2: полезная нагрузка rules_json прогона — текстовые правила (их читают кодогенные стадии)
    + скомпилированные структурные фильтры {field,op,value} (их детерминированно применяет оркестратор).
    A6: текст берём из источника истины (платформа), при её молчании — из кэша сессии."""
    return list(_proc_rules_read(s)["rules"]) + [r for r in (s.get("rules_struct") or []) if isinstance(r, dict)]


def _report_spec_from_words(current, phrase, fields, agent_id=None):
    """Правка ВИДА отчёта словами: «добавь разрез по площадкам», «назови иначе», «убери этот блок».
    Qwen проектирует СПЕКУ (не код) — как в CSPL Studio. Возвращаем {spec, changes, rejected}.

    Гейт честности: разрез можно строить ТОЛЬКО по колонке, которая реально есть в данных.
    Модель охотно выдумывает поля — выдуманные отбрасываем и НАЗЫВАЕМ их владельцу,
    иначе он увидит отчёт без обещанного разреза и не поймёт почему."""
    ag = agent_id or qwen_agent()
    if not ag:
        return {"spec": None, "why": "нет агента для проектирования отчёта"}
    cur = json.dumps(current or {}, ensure_ascii=False)
    prompt = ("Ты проектируешь ВИД отчёта по просьбе владельца. Верни ТОЛЬКО JSON-спеку без пояснений:\n"
              '{"report":"<заголовок>","subtitle":"<подзаголовок или пусто>",'
              '"headline":{"metric":"count"|"sum","field":"<колонка для sum>","label":"<подпись под числом>"},'
              '"views":[{"group_by":"<колонка>","title":"<название раздела>"}],'
              '"style":{"accent":"#RRGGBB","brand_name":"<имя в шапке>","footer":"<подпись внизу>"},'
              '"format":"pdf"|"docx"|"pptx"|"both"|"all"}\n\n'
              "ПРАВИЛА:\n"
              "- format: pdf — документ отправляют как есть; docx — владелец будет его ДОРАБАТЫВАТЬ "
              "(«в ворде», «чтобы дописать», «редактируемый»); pptx — это будут ПОКАЗЫВАТЬ людям "
              "(«презентация», «слайды», «на совет директоров»); both — pdf+docx; all — все три;\n"
              "- group_by и headline.field — ТОЛЬКО из списка колонок ниже, ничего не выдумывай;\n"
              "- сохраняй то, что владелец не просил менять (текущая спека дана);\n"
              "- разрезов не больше четырёх;\n"
              "- язык подписей — язык просьбы владельца.\n\n"
              "Колонки данных: " + (", ".join(str(f) for f in (fields or [])[:30]) or "(неизвестны)") + "\n"
              "Текущая спека: " + cur[:800] + "\n"
              "Просьба владельца: " + str(phrase)[:400])
    try:
        res = api("/api/agent/run", {"agent_id": ag, "input": prompt, "run_timeout": 60,
                                     "store": False, "temperature": 0}, timeout=70)
    except Exception as e:
        return {"spec": None, "why": "модель не ответила: " + _scrub(str(e)[:120])}
    text = ""
    for it in (res or {}).get("output", []):
        if isinstance(it, dict) and it.get("type") == "message":
            for c in it.get("content", []):
                if isinstance(c, dict) and c.get("type") == "output_text":
                    text += c.get("text", "")
    text = text or (res or {}).get("output_text", "")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        # Чаще всего так бывает, когда просят разрез по тому, чего в данных нет: модель
        # отвечает прозой вместо спеки. Владельцу нужен ПОЛЕЗНЫЙ ответ — что у него есть,
        # а не «не вернула спеку».
        _av = ", ".join(str(f) for f in (fields or [])[:12])
        return {"spec": None,
                "why": ("не получилось собрать такой отчёт. В данных процесса есть поля: " + _av +
                        ". Разрез можно построить только по ним.") if _av
                       else "не получилось собрать такой отчёт — колонки данных пока неизвестны "
                            "(процесс ещё ни разу не отработал или не привязан источник)"}
    try:
        raw = json.loads(m.group(0))
    except Exception:
        return {"spec": None, "why": "спека от модели не разобралась"}

    fset = {str(f) for f in (fields or [])}
    spec, rejected = {}, []
    spec["report"] = str(raw.get("report") or (current or {}).get("report") or "Отчёт")[:120]
    if raw.get("subtitle") is not None:
        spec["subtitle"] = str(raw.get("subtitle"))[:240]
    hl = raw.get("headline") if isinstance(raw.get("headline"), dict) else {}
    if hl:
        _h = {"metric": "sum" if str(hl.get("metric")) == "sum" else "count",
              "label": str(hl.get("label") or "")[:60]}
        if _h["metric"] == "sum":
            if str(hl.get("field")) in fset:
                _h["field"] = str(hl["field"])
            else:
                rejected.append("поле для суммы «" + str(hl.get("field")) + "» — такого нет в данных")
                _h["metric"] = "count"
        spec["headline"] = _h
    views = []
    for v in (raw.get("views") or [])[:4]:
        if not isinstance(v, dict):
            continue
        g = str(v.get("group_by") or "")
        if fset and g not in fset:
            rejected.append("разрез по «" + g + "» — такой колонки нет в данных")
            continue
        views.append({"group_by": g, "title": str(v.get("title") or g)[:60]})
    if views:
        spec["views"] = views
    _f = str(raw.get("format") or (current or {}).get("format") or "pdf").lower()
    spec["format"] = _f if _f in ("pdf", "docx", "pptx", "both", "all") else "pdf"
    st = raw.get("style") if isinstance(raw.get("style"), dict) else {}
    style = dict((current or {}).get("style") or {})
    if re.match(r"^#[0-9a-fA-F]{6}$", str(st.get("accent") or "")):
        style["accent"] = str(st["accent"])
    for k in ("brand_name", "footer"):
        if st.get(k) is not None:
            style[k] = str(st[k])[:80]
    if style:
        spec["style"] = style

    # что именно поменялось — владельцу показываем изменения, а не всю спеку
    changes = []
    old = current or {}
    if spec.get("report") != old.get("report"):
        changes.append("заголовок: «" + spec["report"] + "»")
    if spec.get("subtitle") != old.get("subtitle") and spec.get("subtitle"):
        changes.append("подзаголовок обновлён")
    ov = [v.get("group_by") for v in (old.get("views") or [])]
    nv = [v.get("group_by") for v in (spec.get("views") or [])]
    for g in nv:
        if g not in ov:
            changes.append("+ разрез по «" + str(g) + "»")
    for g in ov:
        if g not in nv:
            changes.append("− убран разрез по «" + str(g) + "»")
    if spec.get("format") != (old.get("format") or "pdf"):
        changes.append("формат: " + {"pdf": "PDF", "docx": "Word (можно дорабатывать)",
                                     "pptx": "презентация (слайды с диаграммами)",
                                     "both": "PDF и Word", "all": "PDF, Word и презентация"}.get(spec["format"], spec["format"]))
    if (spec.get("style") or {}).get("accent") != (old.get("style") or {}).get("accent"):
        changes.append("цвет акцента")
    if (spec.get("headline") or {}) != (old.get("headline") or {}) and spec.get("headline"):
        changes.append("главное число: " + spec["headline"].get("label", ""))
    return {"spec": spec, "changes": changes, "rejected": rejected, "why": ""}


def _compile_rule_filters(rules, known=None, agent_id=None):
    """Правило словами → машинный фильтр {field,op,value}. Компилируем ОДИН раз при записи,
    на прогонах оркестратор применяет структуры детерминированно (не «как поймёт модель»).
    Возвращает {filters, why} — why обязателен: раньше любой сбой молча давал пустой список,
    и правило тихо превращалось из жёсткого в мягкое. Немой отказ здесь недопустим."""
    if not rules:
        return {"filters": [], "why": ""}
    ag = agent_id or qwen_agent()
    if not ag:
        return {"filters": [], "why": "нет агента для компиляции"}
    prompt = ("Преврати правила владельца бизнес-процесса в машинные фильтры записей данных.\n"
              "Верни ТОЛЬКО JSON-массив без пояснений: "
              '[{"field":"<имя поля данных>","op":">"|">="|"<"|"<="|"=="|"contains","value":<число или строка>}]\n'
              "Бери только правила-фильтры (какие записи показывать). Сортировку, оформление, "
              "доставку, сроки — пропускай. Ничего не фильтрует — верни [].\n"
              + ("Известные поля данных: " + ", ".join(list(known or [])[:12]) + "\n" if known else "")
              + "Правила:\n- " + "\n- ".join(rules))
    try:
        res = api("/api/agent/run", {"agent_id": ag, "input": prompt, "run_timeout": 60,
                                     "store": False, "temperature": 0}, timeout=70)
    except Exception as e:
        return {"filters": [], "why": "модель не ответила: " + _scrub(str(e)[:120])}
    if isinstance(res, dict) and res.get("status") == "error":
        return {"filters": [], "why": "модель не ответила: " + _scrub(str(res.get("message"))[:120])}
    text = ""
    for it in (res or {}).get("output", []):
        if isinstance(it, dict) and it.get("type") == "message":
            for c in it.get("content", []):
                if isinstance(c, dict) and c.get("type") == "output_text":
                    text += c.get("text", "")
    text = text or (res or {}).get("output_text", "")
    if not text.strip():
        return {"filters": [], "why": "модель вернула пустой ответ"}
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return {"filters": [], "why": "в ответе модели нет JSON-массива"}
    try:
        raw = json.loads(m.group(0))
    except Exception:
        return {"filters": [], "why": "JSON модели не разобрался"}
    out = []
    for v in raw if isinstance(raw, list) else []:
        if isinstance(v, dict) and str(v.get("field", "")).strip() \
           and str(v.get("op", "")) in (">", ">=", "<", "<=", "==", "contains"):
            out.append({"field": str(v["field"]).strip()[:60], "op": str(v["op"]), "value": v.get("value")})
    if raw and not out:
        return {"filters": [], "why": "модель вернула фильтры в неизвестном формате"}
    if out:
        return {"filters": out[:10], "why": ""}
    # Пусто — это НЕ обязательно сбой. Чаще всего правило просто не к чему применить: в данных
    # процесса нет подходящего поля. Владельцу нужен именно этот ответ, а не тишина — иначе он
    # уверен, что поставил жёсткий фильтр, а фильтра нет.
    if known:
        return {"filters": [],
                "why": "правило не стало жёстким фильтром: в данных процесса есть поля " +
                       ", ".join(list(known)[:8]) + " — подходящего среди них нет. "
                       "Правило останется мягким: модель учтёт его при сборке результата"}
    return {"filters": [], "why": "правила не описывают условие отбора записей — останутся мягкими"}


def _recipients(s):
    """Список каналов-получателей результата процесса (кабина «Настройка» → несколько получателей).
    Источник истины — s['recipients'] (список каналов). Обратная совместимость: если списка нет,
    берём одиночный schedule.deliver. Возвращает нормализованные ключи каналов (telegram/email/…)."""
    r = s.get("recipients")
    if isinstance(r, list) and r:
        out, seen = [], set()
        for x in r:
            k = str(x).strip().lower()
            if k and re.match(r"^[a-z0-9_]+$", k) and k not in seen:
                seen.add(k); out.append(k)
        return out
    d = str((s.get("schedule") or {}).get("deliver") or "").strip().lower()
    return [d] if d and re.match(r"^[a-z0-9_]+$", d) else []


def _sched_kv(sid):
    """Читает расписание процесса из общего KV (sched:<sid>)."""
    g = api("/api/kv/get", {"key": "sched:" + sid})
    if not isinstance(g, dict) or not g.get("value"):
        return None
    try:
        return json.loads(g["value"])
    except Exception:
        return None


def _sched_kv_batch(sids):
    """Параллельно читает sched:<sid> для многих сессий. Иначе N сетевых round-trip к платформе
    последовательно (13 сессий → ~40с) — панель «Автоматизации» тормозит. С пулом — секунды."""
    from concurrent.futures import ThreadPoolExecutor
    sids = [s for s in sids if s]
    out = {}
    if not sids:
        return out
    def _one(sid):
        try:
            return sid, _sched_kv(sid)
        except Exception:
            return sid, None
    try:
        with ThreadPoolExecutor(max_workers=min(16, len(sids))) as ex:
            for sid, v in ex.map(_one, sids):
                out[sid] = v
    except Exception:
        for sid in sids:
            out[sid] = None
    return out


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


def _source_schema(out):
    """Схема источника = отсортированный список колонок из pull-вывода (out.columns или ключи первой строки
    preview). Нужна как ОТПЕЧАТОК структуры — чтобы поймать дрифт (источник поменял/убрал колонки)."""
    if not isinstance(out, dict):
        return []
    cols = out.get("columns")
    if isinstance(cols, list) and cols:
        return sorted(str(c) for c in cols)
    prev = out.get("preview") or out.get("rows_preview") or out.get("sample")
    if isinstance(prev, list) and prev and isinstance(prev[0], dict):
        return sorted(str(k) for k in prev[0].keys())
    return []


def _schema_drift(baseline, current):
    """Дрифт структуры: сравнение отпечатка колонок. removed — исчезли (ЛОМАЮЩИЙ дрифт: процесс считает по
    несуществующим колонкам → мусор); added — новые (мягкий). Пустой baseline/current → дрифта нет (нечего сравнивать)."""
    b, c = set(baseline or []), set(current or [])
    if not b or not c:
        return {"drift": False}
    removed, added = sorted(b - c), sorted(c - b)
    return {"drift": bool(removed or added), "removed": removed, "added": added,
            "breaking": bool(removed)}


# ─────────── AC-05: АДАПТЕР ИСТОЧНИКА (выгрузка клиента → поля процесса) ───────────
# Клиент переименовал колонку в выгрузке — и процесс либо падает, либо (хуже) считает по чужой
# схеме и молча выдаёт мусор. Адаптер — ЯВНЫЙ именованный маппинг с версией: поймали дрифт →
# предложили адаптер → человек подтвердил → процесс поехал дальше. Хранится на платформе
# (переживает потерю устройства), в сессии — кэш; наружу уходит человеку понятный список пар.


def _adapter_key(sid):
    return "adapter:" + str(sid)


def _adapter_load(sid):
    """Все версии адаптера процесса: {"versions":[{v,map,columns,at,by}], "active": <v>}."""
    g = api("/api/kv/get", {"key": _adapter_key(sid)})
    if isinstance(g, dict) and g.get("value"):
        try:
            d = json.loads(g["value"])
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return {"versions": [], "active": 0}


def _adapter_active(sid):
    """Действующий адаптер {"map": {...}} или {} — именно он уходит в оркестратор."""
    d = _adapter_load(sid)
    for v in d.get("versions") or []:
        if v.get("v") == d.get("active"):
            return {"map": v.get("map") or {}, "v": v.get("v")}
    return {}


def _adapter_save(sid, field_map, columns, note=""):
    """Новая ВЕРСИЯ адаптера (не перезапись): историю видно, откат возможен."""
    d = _adapter_load(sid)
    nv = max([int(v.get("v") or 0) for v in (d.get("versions") or [])] or [0]) + 1
    d.setdefault("versions", []).append({"v": nv, "map": field_map or {},
                                         "columns": sorted(columns or []),
                                         "at": datetime.now(timezone.utc).isoformat(), "note": note})
    d["versions"] = d["versions"][-10:]
    d["active"] = nv
    r = api("/api/kv/set", {"key": _adapter_key(sid), "value": json.dumps(d, ensure_ascii=False),
                            "description": "adapter " + str(sid)})
    ok = not (isinstance(r, dict) and r.get("status") == "error")
    return {"ok": ok, "v": nv if ok else 0, "why": "" if ok else str(r)[:120]}


def _adapter_propose(current_cols, target_cols):
    """Предложение маппинга: колонки НОВОЙ выгрузки → поля, которые процесс уже знает.
    Сначала точные и регистронезависимые совпадения (детерминированно, без модели), остаток —
    Qwen. Возвращает {map, unmatched, why} — с причиной, если предложить нечего."""
    cur = [str(c).strip() for c in (current_cols or []) if str(c).strip()]
    tgt = [str(c).strip() for c in (target_cols or []) if str(c).strip()]
    if not cur or not tgt:
        return {"map": {}, "unmatched": cur, "why": "нечего сопоставлять: неизвестны колонки выгрузки или поля процесса"}
    fmap, left_cur = {}, []
    tgt_by_fold = {c.casefold(): c for c in tgt}
    for c in cur:
        hit = tgt_by_fold.get(c.casefold())
        if hit:
            fmap[c] = hit          # совпало точно — модель здесь не нужна
        else:
            left_cur.append(c)
    left_tgt = [c for c in tgt if c not in fmap.values()]
    if left_cur and left_tgt:
        ag = qwen_agent()
        if ag:
            prompt = ("Сопоставь колонки новой выгрузки с полями, которые ожидает процесс.\n"
                      "Верни ТОЛЬКО JSON-объект {\"<колонка выгрузки>\": \"<поле процесса>\"} без пояснений.\n"
                      "Сопоставляй по смыслу. Колонку, для которой подходящего поля нет, НЕ включай.\n"
                      "Колонки выгрузки: " + ", ".join(left_cur[:30]) + "\n"
                      "Поля процесса: " + ", ".join(left_tgt[:30]))
            try:
                res = api("/api/agent/run", {"agent_id": ag, "input": prompt, "run_timeout": 60,
                                             "store": False, "temperature": 0}, timeout=70)
                text = ""
                for it in (res or {}).get("output", []):
                    if isinstance(it, dict) and it.get("type") == "message":
                        for c in it.get("content", []):
                            if isinstance(c, dict) and c.get("type") == "output_text":
                                text += c.get("text", "")
                text = text or (res or {}).get("output_text", "")
                m = re.search(r"\{.*\}", text, re.S)
                for k, v in (json.loads(m.group(0)) if m else {}).items():
                    if str(k) in left_cur and str(v) in left_tgt:
                        fmap[str(k)] = str(v)
            except Exception:
                pass
    unmatched = [c for c in cur if c not in fmap]
    return {"map": fmap, "unmatched": unmatched,
            "why": "" if fmap else "ни одна колонка новой выгрузки не сопоставилась с полями процесса"}


def _run_source(kind, mode, sid="", source_key="", timeout=120):
    """Запускает эксперт-источник wz_source_<kind> на ХОСТИНГЕ (пиннинг HOST_TARGET):
    mode='validate' проверка доступа; mode='pull' тянет данные и кладёт в стор под source_key.
    Возвращает dict результата эксперта ({ok, ...})."""
    if isinstance(kind, str) and kind.startswith("gen:"):
        # генеративный источник: исполнитель wz_source_gen_run по gen_id (validate→verify)
        return _run_gen_source(kind[4:], ("verify" if mode == "validate" else mode), sid, source_key)
    exp = "wz_source_" + _source_kindkey(kind)
    rr = api("/api/expert/run", {"expert_name": exp, "global": True, "target": HOST_TARGET,
                                 "params": {"api_token": CONFIG["auth_token"], "client": CLIENT_ID,
                                            "mode": mode, "sid": sid, "source_key": source_key}}, timeout)
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


def _gen_source_code(spec):
    """Генеративный коннектор: Qwen пишет fetch(secret) под описанный источник. Возвращает (code, err).
    Секреты бери ТОЛЬКО из аргумента secret (не хардкодить); только requests; исполнится в песочнице on-device."""
    ag = qwen_agent()
    if not ag:
        return None, "нет доступного Qwen-агента (keyless LLM offline)"
    desc = str(spec.get("description", ""))[:1500]
    endpoint = str(spec.get("endpoint", ""))[:400]
    auth = str(spec.get("auth_kind", "none"))[:60]
    row_hint = str(spec.get("row_hint", ""))[:400]
    # конкретная инструкция по авторизации (oauth_refresh → система сама обновит secret['token'] до вызова fetch)
    _auth_instr = {
        "none": "авторизация не нужна.",
        "bearer": "добавь заголовок Authorization: 'Bearer ' + secret['token'].",
        "oauth_refresh": "добавь заголовок Authorization: 'Bearer ' + secret['token'] (токен уже свежий — система обновила его до вызова fetch). Прочие нужные поля (например developer_token) тоже бери из secret.",
        "header": "передай ключ в заголовке со значением secret['api_key'] (имя заголовка из описания, по умолчанию X-Api-Key).",
        "query": "передай ключ как query-параметр URL со значением secret['api_key'] (имя параметра из описания, по умолчанию api_key).",
    }.get(auth, "авторизация: " + auth)
    prompt = (
        "Сгенерируй Python-функцию-фетчер источника данных под конкретный API. СТРОГО соблюдай:\n"
        "- Сигнатура РОВНО: def fetch(secret: dict) -> list\n"
        "- Возвращает список ПЛОСКИХ dict (строки таблицы); вложенные поля разворачивай в плоские ключи.\n"
        "- Для HTTP используй ТОЛЬКО библиотеку requests. НИКАКИХ os/subprocess/open/eval/файлов/сокетов.\n"
        "- Ключи/токены бери ТОЛЬКО из аргумента secret. НЕ хардкодь секреты. " + _auth_instr + "\n"
        "- Обрабатывай ошибки HTTP (raise_for_status), таймаут 45 секунд.\n"
        "Выведи ТОЛЬКО исполняемый python-код (import requests + def fetch), без пояснений и без markdown-ограждений.\n\n"
        "ИСТОЧНИК: " + desc + "\nЭндпоинт: " + endpoint + "\nОжидаемые поля строки: " + row_hint
    )
    try:
        res = api("/api/agent/run", {"agent_id": ag, "input": prompt, "run_timeout": 120,
                                     "store": False, "temperature": 0}, 140)
        text = ""
        for it in (res or {}).get("output", []):
            if isinstance(it, dict) and it.get("type") == "message":
                for c in it.get("content", []):
                    if isinstance(c, dict) and c.get("type") == "output_text":
                        text += c.get("text", "")
        text = text or (res or {}).get("output_text", "")
    except Exception as e:
        return None, "генерация не удалась: " + str(e)[:120]
    m = re.search(r"```(?:python)?\s*(.+?)```", text, re.S)
    code = (m.group(1) if m else text).strip()
    if "def fetch" not in code:
        return None, "модель не вернула функцию fetch"
    return code, None


def _run_gen_source(gen_id, mode, sid="", source_key="", limit=0):
    """Запускает исполнитель генеративного источника wz_source_gen_run на ХОСТИНГЕ (AST-гард + песочница
    + инъекция секрета из сейфа — всё на устройстве). mode=verify → preview; mode=pull → укладка в стор."""
    rr = api("/api/expert/run", {"expert_name": "wz_source_gen_run", "global": True, "target": HOST_TARGET,
                                 "params": {"api_token": CONFIG["auth_token"], "client": CLIENT_ID, "mode": mode,
                                            "gen_id": gen_id, "sid": sid, "source_key": source_key,
                                            "api_base": BASE, "limit": limit}}, 120)
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


def _trust_check(task, sample, fields, runs=3, golden=None):
    """Слой доверия, срез 1: воспроизводимость БЕЗ разметки. Гоняет судящий промпт N раз на выборке
    (keyless Qwen), сравнивает по полям — где ответы расходятся, определение размыто. Не нужен эталон:
    достаточно, чтобы модель согласилась сама с собой. Возвращает светофор по полям + примеры расхождений."""
    ag = qwen_agent()
    if not ag:
        return {"ok": False, "err": "нет доступного Qwen-агента (keyless LLM offline)"}
    n = len(sample)
    items = "\n".join("%d) %s" % (i + 1, str(s)[:300]) for i, s in enumerate(sample))
    schema = ", ".join('"%s":<...>' % f for f in fields)
    prompt = (str(task)[:1500] + "\n\nВерни СТРОГО JSON-массив, по одному объекту на строку, поля: "
              '{"id":<число>, ' + schema + "}. Только JSON, без пояснений.\n\nСТРОКИ:\n" + items)

    def run_once():
        # ретрай: платформенный Qwen иногда отдаёт пустой вывод — вторая попытка почти всегда спасает
        for _attempt in range(2):
            try:
                res = api("/api/agent/run", {"agent_id": ag, "input": prompt, "temperature": 1.0,
                                             "store": False, "run_timeout": 90}, 120)
                text = ""
                for it in (res or {}).get("output", []):
                    if isinstance(it, dict) and it.get("type") == "message":
                        for c in it.get("content", []):
                            if isinstance(c, dict) and c.get("type") == "output_text":
                                text += c.get("text", "")
                text = text or (res or {}).get("output_text", "")
                m = re.search(r"\[.*\]", text, re.S)
                arr = json.loads(m.group(0)) if m else []
                d = {int(o["id"]): o for o in arr if isinstance(o, dict) and "id" in o}
                if d:
                    return d
            except Exception:
                pass
        return {}

    R = [run_once() for _ in range(max(2, min(int(runs or 3), 5)))]
    if not any(R):
        return {"ok": False, "err": "судящий прогон не дал результата (LLM offline или формат)"}
    gold = golden if isinstance(golden, dict) else {}
    report = []
    for f in fields:
        stable, flaky, evaluated = 0, [], 0
        agree, glabeled = 0, 0   # ур.3: согласие с эталоном (если размечен)
        for i in range(1, n + 1):
            # сравниваем ТОЛЬКО валидные прогоны (сбойный прогон не отравляет: даёт меньше значений, а не '?')
            vals = [str((R[k].get(i) or {}).get(f)) for k in range(len(R)) if (R[k].get(i) or {}).get(f) is not None]
            # эталон: мажоритарный ответ модели vs подтверждённое человеком значение
            gv = (gold.get(str(i)) or gold.get(i) or {}).get(f)
            if gv is not None and vals:
                glabeled += 1
                maj = max(set(vals), key=vals.count)
                if maj == str(gv):
                    agree += 1
            if len(vals) < 2:
                continue   # неопределённо — недостаточно валидных прогонов на этой строке
            evaluated += 1
            if len(set(vals)) == 1:
                stable += 1
            else:
                flaky.append({"id": i, "item": str(sample[i - 1])[:80], "answers": vals})
        pct = round(100 * stable / evaluated) if evaluated else None
        agr = round(100 * agree / glabeled) if glabeled else None
        report.append({"field": f, "reproducibility": pct, "evaluated": evaluated,
                       "agreement": agr, "labeled": glabeled,
                       "light": ("gray" if pct is None else ("green" if pct >= 90 else ("amber" if pct >= 60 else "red"))),
                       "flaky": flaky[:5]})
    _scored = [r["reproducibility"] for r in report if r["reproducibility"] is not None]
    overall = round(sum(_scored) / len(_scored)) if _scored else None
    return {"ok": True, "runs": len(R), "n": n, "overall": overall, "fields": report}


def _trust_refine(task, field, clarification, sample, fields, runs=3):
    """Слой доверия, уровень 2: правка-по-примеру. Правит ОПРЕДЕЛЕНИЕ поля (не кейс): Qwen переписывает
    инструкцию, делая размытое поле однозначным по словам пользователя → перепрогон trust_check → новый
    светофор. Возвращает {new_task, result} — видно, стало ли поле стабильнее."""
    ag = qwen_agent()
    if not ag:
        return {"ok": False, "err": "нет доступного Qwen-агента"}
    prompt = ("Вот инструкция суждения:\n" + str(task)[:1500] + "\n\nПоле «" + str(field)[:40] +
              "» определено размыто — модель не согласна сама с собой на разных прогонах. Пользователь "
              "уточняет, как правильно: «" + str(clarification)[:600] + "». Перепиши ИНСТРУКЦИЮ так, чтобы "
              "определение поля «" + str(field)[:40] + "» стало ОДНОЗНАЧНЫМ (чёткие границы и критерии), "
              "остальные поля НЕ меняй. Верни ТОЛЬКО новую инструкцию целиком, без пояснений и без markdown.")
    try:
        res = api("/api/agent/run", {"agent_id": ag, "input": prompt, "temperature": 0.2,
                                     "store": False, "run_timeout": 60}, 90)
        text = ""
        for it in (res or {}).get("output", []):
            if isinstance(it, dict) and it.get("type") == "message":
                for c in it.get("content", []):
                    if isinstance(c, dict) and c.get("type") == "output_text":
                        text += c.get("text", "")
        text = text or (res or {}).get("output_text", "")
    except Exception as e:
        return {"ok": False, "err": "не удалось уточнить определение: " + str(e)[:100]}
    new_task = text.strip().strip("`").strip()
    if len(new_task) < 10:
        return {"ok": False, "err": "модель не вернула уточнённую инструкцию"}
    rep = _trust_check(new_task, sample, fields, runs)
    return {"ok": bool(rep.get("ok")), "new_task": new_task, "result": rep}


def _agent_text(ag, prompt, temp=0.3):
    """Прогон keyless-агента → текст (ретрай при пустом выводе Qwen)."""
    for _ in range(2):
        try:
            res = api("/api/agent/run", {"agent_id": ag, "input": prompt,
                                         "temperature": temp, "store": False, "run_timeout": 80}, 110)
            text = ""
            for it in (res or {}).get("output", []):
                if isinstance(it, dict) and it.get("type") == "message":
                    for c in it.get("content", []):
                        if isinstance(c, dict) and c.get("type") == "output_text":
                            text += c.get("text", "")
            text = (text or (res or {}).get("output_text", "")).strip()
            if text:
                return text
        except Exception:
            pass
    return ""


def _agent_json(ag, prompt):
    t = _agent_text(ag, prompt, temp=0.2)
    m = re.search(r"\{.*\}", t, re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except Exception:
        return {}


def _sandbox_run(code, fn_name="step", arg=None):
    """Безопасный запуск сгенерированного кода в мосту (AST-гард + песочница) — тот же посыл, что в
    wz_source_gen_run: запрет os/subprocess/open/eval/dunder, белый список импортов, safe builtins.
    Это локальный экспериментальный шаг, НЕ эксперт Extella и НЕ production-путь. Возвращает (result, err)."""
    import ast as _ast
    BAD_MOD = {"os", "subprocess", "sys", "shutil", "socket", "pathlib", "importlib", "ctypes", "pickle",
               "marshal", "builtins", "threading", "multiprocessing", "signal", "resource", "pty"}
    BAD_CALL = {"eval", "exec", "compile", "__import__", "open", "input", "globals", "locals", "vars",
                "getattr", "setattr", "delattr"}
    try:
        tree = _ast.parse(code)
    except SyntaxError as e:
        return None, "код не парсится: " + str(e)[:80]
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in BAD_MOD:
                    return None, "запрещённый импорт: " + a.name
        elif isinstance(node, _ast.ImportFrom):
            if (node.module or "").split(".")[0] in BAD_MOD:
                return None, "запрещённый импорт: " + str(node.module)
        elif isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name) and node.func.id in BAD_CALL:
            return None, "запрещённый вызов: " + node.func.id
        elif isinstance(node, _ast.Attribute) and str(node.attr).startswith("__"):
            return None, "dunder-доступ запрещён"
    ALLOWED = {"json", "re", "datetime", "math", "statistics", "random", "base64", "time", "hashlib",
               "hmac", "collections", "itertools", "decimal", "requests", "urllib.parse"}
    _ri = __import__
    def _si(name, *a, **k):
        root = str(name).split(".")[0]
        if name in ALLOWED or root in {m.split(".")[0] for m in ALLOWED}:
            return _ri(name, *a, **k)
        raise ImportError("импорт '%s' запрещён" % name)
    sb = {"__import__": _si, "len": len, "range": range, "str": str, "int": int, "float": float, "dict": dict,
          "list": list, "tuple": tuple, "set": set, "bool": bool, "bytes": bytes, "print": (lambda *a, **k: None),
          "sorted": sorted, "sum": sum, "min": min, "max": max, "enumerate": enumerate, "zip": zip, "map": map,
          "filter": filter, "any": any, "all": all, "abs": abs, "round": round, "isinstance": isinstance,
          "hasattr": hasattr, "format": format, "repr": repr, "reversed": reversed, "divmod": divmod,
          "Exception": Exception, "ValueError": ValueError, "KeyError": KeyError, "TypeError": TypeError,
          "True": True, "False": False, "None": None}
    g = {"__builtins__": sb}
    try:
        exec(compile(tree, "<gen_step>", "exec"), g)
        fn = g.get(fn_name)
        if not callable(fn):
            return None, "в коде нет функции " + fn_name
        return fn(arg), None
    except Exception as e:
        return None, "исполнение: " + str(e)[:120]


def _gen_step_code(goal, step, facts):
    """Контур пишет локальную sandbox-функцию `def step(facts)`; это не эксперт Extella."""
    ag = qwen_agent()
    if not ag:
        return None
    factstr = ("\n".join("- " + f for f in facts)) if facts else "(нет)"
    prompt = ("Цель: " + str(goal)[:600] + "\nШаг: " + str(step)[:400] + "\nНакопленные факты:\n" + factstr +
              "\n\nНапиши Python-функцию РОВНО `def step(facts):` (facts — список строк-фактов выше), которая "
              "ВЫЧИСЛЯЕТ результат этого шага и возвращает КОРОТКУЮ строку-результат. Разрешено: json, math, "
              "statistics, random, datetime, requests. ЗАПРЕЩЕНО: os/subprocess/open/файлы/eval. Верни ТОЛЬКО "
              "код (def step), без пояснений и без markdown-ограждений.")
    text = _agent_text(ag, prompt, temp=0.2)
    m = re.search(r"```(?:python)?\s*(.+?)```", text, re.S)
    code = (m.group(1) if m else text).strip()
    return code if "def step" in code else None


def _step_gate(goal, step, result):
    """Трас-гейт на КАЖДЫЙ шаг + извлечение ПАМЯТИ (один вызов): (1) скептическая проверка, что результат
    реально выполняет шаг (не заглушка/ошибка/выдумка); (2) что из результата ЗАПОМНИТЬ контуру — durable
    факты (concepts: ключ-значение) и правила/ограничения (rules) для следующих шагов. Так контур
    обогащается структурированным знанием по ходу. Fail-closed: пустой/невалидный ответ блокирует шаг."""
    ag = qwen_agent()
    if not ag:
        return {"ok": False, "why": "нет Qwen для независимой проверки шага", "concepts": [], "rules": []}
    v = _agent_json(ag, "Цель: " + str(goal)[:600] + "\nШаг: " + str(step)[:400] +
                    "\nРезультат шага: " + str(result)[:700] +
                    "\n\n1) Проверь СКЕПТИЧЕСКИ: результат корректен и выполняет ЭТОТ шаг (не заглушка/ошибка/"
                    "выдумка/обрыв)?\n2) Что из результата стоит ЗАПОМНИТЬ контуру для следующих шагов: durable "
                    "факты (concepts — пары ключ-значение) и правила/ограничения (rules)?\n"
                    'Верни JSON {"ok":true|false,"why":"<кратко>","concepts":[{"k":"<ключ>","v":"<значение>"}],"rules":["<правило>"]}.')
    if not isinstance(v, dict) or v.get("ok") is not True:
        return {"ok": False, "why": str((v or {}).get("why") or
                                          "трас-гейт не вернул явное ok=true")[:160],
                "concepts": [], "rules": []}
    cs = [{"k": str(c.get("k"))[:60], "v": str(c.get("v", ""))[:120]}
          for c in (v.get("concepts") or []) if isinstance(c, dict) and c.get("k")][:6]
    rs = [str(r)[:160] for r in (v.get("rules") or []) if str(r).strip()][:6]
    return {"ok": True, "why": str(v.get("why", ""))[:160], "concepts": cs, "rules": rs}


def _cap_fetch(source, params, limit=3):
    """ТОНКИЙ примитив добычи: выполняет ОДИН поисковый запрос к источнику с ДАННЫМИ params (их выбирает
    LLM-стратегия, НЕ хардкод). READ-ONLY. Возвращает кандидатов с полем score (загрузки/звёзды) —
    интеллект выбора стратегии живёт в _acquire_loop, здесь только исполнение."""
    import urllib.parse as _up

    def _get(url, p, headers=None):
        rq = urllib.request.Request(url + "?" + _up.urlencode(p),
                                    headers=dict(headers or {}, **{"User-Agent": "extella-wizard"}))
        with urllib.request.urlopen(rq, timeout=8) as r:
            return json.loads(r.read().decode())

    out = []
    src = str(source or "").lower()
    params = params if isinstance(params, dict) else {}
    try:
        if "hug" in src or src == "hf":
            p = {"sort": "downloads", "direction": -1, "limit": 40}
            p.update({k: str(v) for k, v in params.items() if k in ("search", "filter", "author") and v})
            if "search" not in p and "filter" not in p:
                p["search"] = str(params.get("query") or params.get("q") or "")[:80]
            for m in sorted(_get("https://huggingface.co/api/models", p),
                            key=lambda x: -(x.get("downloads") or 0))[:limit]:
                out.append({"source": "HuggingFace", "id": str(m.get("id")),
                            "url": "https://huggingface.co/" + str(m.get("id")), "score": int(m.get("downloads") or 0),
                            "meta": "загрузок: " + str(m.get("downloads") or 0) +
                                    ((" · " + str(m.get("pipeline_tag"))) if m.get("pipeline_tag") else "")})
        elif "git" in src:
            p = {"sort": "stars", "per_page": limit,
                 "q": str(params.get("q") or params.get("query") or params.get("search") or "")[:100]}
            gh = _get("https://api.github.com/search/repositories", p, {"Accept": "application/vnd.github+json"})
            for it in (gh.get("items") or [])[:limit]:
                out.append({"source": "GitHub", "id": str(it.get("full_name")),
                            "url": str(it.get("html_url")), "score": int(it.get("stargazers_count") or 0),
                            "meta": "⭐ " + str(it.get("stargazers_count") or 0) +
                                    ((" · " + str(it.get("description") or "")[:60]) if it.get("description") else "")})
    except Exception:
        pass
    return out


def _acquire_loop(goal, step, ag, max_tries=3):
    """САМО-КОРРЕКТИРУЮЩАЯСЯ добыча способности (тезис Анвара): LLM придумывает стратегию поиска → примитив
    выполняет → судья оценивает СИЛУ/релевантность кандидатов → если слабо, УРОК записывается и передаётся
    в следующую попытку → LLM ищет ИНАЧЕ → … пока не выйдет или не исчерпаны попытки. Интеллект в петле
    (стратегия+урок как память), НЕ в хардкоде параметров. Возвращает {candidates, attempts, lessons}."""
    attempts, lessons, best = [], [], []
    for t in range(max(1, min(int(max_tries), 4))):
        les = ("\n\nУРОКИ прошлых попыток (НЕ повторяй их — ищи ПО-ДРУГОМУ):\n- " + "\n- ".join(lessons)) if lessons else ""
        strat = _agent_json(ag,
            "Нужно ДОБЫТЬ готовую способность (модель/репозиторий) под задачу: " + step + les +
            "\n\nПридумай ОДИН поисковый запрос. Источники и их параметры:\n"
            "• HuggingFace (\"source\":\"huggingface\") — params либо {\"filter\":\"<канонический pipeline-тег: "
            "depth-estimation, object-detection, image-segmentation, image-classification, text-to-image, "
            "automatic-speech-recognition, text-to-speech, translation, summarization, text-classification, "
            "question-answering, feature-extraction, text-generation>\"} (даёт ТОП-модели по загрузкам), "
            "либо {\"search\":\"<слово в имени модели>\"}.\n"
            "• GitHub (\"source\":\"github\") — params {\"q\":\"<2-4 англ. слова>\"}.\n"
            'Верни JSON {"source":"huggingface|github","params":{...},"why":"<чем эта стратегия ОТЛИЧАЕТСЯ от прошлых>"}.')
        source = str(strat.get("source") or "huggingface")
        params = strat.get("params") if isinstance(strat.get("params"), dict) else {}
        cands = _cap_fetch(source, params)
        verdict = _agent_json(ag,
            "Задача: " + step + "\nНайденные кандидаты:\n" +
            ("\n".join("- " + c["source"] + " " + c["id"] + " (" + c["meta"] + ")" for c in cands) if cands else "(пусто)") +
            "\n\nЭто СИЛЬНЫЕ и РЕЛЕВАНТНЫЕ готовые способности под задачу (реально решают, не случайные форки/мусор, "
            'популярные)? Верни JSON {"ok":true|false,"why":"<кратко: почему подходит / чего не хватает>"}.')
        ok = verdict.get("ok") is True and bool(cands)
        attempts.append({"try": t + 1, "source": source, "params": params, "why": str(strat.get("why", ""))[:150],
                         "n": len(cands), "candidates": cands, "ok": ok, "verdict": str(verdict.get("why", ""))[:150]})
        if len(cands) > len(best):
            best = cands
        if ok:
            best = cands
            break
        lessons.append(source + " " + json.dumps(params, ensure_ascii=False)[:70] + " → слабо: " +
                       str(verdict.get("why", ""))[:110])
    return {"candidates": best, "attempts": attempts, "lessons": lessons}


# ── Ф2: реестр агентов визарда ────────────────────────────────────────────────────────────────────
# Платформенный /api/agent/list — баг (пусто), поэтому визард ведёт СВОЙ реестр в KV (глобальный скоуп
# agent_extella_default, как всё KV визарда). Выбранный агент = чей МОЗГ наполняем (концепты/правила
# per-agent) и кому деплоим; эксперты/сессии/KV остаются общими (канон ТЗ). Для Визарда важна
# принадлежность модели семейству Qwen и фактическая работоспособность, а не конкретный провайдер,
# версия, пользовательский ключ или OpenAI-compatible endpoint.
BASE_QWEN_AGENT = "agent_XwZBKvd8dD70jKvW4WrZm"
AGENTS_KV = "wz_agents_index"
CURAGENT_KV = "wz_current_agent"


def _kv_read(key, default=None):
    r = api("/api/kv/get", {"key": key}, 20)
    if isinstance(r, dict) and isinstance(r.get("value"), str):
        try:
            return json.loads(r["value"])
        except Exception:
            return default
    return default


def _kv_write(key, value):
    return api("/api/kv/set", {"key": key, "value": json.dumps(value, ensure_ascii=False)}, 20)


def _api_agent(ep, payload, agent_id, timeout=25):
    """api()-вызов с override X-Agent-Id — для записи per-agent (концепты/правила в мозг ВЫБРАННОГО агента,
    не в общий default). Скоуп мозга = X-Agent-Id; эксперты/KV визарда остаются под default (канон ТЗ)."""
    h = dict(HEADERS)
    h["X-Agent-Id"] = agent_id
    rq = urllib.request.Request(BASE + ep, data=json.dumps(payload).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(rq, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"status": "error", "http_code": e.code, "message": _scrub(e.read().decode()[:300])}
    except Exception as e:
        return {"status": "error", "message": _scrub(str(e)[:200])}


def _brain_write(agent_id, concepts, rules):
    """Записать знание в МОЗГ выбранного агента: концепты (/api/concept/add {text}) и правила
    (/api/rules/add {rule}), скоуп X-Agent-Id=agent_id. Возвращает счётчики реально записанного."""
    cn = rn = 0
    for c in (concepts or []):
        text = ((str(c.get("k", "")) + ": " + str(c.get("v", ""))).strip(": ").strip()
                if isinstance(c, dict) else str(c).strip())
        if not text:
            continue
        r = _api_agent("/api/concept/add", {"text": text[:400]}, agent_id)
        if isinstance(r, dict) and (r.get("status") == "success" or r.get("id")):
            cn += 1
    for rule in (rules or []):
        text = str(rule).strip()
        if not text:
            continue
        r = _api_agent("/api/rules/add", {"rule": text[:400]}, agent_id)
        if isinstance(r, dict) and (r.get("status") == "success" or r.get("id")):
            rn += 1
    return {"concepts": cn, "rules": rn}


# ─────────── A6: ПРАВИЛА ПРОЦЕССА — ИСТОЧНИК ИСТИНЫ НА ПЛАТФОРМЕ ───────────
# Было: текст правил лежал в файле сессии на Маке оператора И копией в записи расписания.
# Три следствия: умрёт Мак — правила потеряны; с другого устройства их не видно; две копии
# приходилось руками синхронизировать. Теперь правила живут в /api/rules в скоупе агента
# процесса (и потому видны в кабинете-мозге), а сессия держит КЭШ на случай, если платформа
# недоступна — прогон не должен падать из-за сети.
PROC_RULE_TAG = "[процесс:%s]"
LEARNED_RULE_TAG = "[выучено:%s]"


def _proc_rule_tag(sid):
    return PROC_RULE_TAG % str(sid)


def _proc_rules_pull(sid, agent_id):
    """Правила процесса с платформы (без тега). None = платформа не ответила (≠ «правил нет»)."""
    r = _api_agent("/api/rules/list", {}, agent_id)
    if not isinstance(r, dict) or r.get("status") == "error":
        return None
    tag = _proc_rule_tag(sid)
    out = []
    # формат платформы проверен вживую: {"status","count","results":[{"id","rule","created_at",…}]}
    for it in (r.get("results") or r.get("rules") or []):
        text = str((it or {}).get("rule") or (it or {}).get("text") or "")
        if text.startswith(tag):
            out.append({"id": (it or {}).get("id") or (it or {}).get("rule_id"),
                        "at": str((it or {}).get("created_at") or ""),
                        "text": text[len(tag):].strip()})
    # платформа отдаёт свежие первыми — владельцу правила должны показываться в том порядке,
    # в каком он их писал, иначе список «сам себя перетасовывает» при каждом открытии
    out.sort(key=lambda x: (x.get("at") or "", str(x.get("id") or "")))
    return out


def _proc_rules_push(sid, rules, agent_id):
    """Синхронизировать список правил процесса с платформой ДИФФОМ: что убрали — удалить,
    что добавили — добавить. Полная перезапись плодила бы дубли и теряла порядок."""
    have = _proc_rules_pull(sid, agent_id)
    if have is None:
        return {"ok": False, "why": "платформа недоступна"}
    tag = _proc_rule_tag(sid)
    want = [str(x).strip() for x in (rules or []) if str(x).strip()]
    have_txt = [h["text"] for h in have]
    added = deleted = 0
    for h in have:
        if h["text"] not in want and h.get("id"):
            rr = _api_agent("/api/rules/delete", {"rule_id": str(h["id"])}, agent_id)
            if isinstance(rr, dict) and rr.get("status") != "error":
                deleted += 1
    for w in want:
        if w not in have_txt:
            rr = _api_agent("/api/rules/add", {"rule": (tag + " " + w)[:400]}, agent_id)
            if isinstance(rr, dict) and (rr.get("status") == "success" or rr.get("rule_id")):
                added += 1
    return {"ok": True, "added": added, "deleted": deleted, "total": len(want)}


def _proc_learned_rules_push(sid, rules, agent_id):
    """Verified-правила Builder хранятся отдельно: синхронизация правил владельца их не отменяет."""
    tag = LEARNED_RULE_TAG % str(sid)
    listed = _api_agent("/api/rules/list", {}, agent_id)
    if not isinstance(listed, dict) or listed.get("status") == "error":
        return {"ok": False, "why": "платформа недоступна"}
    have = []
    for item in (listed.get("results") or listed.get("rules") or []):
        text = str((item or {}).get("rule") or (item or {}).get("text") or "")
        if text.startswith(tag):
            have.append({"id": (item or {}).get("id") or (item or {}).get("rule_id"),
                         "text": text[len(tag):].strip()})
    want = [str(x).strip() for x in (rules or []) if str(x).strip()]
    have_txt = [x["text"] for x in have]
    added = deleted = 0
    for item in have:
        if item["text"] not in want and item.get("id"):
            rr = _api_agent("/api/rules/delete", {"rule_id": str(item["id"])}, agent_id)
            if isinstance(rr, dict) and rr.get("status") != "error":
                deleted += 1
    for text in want:
        if text not in have_txt:
            rr = _api_agent("/api/rules/add", {"rule": (tag + " " + text)[:400]}, agent_id)
            if isinstance(rr, dict) and (rr.get("status") == "success" or rr.get("rule_id")):
                added += 1
    return {"ok": True, "added": added, "deleted": deleted, "total": len(want)}


def _proc_concepts_push(sid, concepts, agent_id):
    """Упаковать предметный контекст процесса в мозг рабочего агента без дублей при redeploy."""
    tag = _proc_rule_tag(sid)
    want = [str(x).strip() for x in (concepts or []) if str(x).strip()]
    listed = _api_agent("/api/concept/list", {}, agent_id)
    if not isinstance(listed, dict) or listed.get("status") == "error":
        return {"ok": False, "why": "платформа недоступна"}
    have = []
    for item in (listed.get("results") or listed.get("concepts") or []):
        text = str((item or {}).get("concept_text") or (item or {}).get("text") or "")
        if text.startswith(tag):
            have.append({"id": (item or {}).get("concept_id") or (item or {}).get("id"),
                         "text": text[len(tag):].strip()})
    have_txt = [x["text"] for x in have]
    added = deleted = 0
    for item in have:
        if item["text"] not in want and item.get("id"):
            rr = _api_agent("/api/concept/delete", {"concept_id": item["id"]}, agent_id)
            if isinstance(rr, dict) and rr.get("status") != "error":
                deleted += 1
    for text in want:
        if text not in have_txt:
            rr = _api_agent("/api/concept/add", {"text": (tag + " " + text)[:400]}, agent_id)
            if isinstance(rr, dict) and (rr.get("status") == "success" or rr.get("concept_id") or rr.get("id")):
                added += 1
    return {"ok": True, "added": added, "deleted": deleted, "total": len(want)}


def _proc_rules_read(s):
    """Правила процесса для показа и прогона: платформа — источник истины, файл сессии — кэш.
    Платформа молчит → работаем по кэшу и честно говорим об этом (правила не «исчезают»)."""
    sid = s.get("session_id") or ""
    cached = list(s.get("rules") or [])
    ag = qwen_agent()
    if not (sid and ag):
        return {"rules": cached, "source": "cache"}
    live = _proc_rules_pull(sid, ag)
    if live is None:
        return {"rules": cached, "source": "cache", "stale": True}
    if not live and cached:
        # ПЕРЕЕЗД, а не потеря. Процессы, созданные до A6, держат правила только в файле сессии.
        # Наивное «платформа — источник истины» показало бы их владельцу как «правил нет»
        # и затёрло бы кэш пустотой — то есть молча уничтожило его настройку.
        # Поэтому первое чтение ПОДНИМАЕТ старые правила на платформу.
        mig = _proc_rules_push(sid, cached, ag)
        if mig.get("ok"):
            return {"rules": cached, "source": "platform", "migrated": True}
        return {"rules": cached, "source": "cache", "stale": True}
    return {"rules": [x["text"] for x in live], "source": "platform"}


def _agent_skills(agent_id):
    """Ф3 «Умения»: эксперты, которыми агент реально владеет через СВОИ процессы (стемп agent_id).
    Эксперты — общий граф платформы (канон ТЗ), поэтому read-only: показываем, что уже задействовано."""
    seen, procs = {}, 0
    for p in SESS_DIR.glob("wz_*.json"):
        if p.name.endswith(("_blueprint.json", "_build_plan.json")):
            continue
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if s.get("agent_id") != agent_id:
            continue
        builds = s.get("builds") or []
        if not builds:
            continue
        procs += 1
        for e in (builds[-1].get("experts") or []):
            en = str(e).strip()
            if en and en not in seen:
                seen[en] = s.get("process_name") or s.get("client_name") or s.get("session_id")
    return {"experts": [{"name": k, "from": v} for k, v in list(seen.items())[:60]], "processes": procs}


def _brain_read(agent_id, cap=150):
    """Ф3 кабинет-мозг: читаем МОЗГ выбранного агента — концепты (знания), правила (per-agent скоуп) и
    умения (эксперты из его процессов; сам граф экспертов — общий, канон ТЗ)."""
    cs, rs = [], []
    cl = _api_agent("/api/concept/list", {}, agent_id)
    for c in (cl.get("results", []) if isinstance(cl, dict) else [])[:cap]:
        cs.append({"id": c.get("concept_id"), "text": str(c.get("concept_text", ""))})
    rl = _api_agent("/api/rules/list", {}, agent_id)
    for r in (rl.get("results", []) if isinstance(rl, dict) else [])[:cap]:
        rs.append({"id": r.get("id"), "text": str(r.get("rule", ""))})
    return {"concepts": cs, "rules": rs, "skills": _agent_skills(agent_id)}


def _brain_edit(agent_id, op, text="", item_id=None):
    """Правка мозга словами: добавить/удалить концепт или правило в скоупе выбранного агента."""
    if op == "add_concept" and text.strip():
        r = _api_agent("/api/concept/add", {"text": text.strip()[:400]}, agent_id)
        return {"ok": bool(isinstance(r, dict) and (r.get("id") or r.get("status") == "success"))}
    if op == "add_rule" and text.strip():
        r = _api_agent("/api/rules/add", {"rule": text.strip()[:400]}, agent_id)
        return {"ok": bool(isinstance(r, dict) and (r.get("rule_id") or r.get("status") == "success"))}
    if op == "del_concept" and item_id is not None:
        r = _api_agent("/api/concept/delete", {"concept_id": item_id}, agent_id)
        return {"ok": bool(isinstance(r, dict) and (r.get("deleted") or r.get("status") == "success"))}
    if op == "del_rule" and item_id is not None:
        r = _api_agent("/api/rules/delete", {"rule_id": str(item_id)}, agent_id)
        return {"ok": bool(isinstance(r, dict) and (r.get("deleted") or r.get("status") == "success"))}
    return {"ok": False, "err": "неизвестная операция или пустой ввод"}


VOICE_TAG = "[голос]"


def _voice_learn(agent_id, samples):
    """Слой знание-и-голос: агент учится ГОЛОСУ клиента по примерам. Qwen извлекает стиль (тон, длина,
    характерные обороты, что делать/не делать) → сохраняем правилами '[голос] …' в мозг агента (per-agent).
    Стиль становится ПРАВИЛАМИ → шейпит будущие ответы агента. Возвращает профиль + сколько правил записано."""
    ag = qwen_agent()
    texts = [str(s).strip() for s in (samples or []) if str(s).strip()][:6]
    if not ag or not texts:
        return {"ok": False, "err": "нужен агент и хотя бы один пример"}
    joined = "\n\n".join("Пример " + str(i + 1) + ":\n" + t[:600] for i, t in enumerate(texts))
    v = _agent_json(ag, "Вот как ПИШЕТ этот человек/компания (реальные примеры):\n\n" + joined +
                    "\n\nИзвлеки его ГОЛОС для будущих ответов агента. Верни JSON "
                    '{"summary":"<1-2 фразы про стиль>","rules":["<конкретное правило голоса в повелительном '
                    'наклонении, напр. «Обращаться на вы, коротко, без канцелярита»>", "..."]}. '
                    "Правила должны быть ДЕЙСТВЕННЫМИ (тон, длина, обороты, приветствие/подпись, что избегать), 3-6 штук.")
    rules = [str(r).strip() for r in (v.get("rules") or []) if str(r).strip()][:6]
    saved = 0
    for r in rules:
        rr = _api_agent("/api/rules/add", {"rule": (VOICE_TAG + " " + r)[:400]}, agent_id)
        if isinstance(rr, dict) and (rr.get("rule_id") or rr.get("status") == "success"):
            saved += 1
    return {"ok": saved > 0, "summary": str(v.get("summary", ""))[:300], "rules": rules, "saved": saved}


def _voice_try(agent_id, situation):
    """Примерка голоса: генерим пробный ответ агента в ВЫУЧЕННОМ голосе (по его правилам '[голос]')."""
    ag = qwen_agent()
    if not ag:
        return {"ok": False, "err": "нет Qwen-агента"}
    brain = _brain_read(agent_id)
    voice = [r["text"].replace(VOICE_TAG, "").strip() for r in brain.get("rules", []) if VOICE_TAG in r["text"]]
    if not voice:
        return {"ok": False, "err": "голос ещё не выучен — сначала обучите по примерам"}
    reply = _agent_text(ag, "Ты отвечаешь ГОЛОСОМ, заданным правилами:\n- " + "\n- ".join(voice) +
                        "\n\nСитуация/сообщение, на которое надо ответить: " + str(situation)[:500] +
                        "\n\nНапиши ответ СТРОГО в этом голосе (только текст ответа, без пояснений).")
    return {"ok": bool(reply), "reply": (reply or "").strip()[:800], "voice": voice}


def _knowledge_learn(agent_id, text):
    """Слой знание-и-голос (знание КОМПАНИИ): кусок знаний клиента (FAQ/прайс/регламент/описание) → Qwen
    дробит на АТОМАРНЫЕ факты-концепты → пачкой в мозг выбранного агента. Так документ становится
    структурированным знанием агента (внутренняя база), а не одним куском текста."""
    ag = qwen_agent()
    blob = str(text or "").strip()
    if not ag or not blob:
        return {"ok": False, "err": "нужен агент и текст знаний"}
    v = _agent_json(ag, "Вот знания о компании/продукте/регламенте клиента:\n\n" + blob[:4000] +
                    "\n\nРазбей на АТОМАРНЫЕ самодостаточные факты для памяти агента (каждый — одна законченная "
                    'мысль, понятная без контекста). Верни JSON {"facts":["<факт 1>","<факт 2>", ...]}. '
                    "Только факты из текста, без выдумок; 3-20 штук.")
    facts = [str(f).strip() for f in (v.get("facts") or []) if str(f).strip()][:20]
    saved = 0
    for f in facts:
        r = _api_agent("/api/concept/add", {"text": f[:400]}, agent_id)
        if isinstance(r, dict) and (r.get("id") or r.get("status") == "success"):
            saved += 1
    return {"ok": saved > 0, "facts": facts, "saved": saved}


def _knowledge_from_rows(agent_id, rows, context=""):
    """Знание из ЖИВОГО источника: строки данных (от генеративного коннектора) → Qwen извлекает атомарные
    факты (конкретные значения из данных) → в мозг агента. Замыкает: описал источник → сам вытянул → в мозг."""
    ag = qwen_agent()
    sample = [r for r in (rows or [])][:25]
    if not ag or not sample:
        return {"ok": False, "err": "нет данных для запоминания"}
    v = _agent_json(ag, "Живые данные из источника" + (" («" + str(context)[:120] + "»)" if context else "") + ":\n" +
                    json.dumps(sample, ensure_ascii=False)[:3500] +
                    "\n\nИзвлеки АТОМАРНЫЕ факты для памяти агента (конкретные значения/сущности из ДАННЫХ, каждый "
                    'самодостаточен, напр. «Товар X стоит Y», «У клиента Z долг W»). Верни JSON {"facts":[...]}. '
                    "Только из данных, без выдумок, 3-20 штук.")
    facts = [str(f).strip() for f in (v.get("facts") or []) if str(f).strip()][:20]
    ids = []
    for f in facts:
        r = _api_agent("/api/concept/add", {"text": f[:400]}, agent_id)
        if isinstance(r, dict) and r.get("id"):
            ids.append(r.get("id"))
    return {"ok": len(ids) > 0, "facts": facts, "saved": len(ids), "ids": ids}


# ── A4: источник знаний с обновлением (мозг агента не устаревает) ──────────────────────────────────
def _knowsrc_key(agent_id, gen_id):
    return "knowsrc:" + _ns(agent_id) + ":" + _ns(gen_id)


def _knowsrc_index_key(agent_id):
    return "knowsrc_idx:" + _ns(agent_id)


KNOWSRC_ALL = "knowsrc:__all__"   # ГЛОБАЛЬНЫЙ индекс для тика 24/7: готовые KV-ключи (тику не считать _ns)


def _knowsrc_index_add(agent_id, gen_id):
    idx = _kv_read(_knowsrc_index_key(agent_id), []) or []
    if gen_id not in idx:
        idx.append(gen_id)
        _kv_write(_knowsrc_index_key(agent_id), idx)
    allx = _kv_read(KNOWSRC_ALL, []) or []
    k = _knowsrc_key(agent_id, gen_id)
    if not any(isinstance(e, dict) and e.get("key") == k for e in allx):
        allx.append({"key": k, "agent_id": agent_id, "gen_id": gen_id})
        _kv_write(KNOWSRC_ALL, allx)


def _knowsrc_index_remove(agent_id, gen_id):
    idx = [g for g in (_kv_read(_knowsrc_index_key(agent_id), []) or []) if g != gen_id]
    _kv_write(_knowsrc_index_key(agent_id), idx)
    k = _knowsrc_key(agent_id, gen_id)
    allx = [e for e in (_kv_read(KNOWSRC_ALL, []) or []) if not (isinstance(e, dict) and e.get("key") == k)]
    _kv_write(KNOWSRC_ALL, allx)


def _knowledge_refresh(agent_id, gen_id):
    """Обновление знания из источника: перечитать ЖИВЫЕ строки (verify по сохранённому gen_id) → УДАЛИТЬ
    прошлые факты этого источника (по их id) → записать свежие. Так мозг агента освежается, а не пухнет."""
    rec = _kv_read(_knowsrc_key(agent_id, gen_id), None)
    if not isinstance(rec, dict):
        return {"ok": False, "err": "источник знаний не найден"}
    out = _run_gen_source(gen_id, "verify", limit=25)
    rows = out.get("preview") if isinstance(out, dict) else None
    if not (isinstance(out, dict) and out.get("ok") and rows):
        return {"ok": False, "err": "источник не отдал живых строк при обновлении"}
    for cid in (rec.get("ids") or []):   # выкидываем прошлый набор фактов этого источника
        _api_agent("/api/concept/delete", {"concept_id": cid}, agent_id)
    kn = _knowledge_from_rows(agent_id, rows, rec.get("description") or gen_id)
    rec["ids"] = kn.get("ids", [])
    rec["refreshed_at"] = datetime.now(timezone.utc).isoformat()
    ivl = int(rec.get("interval_hours", 24) or 24)
    rec["next_due"] = (datetime.now(timezone.utc) + timedelta(hours=max(1, ivl))).isoformat()
    _kv_write(_knowsrc_key(agent_id, gen_id), rec)
    return {"ok": kn.get("ok", False), "saved": kn.get("saved", 0), "facts": kn.get("facts", []),
            "refreshed_at": rec["refreshed_at"], "next_due": rec["next_due"]}


def _knowsrc_list(agent_id):
    """Источники знаний агента (для панели мозга): что обновляется и когда следующий раз. Список — по индексу."""
    out = []
    for gid in (_kv_read(_knowsrc_index_key(agent_id), []) or []):
        rec = _kv_read(_knowsrc_key(agent_id, gid), None)
        if isinstance(rec, dict):
            out.append({"gen_id": rec.get("gen_id"), "description": rec.get("description"),
                        "interval_hours": rec.get("interval_hours"), "facts": len(rec.get("ids") or []),
                        "refreshed_at": rec.get("refreshed_at"), "next_due": rec.get("next_due")})
    return out


def _agents_load():
    reg = _kv_read(AGENTS_KV, [])
    return reg if isinstance(reg, list) else []


def _agents_state():
    """Реестр агентов визарда + текущий выбранный (для UI-инварианта «видно, для кого строим»)."""
    return {"agents": _agents_load(), "current": _kv_read(CURAGENT_KV, "") or ""}


def _agent_register(agent_id, name, source):
    reg = _agents_load()
    if not any(a.get("id") == agent_id for a in reg):
        reg.append({"id": agent_id, "name": name, "source": source})
        _kv_write(AGENTS_KV, reg)
    _kv_write(CURAGENT_KV, agent_id)
    return reg


def _is_qwen_agent_record(record):
    """Qwen может приходить от Alibaba/DashScope, OpenRouter, локального или custom endpoint."""
    if not isinstance(record, dict):
        return False
    identity = " ".join(str(record.get(k) or "") for k in
                        ("model", "name", "description", "provider", "provider_name")).casefold()
    return "qwen" in identity or "tongyi" in identity


def _agent_probe_ok(response):
    """Не путать HTTP-ответ с работающей моделью: нужен completion/result или явный success."""
    if not isinstance(response, dict) or response.get("status") == "error":
        return False
    text = str(response.get("output_text") or response.get("result") or "").strip()
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict):
                text += str(content.get("text") or "")
    return bool(text.strip()) or str(response.get("status") or "").lower() in (
        "success", "completed", "done", "finished", "ok")


def _agent_create_copy(name):
    """Создать копию любого доступного Qwen и принять её только после фактического ответа.

    Сначала используем Qwen, уже выбранные/настроенные пользователем: их provider/model сохраняют
    привязанный на аккаунте BYOK и endpoint. Конкретную версию и провайдера не фиксируем. Канонический
    Alibaba Qwen — только последний fallback, если на аккаунте не удалось прочитать ни одного Qwen.
    """
    base = None
    candidates = [_kv_read(CURAGENT_KV, "") or ""] + list(qwen_agents()) + [
        "agent_extella_alibaba_default", BASE_QWEN_AGENT]
    seen = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        g = api("/api/agent/get", {"agent_id": cand})
        if isinstance(g, dict) and g.get("model") and _is_qwen_agent_record(g):
            base = g
            break
    # ПУЛЕНЕПРОБИВАЕМО: если ни один базовый не прочитался (аккаунт клиента без доступа к чужим
    # агентам — Гульжан 20.07), создаём Qwen С НУЛЯ по каноническим параметрам, не проваливаясь.
    # Проверено на втором живом аккаунте: create с этими полями проходит.
    if base:
        prov, model, params, instr, tools = (base.get("provider"), base.get("model"),
                                             base.get("model_parameters") or {},
                                             base.get("instructions") or "", base.get("tools") or [])
    else:
        prov, model = "alibaba", "qwen3.7-max-2026-06-08"
        params = {"maxContextTokens": 1000000, "web_search": True, "top_p": 0.95, "temperature": 0.6}
        instr, tools = "", []
    cr = api("/api/agent/create", {
        "name": str(name)[:80] or "Агент клиента",
        "provider": prov, "model": model,
        "description": "Клиентский агент (Qwen Extella) — создан визардом",
        "instructions": instr, "tools": tools, "model_parameters": params})
    nid = cr.get("id") if isinstance(cr, dict) else None
    if not nid or not str(nid).startswith("agent_"):
        return {"ok": False, "err": _scrub(str(cr)[:200])}
    # create может вернуть id, хотя Pro custom-agent ещё ждёт пользовательский BYOK. Не называем его
    # рабочим до smoke. При BYOK-ошибке карточку сохраняем: пользователь сможет привязать ключ и затем
    # добавить этот же id; при прочих ошибках удаляем действительно битый артефакт.
    probe = api("/api/agent/run", {"agent_id": nid, "input": "Ответь одним словом: готов.",
                                    "run_timeout": 45, "store": False}, timeout=55)
    if not _agent_probe_ok(probe):
        raw = _scrub(str((probe or {}).get("message") if isinstance(probe, dict) else probe))
        if "pro_key_required" in raw or "provider API key" in raw:
            why = ("Qwen создан как Pro, но ждёт BYOK-ключ провайдера. Привяжите к нему любой "
                   "подходящий Qwen-ключ/endpoint и затем добавьте по id: " + str(nid))
        else:
            api("/api/agent/delete", {"agent_id": nid}, timeout=30)
            why = "Созданный агент не прошёл контрольный запуск; пустую карточку я удалил: " + raw[:160]
        return {"ok": False, "err": why, "id": nid, "needs_byok": "pro_key_required" in raw or "provider API key" in raw}
    _agent_register(nid, cr.get("name") or name, "created")
    return {"ok": True, "id": nid, "name": cr.get("name") or name}


def _agent_link(agent_id):
    """Привязать существующий рабочий Qwen независимо от провайдера, ключа и endpoint."""
    aid = str(agent_id).strip()
    if not aid.startswith("agent_"):
        return {"ok": False, "err": "нужен корректный agent_id (agent_...)"}
    g = api("/api/agent/get", {"agent_id": aid})
    if not isinstance(g, dict) or not g.get("id"):
        return {"ok": False, "err": "агент не найден на платформе"}
    if not _is_qwen_agent_record(g):
        return {"ok": False, "err": "для Визарда нужен агент семейства Qwen; этот агент не распознан как Qwen"}
    probe = api("/api/agent/run", {"agent_id": aid, "input": "Ответь одним словом: готов.",
                                    "run_timeout": 45, "store": False}, timeout=55)
    if not _agent_probe_ok(probe):
        return {"ok": False, "err": "Qwen найден, но пока не отвечает: " + _scrub(str(probe)[:180])}
    _agent_register(aid, g.get("name") or aid, "linked")
    return {"ok": True, "id": aid, "name": g.get("name") or aid}


def _goal_loop(goal, max_iters=3):
    """Контур: цель → LLM планирует ОДИН следующий шаг → выполняет → копит факты → трас-гейт на завершение
    (независимая проверка «цель достигнута?») → дальше/готово/спросить человека. Честные стоп-условия:
    done (после верификации), need_human, потолок итераций. Это скелет самопродлевающегося цикла."""
    ag = qwen_agent()
    if not ag:
        return {"ok": False, "err": "нет доступного Qwen-агента"}
    goal = str(goal)[:800]
    facts, journal = [], []
    memory = {"concepts": [], "rules": []}   # структурированная память контура: копится по ходу, обогащает шаги

    def ftext():
        return ("\n".join("- " + f for f in facts)) if facts else "(пока ничего)"

    def mtext():
        if not memory["concepts"] and not memory["rules"]:
            return ""
        parts = []
        if memory["concepts"]:
            parts.append("Известные факты (концепты): " +
                         "; ".join(c["k"] + "=" + c["v"] for c in memory["concepts"]))
        if memory["rules"]:
            parts.append("Действующие правила: " + "; ".join(memory["rules"]))
        return "\nПАМЯТЬ КОНТУРА (учитывай):\n" + "\n".join(parts) + "\n"

    def remember(g):
        for c in g.get("concepts", []):
            if not any(x["k"].lower() == c["k"].lower() for x in memory["concepts"]):
                memory["concepts"].append(c)
        for r in g.get("rules", []):
            if not any(x.lower() == r.lower() for x in memory["rules"]):
                memory["rules"].append(r)

    for it in range(max(1, min(int(max_iters or 3), 6))):
        plan = _agent_json(ag, "Цель: " + goal + "\nСделано (накоплено):\n" + ftext() + mtext() +
                           "\n\nОпредели ОДИН следующий конкретный шаг к цели. Верни СТРОГО JSON: "
                           '{"kind":"reason|build|acquire|need_human|done","step":"<что делаем / что спросить у человека / итог>","need":"<для acquire: короткий АНГЛ. поисковый запрос способности>","why":"<кратко>"}. '
                           "done — цель уже достигнута накопленным; need_human — нужен человек (доступ/решение/данные, "
                           "которых нет); build — шаг требует ВЫЧИСЛЕНИЯ/обработки данных/кода (посчитать, сгенерировать, "
                           "преобразовать, дёрнуть API) — под него напишется и запустится код; acquire — шаг требует "
                           "ГОТОВОЙ ВНЕШНЕЙ способности, которую нельзя просто написать кодом с нуля (обученная ML/3D-"
                           "модель, спец-репозиторий/инструмент) — её надо ДОБЫТЬ (модель HuggingFace / репо GitHub); "
                           "reason — шаг решается рассуждением/текстом без кода.")
        kind = str(plan.get("kind", "")).strip().lower()
        if kind == "compute":
            kind = "reason"
        step = str(plan.get("step", ""))[:400]
        if kind not in ("reason", "build", "acquire", "need_human", "done") or not step:
            journal.append({"iter": it + 1, "kind": "error", "step": "план не разобран"})
            break
        if kind == "done":
            v = _agent_json(ag, "Цель: " + goal + "\nНакоплено:\n" + ftext() + mtext() +
                            "\n\nДостигнута ли цель ПОЛНОСТЬЮ этими фактами? Отвечай строго и скептически. "
                            'Верни JSON {"met":true|false,"why":"<кратко>"}.')
            if v.get("met") is True:
                journal.append({"iter": it + 1, "kind": "done", "step": step})
                return {"ok": True, "status": "done", "iters": it + 1, "journal": journal,
                        "facts": facts, "memory": memory}
            facts.append("Трас-гейт: цель ещё не достигнута — " + str(v.get("why", ""))[:200])
            journal.append({"iter": it + 1, "kind": "reject_done", "step": step, "why": str(v.get("why", ""))[:200]})
            continue
        if kind == "need_human":
            journal.append({"iter": it + 1, "kind": "need_human", "step": step})
            return {"ok": True, "status": "need_human", "question": step, "iters": it + 1,
                    "journal": journal, "facts": facts, "memory": memory}
        if kind == "acquire":
            # ДОБЫЧА СПОСОБНОСТИ (само-корректирующаяся): кодоген не тянет → LLM ищет, оценивает свой же
            # результат, учится на слабом и пробует ИНАЧЕ (интеллект в петле, не в хардкоде). Установка =
            # действие на устройстве → канон предложи-подтверди: эскалируем лучшего кандидата на гейт человека.
            aq = _acquire_loop(goal, step, ag)
            cands = aq["candidates"]
            # что контур ВЫУЧИЛ про добычу этой способности → в память (сработавшая стратегия + уроки)
            won = next((a for a in aq["attempts"] if a["ok"]), None)
            if won:
                remember({"concepts": [{"k": "как добыть «" + step[:40] + "»",
                          "v": won["source"] + " " + json.dumps(won["params"], ensure_ascii=False)[:70]}], "rules": []})
            if aq["lessons"]:
                remember({"concepts": [], "rules": ["поиск способности: не искать способом — " +
                          "; ".join(aq["lessons"])[:220]]})
            journal.append({"iter": it + 1, "kind": "acquire", "step": step,
                            "candidates": cands, "attempts": aq["attempts"]})
            if cands:
                top = cands[0]
                q = ("Для шага «" + step[:120] + "» нужна готовая способность (её нельзя написать кодом с нуля). "
                     "Контур сам подобрал за " + str(len(aq["attempts"])) + " попыт(ку/ки): " +
                     top["source"] + " · " + top["id"] + " (" + top["meta"] + "). "
                     "Это только кандидат: Контур его не устанавливал и автоматически продолжить не может. "
                     "Проверьте источник и установите/подключите способность отдельным подтверждённым действием.")
            else:
                q = ("Для шага «" + step[:120] + "» нужна внешняя способность, но за " + str(len(aq["attempts"])) +
                     " попыт(ку/ки) контур не нашёл сильного кандидата. Подскажите источник (ссылка на модель/репо).")
            return {"ok": True, "status": "need_human", "question": q, "iters": it + 1, "journal": journal,
                    "facts": facts, "memory": memory, "candidates": cands, "attempts": aq["attempts"]}
        if kind == "build":
            # Эксперимент: LLM пишет локальную функцию → запускаем в песочнице. Это НЕ production-эксперт Extella.
            code = _gen_step_code(goal, step, facts)
            if code:
                res, err = _sandbox_run(code, "step", list(facts))
                if err is None and res is not None:
                    rs = str(res)[:280]
                    g = _step_gate(goal, step, rs)   # трас-гейт на ШАГ + извлечение памяти: до принятия в факты
                    if not g["ok"]:
                        facts.append("Шаг «" + step[:80] + "» не прошёл проверку: " + g["why"] + " — переделать иначе.")
                        journal.append({"iter": it + 1, "kind": "reject_step", "step": step, "why": g["why"], "code": code[:600]})
                        continue
                    remember(g)
                    facts.append(step + " → " + rs)
                    journal.append({"iter": it + 1, "kind": "build", "step": step, "result": rs, "code": code[:600],
                                    "learned": {"concepts": g["concepts"], "rules": g["rules"]}})
                    continue
                facts.append(step + " → сборка не удалась (" + str(err or "нет кода")[:120] + ")")
                journal.append({"iter": it + 1, "kind": "build_fail", "step": step,
                                "why": str(err or "код не сгенерирован")[:160], "code": (code or "")[:600]})
                continue
            journal.append({"iter": it + 1, "kind": "build_fail", "step": step, "why": "код не сгенерирован"})
            facts.append(step + " → сборка не удалась (код не сгенерирован)")
            continue
        # reason: шаг решается рассуждением (LLM даёт результат) → трас-гейт → в факты (память контура)
        result = _agent_text(ag, "Цель: " + goal + "\nВыполни шаг: " + step + "\nНакоплено:\n" + ftext() + mtext() +
                             "\nВерни КРАТКИЙ конкретный результат этого шага (1-3 предложения), двигающий к цели.")
        rr = result[:280] if result else "(шаг не дал результата)"
        g = _step_gate(goal, step, rr)
        if not g["ok"]:
            facts.append("Шаг «" + step[:80] + "» не прошёл проверку: " + g["why"] + " — переделать иначе.")
            journal.append({"iter": it + 1, "kind": "reject_step", "step": step, "why": g["why"]})
            continue
        remember(g)
        facts.append(step + " → " + rr)
        journal.append({"iter": it + 1, "kind": "reason", "step": step, "result": rr,
                        "learned": {"concepts": g["concepts"], "rules": g["rules"]}})
    return {"ok": True, "status": "max_iters", "iters": len(journal), "journal": journal,
            "facts": facts, "memory": memory}


# Фаза 1 шов #3: кластер стройки вынесен в wz_build.py
from wz_build import _run_build, sample_preflight
from wz_agentic import apply_owner_clarification


def _start_build_job(sid):
    """Единый старт обычной и продолженной стройки; очищает только owner-checkpoint этой сессии."""
    build_id = "build_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M") + "_" + uuid.uuid4().hex[:4]
    (RUNS_DIR / build_id).mkdir(parents=True, exist_ok=True)
    (RUNS_DIR / build_id / "build_progress.json").write_text(
        json.dumps({"build_id": build_id, "session_id": sid, "status": "running", "stages": [],
                    "kind": "build", "resume_sched": False}, ensure_ascii=False), encoding="utf-8")

    def mark(s):
        s.pop("waiting_build", None)
        s["building"] = build_id

    _update_session(sid, mark)
    threading.Thread(target=_run_build, args=(sid, build_id), daemon=True).start()
    return build_id


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
        """CSRF-защита мутирующих эндпоинтов: блокируем ЯВНО внешние веб-origin (evil.com) И песочный
        Origin:null. #9 null больше НЕ доверенный: sandbox-iframe/data:/file: могут слать «простой»
        POST (text/plain, без preflight), а мост парсит тело как JSON независимо от Content-Type →
        это рабочий вектор CSRF. Свой UI грузится с 127.0.0.1:8765 (same-origin, никогда не null — иначе
        его application/json-запросы уже ловил бы preflight, а OPTIONS-обработчика на мосту нет).
        Пустой Origin (нативный тулбар/curl — браузер ВСЕГДА шлёт Origin на POST) пропускаем."""
        o = (self.headers.get("Origin", "") or "").strip()
        if not o:
            return False
        if o == "null":
            return True
        m = re.match(r"^https?://([^/:]+)", o)
        if not m:
            return False
        if m.group(1) not in ("127.0.0.1", "localhost"):
            return True
        # #12 локальный БРАУЗЕРНЫЙ origin (в т.ч. другой localhost-порт) может слать «простой» POST мосту.
        # Требуем double-submit токен — он есть только у нашего wizard.html (чужая страница не прочитает
        # его через SOP). Свой UI шлёт X-Bridge-CSRF; при несовпадении — отклоняем.
        return (self.headers.get("X-Bridge-CSRF", "") or "").strip() != BRIDGE_CSRF

    def _bad_host(self):
        """Защита от DNS-rebinding: Host обязан быть локальным. Иначе чужой сайт, чей DNS переехал на
        127.0.0.1, становится same-origin с мостом в глазах браузера и читает данные/секреты (даже GET,
        где Origin не шлётся). Пустой Host (не-браузерные клиенты) — пропускаем."""
        h = (self.headers.get("Host", "") or "").strip().lower()
        if not h:
            return False
        if h.startswith("["):                       # IPv6: [::1]:8765
            host = h[1:h.find("]")] if "]" in h else h[1:]
        else:
            host = h.rsplit(":", 1)[0] if ":" in h else h
        return host not in ("127.0.0.1", "localhost", "::1")

    # ---------------- reads: local files ----------------
    def do_GET(self):
        if self._bad_host():   # DNS-rebinding: чужой Host → отказ (защищает чтения/секреты)
            self._send({"status": "error", "message": "forbidden host"}, 403)
            return
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
                # ФОЛБЭК: файла нет локально (сформирован на другом устройстве / вычищен из /tmp) —
                # собираем его из общего стора по sid+имени. Гульжан «файл недоступен», 20.07.
                _sid = _up.unquote(qs.get("sid", ""))
                _bn = Path(raw).name
                if _sid and _bn:
                    _m = _materialize_from_store(_sid, _bn, REPORTS_DIR / _ns(_sid))
                    if _m and Path(_m).is_file():
                        fp = Path(_m)
            if not fp:
                self._send({"status": "error", "code": "report_missing",
                            "message": "Отчёт не найден ни на этом устройстве, ни в хранилище. "
                                       "Запустите процесс ещё раз — новые отчёты сохраняются и "
                                       "скачиваются с любого устройства."}, 404)
                return
            # render-check: не отдаём клиенту БИТЫЙ отчёт как готовый (0 байт / повреждённый OOXML|PDF).
            # force=1 — всё равно скачать (для отладки).
            if qs.get("force", "") not in ("1", "true", "yes"):
                _chk = _artifact_check(fp)
                if not _chk["ok"]:
                    self._send({"status": "error", "code": "artifact_corrupt", "reason": _chk["reason"],
                                "kind": _chk["kind"], "bytes": _chk["bytes"],
                                "message": "файл повреждён: " + _chk["reason"] + " — скачать как есть можно с force=1"}, 422)
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
        elif path == "/x/csrf":
            # #12 double-submit: отдаём CSRF-токен своему UI. Чужой localhost-странице ответ недоступен
            # (мост не шлёт CORS-заголовков → кросс-origin чтение блокирует SOP). Host уже проверен _bad_host.
            self._send({"status": "success", "token": BRIDGE_CSRF})
        elif path == "/x/chat_history":
            import urllib.parse as _up
            sid = _up.unquote(qs.get("session_id", ""))
            self._send({"status": "success", "turns": _chat_load(sid)})
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
            out = [dict({"connector": k}, **_secidx_entry(v)) for k, v in sorted(idx.items())]
            # рассинхрон паков: WhatsApp (GreenAPI) мог быть подключён Travel-паком в config.json —
            # показываем его как найденный external, UI предложит «сделать общим» (адопция в сейф).
            if "whatsapp" not in idx and CONFIG.get("greenapi_id") and CONFIG.get("greenapi_token"):
                out.append({"connector": "whatsapp", "external": "config"})
            # Tourvisor: источник правды — config.tourvisor_jwt (его читают ta_*-эксперты Travel-пака).
            # В сейф не переносим; показываем статус + срок JWT (exp), чтобы истечение было видно ЗАРАНЕЕ.
            if CONFIG.get("tourvisor_jwt"):
                _tvexp = None
                try:
                    import base64 as _b64x
                    _p = str(CONFIG["tourvisor_jwt"]).split(".")[1]
                    _p += "=" * (-len(_p) % 4)
                    _e = json.loads(_b64x.urlsafe_b64decode(_p)).get("exp")
                    if _e:
                        _tvexp = datetime.fromtimestamp(int(_e), tz=timezone.utc).isoformat()
                except Exception:
                    _tvexp = None
                out.append({"connector": "tourvisor", "external": "config", "expires": _tvexp})
            self._send({"status": "success", "client": client, "secrets": out})
        elif path == "/x/catalog":
            cp = _ensure_catalog_path()
            if not _catalog_usable(cp):
                self._send({"status": "error", "code": "catalog_invalid",
                            "message": "Каталог возможностей отсутствует или повреждён. Обновите Wizard и повторите действие."}, 503)
                return
            self._send(json.loads(cp.read_text(encoding="utf-8")))
        elif path == "/x/registry":
            # Capability Registry v0 (ТЗ v2 §8.9, версия «MD+KV» по решению Анвара): единый реестр
            # возможностей для четырёх поверхностей. Пишет scripts/capability_registry.py в KV
            # capability:registry (b64-шарды по 8000 — паттерн файлового стора); мост только читает.
            try:
                g = api("/api/kv/get", {"key": "capability:registry"})
                meta = json.loads(g.get("value") or "{}")
                buf = ""
                for i in range(int(meta.get("chunks", 0))):
                    c = api("/api/kv/get", {"key": "capability:registry:" + str(i)})
                    buf += c.get("value") or ""
                if meta.get("enc") == "b64" and buf:
                    import base64 as _b64r
                    buf = _b64r.b64decode(buf).decode("utf-8")
                doc = json.loads(buf) if buf else {}
                caps = doc.get("capabilities") or []
                t = qs.get("type", "")
                if t:
                    caps = [c for c in caps if c.get("type") == t]
                self._send({"status": "success", "count": len(caps),
                            "generated_at": doc.get("generated_at"), "capabilities": caps})
            except Exception as e:
                self._send({"status": "error",
                            "message": "реестр не собран — запустите scripts/capability_registry.py: "
                                       + _scrub(str(e)[:120])}, 503)
            return

        elif path == "/x/team":
            # Команда v0: участники = именные invite:-токены аккаунта. Токены наружу — ТОЛЬКО маской.
            try:
                r = api("/api/token/list", {})
                items = r.get("tokens") or r.get("results") or []
                team = []
                for t in items:
                    if not (isinstance(t, dict) and str(t.get("name", "")).startswith("invite: ")):
                        continue
                    tv = str(t.get("token", ""))
                    team.append({"name": t.get("name", "")[8:], "created_at": t.get("created_at"),
                                 "revoked": bool(t.get("revoked")),
                                 "token_mask": (tv[:6] + "…" + tv[-4:]) if len(tv) > 12 else "***",
                                 "full_name": t.get("name")})
                self._send({"status": "success", "count": len(team), "team": team})
            except Exception as e:
                self._send({"status": "error", "message": _scrub(str(e)[:120])}, 503)
            return

        elif path == "/x/launchagents":
            # Системные агенты устройства (запрос Анвара: «видеть LaunchAgents и управлять»).
            # Источник: ~/Library/LaunchAgents/*.plist (ТОЛЬКО пользовательские) + launchctl list
            # (running/pid). UI-раздел на Plugins-главной рисует тулбар; мост отдаёт данные/действия.
            import subprocess as _sp
            agents = []
            try:
                la_dir = Path.home() / "Library" / "LaunchAgents"
                running = {}
                try:
                    out = _sp.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
                    for ln in out.splitlines()[1:]:
                        parts = ln.split("\t")
                        if len(parts) >= 3:
                            running[parts[2].strip()] = parts[0].strip()
                except Exception:
                    pass
                disabled = set()
                try:
                    dout = _sp.run(["launchctl", "print-disabled", "gui/" + str(os.getuid())],
                                   capture_output=True, text=True, timeout=10).stdout
                    disabled = set(re.findall(r'"([\w\.\-]+)"\s*=>\s*(?:true|disabled)', dout))
                except Exception:
                    pass
                for p in sorted(la_dir.glob("*.plist")):
                    label = p.stem
                    pid = running.get(label)
                    agents.append({"label": label, "file": p.name,
                                   "running": bool(pid and pid != "-"),
                                   "pid": (pid if pid and pid != "-" else None),
                                   "enabled": label not in disabled,
                                   "family": ("extella" if "extella" in label else
                                              "dronor" if ("dronor" in label or "pagi" in label or "personalagi" in label)
                                              else "other")})
                self._send({"status": "success", "count": len(agents), "agents": agents})
            except Exception as e:
                self._send({"status": "error", "message": _scrub(str(e)[:150])}, 500)
            return

        elif path == "/x/listener_procs":
            # Дубли листенера: два листенера одного аккаунта конкурируют за фоновые задачи.
            # Сирота = процесс extella-listener с PPID=1 (родитель-лаунчер приложения умер —
            # остался от прошлого запуска). Живой штатный — дитя uv-лаунчера Extella.app.
            import subprocess as _sp
            procs = []
            try:
                out = _sp.run(["ps", "-axo", "pid,ppid,lstart,command"], capture_output=True, text=True, timeout=10).stdout
                for ln in out.splitlines()[1:]:
                    if "bin/extella-l" not in ln or "grep" in ln:
                        continue
                    parts = ln.split(None, 2)
                    pid, ppid = parts[0], parts[1]
                    procs.append({"pid": int(pid), "ppid": int(ppid),
                                  "orphan": ppid == "1",
                                  "started": " ".join(ln.split()[2:7])})
                self._send({"status": "success", "count": len(procs),
                            "orphans": sum(1 for p in procs if p["orphan"]), "procs": procs})
            except Exception as e:
                self._send({"status": "error", "message": _scrub(str(e)[:120])}, 500)
            return

        elif path == "/x/targets":
            # Мультитаргет T1: паспорта устройств (их пишет wz_target_passport, исполняясь НА устройстве).
            # Отдаём как есть + свежесть; ничего не выдумываем — нет паспорта, значит нет устройства в карте.
            try:
                g = api("/api/kv/get", {"key": "target:passports:__index__"})
                slugs = (json.loads(g.get("value") or "{}") or {}).get("slugs") or []
                out = []
                for sl in slugs[:20]:
                    if not re.match(r"^[a-z0-9-]+$", str(sl)):
                        continue
                    try:
                        p = api("/api/kv/get", {"key": "target:passport:" + sl})
                        pp = json.loads(p.get("value") or "{}")
                        if pp:
                            out.append(pp)
                    except Exception:
                        pass
                self._send({"status": "success", "count": len(out), "targets": out})
            except Exception as e:
                self._send({"status": "error", "message": _scrub(str(e)[:120])}, 503)
            return

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
                                "agent_id": s.get("agent_id"), "agent_name": s.get("agent_name"),
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
        elif path == "/x/agents":
            # Ф2: реестр агентов визарда + текущий выбранный (инвариант «видно, для кого строим»)
            self._send({"status": "success", **_agents_state()})
        elif path == "/x/agent_check":
            # Проверка ЖИВОСТИ агента. Выбрать агента мало — надо убедиться, что он отвечает:
            # иначе мы просто сдвигаем ошибку «нет ключа» на первый вопрос клиента (случай Гульжан).
            aid = qs.get("agent_id", "") or (_kv_read(CURAGENT_KV, "") or "")
            if not aid:
                self._send({"status": "error", "message": "агент не выбран"}, 400)
            else:
                r = api("/api/agent/run", {"agent_id": aid, "input": "ответь одним словом: готов",
                                           "run_timeout": 40, "store": False}, timeout=50)
                _txt = ""
                for it in (r or {}).get("output", []):
                    if isinstance(it, dict) and it.get("type") == "message":
                        for c in it.get("content", []):
                            if isinstance(c, dict) and c.get("type") == "output_text":
                                _txt += c.get("text", "")
                ok = bool(_txt.strip()) or (isinstance(r, dict) and r.get("status") == "completed")
                self._send({"status": "success", "agent_id": aid, "ok": ok,
                            "why": "" if ok else (_llm_error_human(r) or
                                                  "агент не ответил — попробуйте другого или создайте нового"),
                            "detail": "" if ok else _scrub(str(r)[:200])})
        elif path == "/x/devices":
            # A1: устройства аккаунта + ЖИВОСТЬ + способности (проба + паспорт). refresh=1 — мимо кэша.
            self._send({"status": "success",
                        "devices": _targets_live(force=qs.get("refresh", "") in ("1", "true"))})
        elif path == "/x/archive":
            # Диалог удаления обещает «можно вернуть» — значит владелец обязан ВИДЕТЬ архив
            # и возвращать оттуда сам. До этого возврат был возможен только руками в терминале:
            # обещание есть, исполнить его некому.
            arch = SESS_DIR.parent / "sessions_archive"
            out = []
            if arch.exists():
                for f in sorted(arch.glob("wz_*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
                    if f.name.endswith(("_blueprint.json", "_build_plan.json", "_chat.json",
                                        "_build_manifest.json", "_spec.json")):
                        continue
                    try:
                        s = json.loads(f.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    lb = (s.get("builds") or [{}])[-1]
                    out.append({"session_id": s.get("session_id") or f.stem,
                                "name": s.get("process_name") or s.get("client_name") or f.stem,
                                "goal": (s.get("goal") or "")[:160],
                                "archived_at": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat(),
                                "runs": len(s.get("runs") or []),
                                "built": bool(lb.get("orchestrator")),
                                "conflict": (SESS_DIR / f.name).exists()})
            self._send({"status": "success", "archived": out[:100]})
        elif path == "/x/report_spec":
            sid = qs.get("sid", "")
            sp = SESS_DIR / (sid + ".json")
            if not sid or not sp.exists():
                self._send({"status": "error", "message": "нет сессии"}, 400)
            else:
                s = json.loads(sp.read_text(encoding="utf-8"))
                self._send({"status": "success", "spec": s.get("report_spec") or {},
                            "supported": int(((s.get("builds") or [{}])[-1]).get("report_contract", 0) or 0) >= 1,
                            "fields": (s.get("source") or {}).get("schema") or []})
        elif path == "/x/manifest":
            # AC-06: контракт собранного процесса — что ест, что отдаёт, из чего состоит.
            sid = qs.get("sid", "")
            sp = SESS_DIR / (sid + ".json")
            if not sid or not sp.exists():
                self._send({"status": "error", "message": "нет сессии"}, 400)
            else:
                s = json.loads(sp.read_text(encoding="utf-8"))
                lb = (s.get("builds") or [{}])[-1]
                mani = lb.get("manifest")
                if not mani:
                    self._send({"status": "success", "manifest": None,
                                "note": "процесс собран до появления манифестов — появится после пересборки"})
                else:
                    self._send({"status": "success", "manifest": mani})
        elif path == "/x/adapter":
            # AC-05: действующий адаптер + история версий + текущие колонки источника
            sid = qs.get("sid", "")
            sp = SESS_DIR / (sid + ".json")
            if not sid or not sp.exists():
                self._send({"status": "error", "message": "нет сессии"}, 400)
            else:
                s = json.loads(sp.read_text(encoding="utf-8"))
                d = _adapter_load(sid)
                self._send({"status": "success", "active": d.get("active") or 0,
                            "versions": [{"v": v.get("v"), "at": v.get("at"), "note": v.get("note"),
                                          "pairs": len(v.get("map") or {})} for v in (d.get("versions") or [])],
                            "map": (_adapter_active(sid) or {}).get("map") or {},
                            "process_fields": (s.get("source") or {}).get("schema") or [],
                            "supported": int(((s.get("builds") or [{}])[-1]).get("adapter_contract", 0) or 0) >= 1})
        elif path == "/x/rules":
            # A6: правила процесса из ИСТОЧНИКА ИСТИНЫ (платформа), кэш сессии — фолбэк.
            sid = qs.get("sid", "")
            sp = SESS_DIR / (sid + ".json")
            if not sid or not sp.exists():
                self._send({"status": "error", "message": "нет сессии"}, 400)
            else:
                s = json.loads(sp.read_text(encoding="utf-8"))
                rr = _proc_rules_read(s)
                if rr.get("source") == "platform" and rr["rules"] != list(s.get("rules") or []):
                    _update_session(sid, lambda ss: ss.__setitem__("rules", rr["rules"]))   # кэш подтягиваем к правде
                self._send({"status": "success", "rules": rr["rules"], "source": rr["source"],
                            "stale": bool(rr.get("stale")), "fields": s.get("fields") or {},
                            "rules_struct": s.get("rules_struct") or []})
        elif path == "/x/placement":
            # A1: карта размещения процесса. Наружу — только ref устройства.
            sid = qs.get("sid", "")
            sp = SESS_DIR / (sid + ".json")
            if not sid or not sp.exists():
                self._send({"status": "error", "message": "нет сессии"}, 400)
            else:
                s = json.loads(sp.read_text(encoding="utf-8"))
                devs = _targets_live()
                pl = s.get("placement") or {}
                self._send({"status": "success",
                            "stages": _placement_stages(s),
                            "stage_labels": _placement_stage_labels(s),
                            "devices": [{"ref": d.get("ref"), "label": d.get("label"),
                                         "online": d.get("online"), "is_host": d.get("is_host"),
                                         "is_local": d.get("is_local")} for d in devs],
                            "map": _placement_get(s),
                            "confirmed": bool(pl.get("confirmed")),
                            "supported": int(((s.get("builds") or [{}])[-1]).get("placement_contract", 0) or 0) >= 1,
                            "proposal": _placement_plan(s, devs)})
        elif path == "/x/brain":
            # Ф3 кабинет-мозг: знания (концепты) + правила текущего агента (per-agent скоуп)
            cur = _kv_read(CURAGENT_KV, "") or ""
            if not cur:
                self._send({"status": "error", "message": "агент не выбран"}, 400)
            else:
                nm = next((a.get("name") for a in _agents_load() if a.get("id") == cur), cur)
                self._send({"status": "success", "agent": {"id": cur, "name": nm},
                            "sources": _knowsrc_list(cur), **_brain_read(cur)})
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
            _amap = _sched_kv_batch([pp.stem for pp in SESS_DIR.glob("wz_*.json")
                                     if not pp.name.endswith(("_blueprint.json", "_build_plan.json"))])
            # личность карточек (emoji/accent/tagline) из общей витрины — по sessionId (её пишет /x/publish)
            _ident = {}
            try:
                _mc = api("/api/kv/get", {"key": "_mkt_automations", "global": True})
                _mcv = _mc.get("value") if isinstance(_mc, dict) else None
                for _c in (json.loads(_mcv).get("items", []) if _mcv else []):
                    _sid = _c.get("sessionId")
                    if _sid:
                        _ident[_sid] = {k: _c.get(k) for k in
                                        ("emoji", "accent", "tagline", "capabilities", "category", "status")}
            except Exception:
                _ident = {}
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
                manual_runs = s.get("runs") or []   # (склейка ниже — через _runs_unified)
                skv = _amap.get(s.get("session_id", ""))
                runs = _runs_unified(s, skv)   # F3: единая история (dedup + trigger)
                out.append({
                    "session_id": s.get("session_id"),
                    "client_name": s.get("client_name"),
                    "process_name": (bp or {}).get("process_name") or s.get("client_name"),
                    "stage": s.get("stage"),
                    "agent_id": s.get("agent_id"), "agent_name": s.get("agent_name"),   # Ф2: для какого агента процесс
                    "components": lb.get("components_human") or lb.get("experts") or [],   # C5: ярлыки для плиток
                    "params_contract": int(lb.get("params_contract", 0) or 0),   # F2: правила влияют на прогоны?
                    "target_requirements": s.get("target_requirements"),   # T2: требования к устройству
                    "decisions": (s.get("decisions") or [])[-5:],   # C6: журнал правок для карточки «Версии»
                    "can_rollback": bool(s.get("blueprint_history")),   # C6: есть куда откатывать
                    "orchestrator": lb.get("orchestrator"),
                    "flow_id": lb.get("flow_id") or None,   # C2: композиция — для вкладки «Состав·Доводка»
                    "audit": (lb.get("audit") or {}).get("verdict"),
                    "slice_summary": lb.get("slice_summary"),
                    "source_file": lb.get("source_file"),
                    "schedule": s.get("schedule"),
                    "paused": bool(s.get("paused")),   # C1: Пауза (источник статуса — сессия; sched/inbound KV — исполнение)
                    "inbound": s.get("inbound") or None,   # C1: есть ли приём входящих (для статуса и предупреждения паузы)
                    "next_due_ts": (skv or {}).get("next_due_ts"),   # C1: «след. запуск» в шапке Пульта
                    "recipients": _recipients(s),   # кабина «Настройка»: получатели результата (несколько каналов)
                    "message_template": s.get("message_template") or "",   # кабина «Шаблон сообщения»
                    "source": s.get("source") or None,
                    "production_agent": s.get("production_agent"),
                    "runs_count": len(runs),
                    "last_run": runs[-1] if runs else None,
                    "runs": runs[-40:][::-1],   # полная история (свежие сверху) — для вкладки «Запуски» кабинета
                    "identity": _ident.get(s.get("session_id", "")) or {},   # личность карточки (обложка)
                    "panel_url": s.get("panel_url"), "panel_name": s.get("panel_name"),   # родная панель пака (Travel и т.п.)
                    "rules": s.get("rules") or [], "fields": s.get("fields") or {},   # «Правила и поля» владельца (§7bis ступень 2)
                    "panel_manifest": s.get("panel_manifest") or None,   # сгенерированные доменные поля (§7bis ступень 3)
                    "knowledge_pack": (bp or {}).get("knowledge_pack") or None,
                    "goal": (bp or {}).get("goal") or (bp or {}).get("summary"),
                    # ЖАНР процесса: определяется ещё при проектировании и лежит в blueprint,
                    # но кабина о нём не знала и показывала всем один и тот же экран. Отчёт,
                    # мониторинг и дайджест — разные жанры: у них по-разному читается результат
                    # (у мониторинга НОЛЬ срабатываний — хорошая новость, у отчёта ноль позиций — тревога).
                    "archetype": ((bp or {}).get("archetype") or {}).get("id")
                                 if isinstance((bp or {}).get("archetype"), dict)
                                 else ((bp or {}).get("archetype") if isinstance((bp or {}).get("archetype"), str) else None),
                    "stages_meta": [{"title": st.get("title"), "inputs": st.get("inputs"),
                                     "outputs": st.get("outputs"), "capability_ids": st.get("capability_ids")}
                                    for st in ((bp or {}).get("stages") or [])],
                    # Шаги, собранные структурно, но сомнительные по смыслу (смысловой гейт сборки).
                    # Показываем В КАБИНЕТЕ, чтобы доводка была направленной: видно, ЧТО поправить словами.
                    "needs_review": (lb or {}).get("needs_review") or [],
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

            _tv_days = _tourvisor_days()   # срок JWT Tourvisor (общий кред тенанта) — считаем один раз

            def _mon_one(s):
                # карточка здоровья одного процесса (до 3 KV-round-trip) — вызывается параллельно
                sid = s.get("session_id", "")
                skv = _sched_kv(sid) or {}
                sched = s.get("schedule") or {}
                runs = _runs_unified(s, skv)   # F3
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
                if last and last.get("status") == "blocked":
                    # A3: прогон ЗАБЛОКИРОВАН защитой (дрифт источника/устройства, преflight) — причина словами
                    health = "error"
                    reasons.append("прогон заблокирован: " + str(last.get("needs_review_reason") or last.get("blocked_code") or "проверьте процесс"))
                elif last and last.get("status") not in (None, "success"):
                    health = "error"; reasons.append("последний прогон: " + str(last.get("status")))
                if last and isinstance(last.get("source_drift"), dict) and last["source_drift"].get("added"):
                    if health == "ok":
                        health = "warn"
                    reasons.append("источник добавил колонки: " + ", ".join(last["source_drift"]["added"][:5]))
                if last and int(last.get("attempts") or 1) > 1:
                    if health == "ok":
                        health = "warn"
                    reasons.append("прогон прошёл только с " + str(last.get("attempts")) + "-й попытки (источник/платформа флейкуют)")
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
                # креды с датой: процесс, использующий Tourvisor (Travel-пак), желтеет/краснеет ЗАРАНЕЕ
                _uses_tv = bool(s.get("panel_url")) or "ta_" in str((s.get("builds") or [{}])[-1].get("orchestrator") or "")
                if _uses_tv and _tv_days is not None:
                    if _tv_days < 0:
                        health = "error"; reasons.append("токен Tourvisor истёк")
                    elif _tv_days <= 7:
                        if health == "ok":
                            health = "warn"
                        reasons.append("токен Tourvisor истекает через " + str(_tv_days) + " дн.")
                return {
                    "session_id": sid,
                    "process_name": s.get("client_name") or sid,
                    "health": health, "reasons": reasons,
                    "schedule": ({"period": sched.get("period"), "interval_min": interval,
                                  "next_due": nd, "overdue": overdue} if sched else None),
                    "last_run": ({"at": (last or {}).get("at"), "status": (last or {}).get("status"),
                                  "total_sum": (last or {}).get("total_sum"), "total_count": (last or {}).get("total_count"),
                                  "needs_review_reason": (last or {}).get("needs_review_reason")} if last else None),
                    "runs_count": len(runs),
                    "source": ({"kind": src.get("kind"), "refresh": src.get("refresh")} if src else None),
                    "deliver": deliver or None,
                    "recipients": _recipients(s),
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
            creds = []   # учётные данные с датой — общий блок панели (желтеет заранее)
            if _tv_days is not None:
                creds.append({"name": "Tourvisor", "days": _tv_days,
                              "severity": "error" if _tv_days < 0 else ("warn" if _tv_days <= 7 else "ok")})
            _resp = {"status": "success", "at": now_dt.isoformat(), "summary": summ, "processes": procs, "credentials": creds}
            _MON_CACHE["at"] = datetime.now(timezone.utc); _MON_CACHE["resp"] = _resp   # TTL с момента ЗАВЕРШЕНИЯ
            self._send(_resp)
        elif path == "/x/runs":
            sid = qs.get("session_id", "")
            sp = SESS_DIR / (sid + ".json")
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
            else:
                s = json.loads(sp.read_text(encoding="utf-8"))
                runs = _runs_unified(s, _sched_kv(sid))   # F3
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
        # Безопасность локального моста (у него токен Extella): Host-валидация (DNS-rebinding) + ЕДИНАЯ
        # проверка Origin для ВСЕХ POST (CSRF) — раньше часть эндпоинтов её пропускала. Свои вызовы
        # (UI same-origin, нативные/тулбар с пустым Origin) проходят; чужой сайт evil.com — отклоняется.
        if self._bad_host():
            self._send({"status": "error", "message": "forbidden host"}, 403)
            return
        if self._blocked_origin():
            self._send({"status": "error", "message": "forbidden origin"}, 403)
            return
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
                if "server.py" not in fl:
                    self._send({"status": "error", "applied": False, "message": "манифест без server.py"}, 502)
                    return
                # МНОГОФАЙЛ (Фаза 1): скачать ВСЕ файлы манифеста (server.py + модули bridge + wizard.html),
                # а не 2 хардкод — иначе после разреза монолита релиз слал бы server.py без wz_platform.py → кирпич.
                staged = {}
                for _name, _meta in fl.items():
                    _raw = _release_download(_name, (_meta or {}).get("sha256", ""), (_meta or {}).get("chunks", 0))
                    if not _raw:
                        for _x in staged.values():
                            _x.unlink(missing_ok=True)
                        self._send({"status": "error", "applied": False, "message": "download/sha256 mismatch: " + _name}, 502)
                        return
                    _stp = APP_DIR / (_name + ".new")
                    _stp.write_bytes(_raw)
                    staged[_name] = _stp
                for _name, _stp in staged.items():   # синтаксис всех .py
                    if _name.endswith(".py"):
                        try:
                            _pc.compile(str(_stp), doraise=True)
                        except Exception as e:
                            for _x in staged.values():
                                _x.unlink(missing_ok=True)
                            self._send({"status": "error", "applied": False, "message": "compile failed (%s): %s" % (_name, str(e)[:120])}, 500)
                            return
                # РЕАЛЬНЫЙ smoke: ВСЕ новые файлы + config.json в temp-каталоге на эфемерном порту → health(новая версия)+GET/ 200 HTML
                smoke_ok, smoke_err, proc = False, "", None
                tmp = _tf.mkdtemp(prefix="wzsmoke_")
                try:
                    for _name, _stp in staged.items():
                        _sh.copy2(str(_stp), str(Path(tmp) / _name))
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
                    for _x in staged.values():
                        _x.unlink(missing_ok=True)
                    self._send({"status": "error", "applied": False, "message": "smoke-тест не пройден: " + smoke_err[:200]}, 500)
                    return
                # маркер ДО необратимого свопа; бэкап; атомарный своп (server.py — ПОСЛЕДНИМ, он триггерит рестарт)
                (APP_DIR / ".update_state").write_text(json.dumps({"to": latest, "from": BRIDGE_VERSION, "attempts": 0,
                                                                   "state": "swapping", "at": datetime.now(timezone.utc).isoformat()}))
                for _name in staged:   # бэкап всех живых → .prev
                    _live = APP_DIR / _name
                    if _live.exists():
                        _sh.copy2(str(_live), str(APP_DIR / (_name + ".prev")))
                for _name in [n for n in staged if n != "server.py"]:   # модули и html — раньше
                    _os.replace(str(staged[_name]), str(APP_DIR / _name))
                _os.replace(str(staged["server.py"]), str(APP_DIR / "server.py"))   # server.py — ПОСЛЕДНИМ (триггерит рестарт)
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
            # #10/#15: тест/отправка = проверка живости → пишем результат в индекс (карточка покажет честно)
            _secidx_mark(CLIENT_ID, connector, validated_ok=ok, err=(out.get("err") if isinstance(out, dict) else None))
            self._send({"status": "success" if ok else "error", "connector": connector, "result": out})
            return

        if self.path == "/x/trust_check":
            # Слой доверия, срез 1: воспроизводимость без разметки. Гоняем судящий промпт N раз на выборке →
            # светофор по полям (где расходится — определение размыто) + примеры расхождений. Эталон не нужен.
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            task = str(body.get("task", "")).strip()
            sample = body.get("sample") if isinstance(body.get("sample"), list) else []
            fields = body.get("fields") if isinstance(body.get("fields"), list) else []
            runs = int(body.get("runs", 3) or 3)
            if not task or not sample or not fields:
                self._send({"status": "error", "message": "нужны task, sample[] и fields[]"}, 400)
                return
            golden = body.get("golden") if isinstance(body.get("golden"), dict) else None
            out = _trust_check(task, sample[:20], [str(f)[:40] for f in fields][:12], runs, golden)
            ok = isinstance(out, dict) and out.get("ok")
            self._send({"status": "success" if ok else "error", "result": out})
            return

        if self.path == "/x/goal_loop":
            # Контур: цель → сам генерит следующий шаг → выполняет → трас-гейт → дальше/готово/спросить человека.
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            goal = str(body.get("goal", "")).strip()
            max_iters = int(body.get("max_iters", 3) or 3)
            if not goal:
                self._send({"status": "error", "message": "нужна цель (goal)"}, 400)
                return
            out = _goal_loop(goal, max_iters)
            ok = isinstance(out, dict) and out.get("ok")
            # В постоянный мозг — только после доказанного done. need_human/max_iters/failed остаются журналом.
            if ok and out.get("status") == "done" and isinstance(out.get("memory"), dict):
                cur = _kv_read(CURAGENT_KV, "") or ""
                mem = out["memory"]
                if cur and (mem.get("concepts") or mem.get("rules")):
                    saved = _brain_write(cur, mem.get("concepts"), mem.get("rules"))
                    nm = next((a.get("name") for a in _agents_load() if a.get("id") == cur), cur)
                    out["saved_to_agent"] = {"id": cur, "name": nm, **saved}
            self._send({"status": "success" if ok else "error", "result": out})
            return

        if self.path in ("/x/agent_create", "/x/agent_link", "/x/agent_select", "/x/agent_unlink"):
            # Ф2: управление агентами в самом визарде (создать копию Qwen / привязать / выбрать / отвязать)
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            if self.path == "/x/agent_create":
                r = _agent_create_copy(str(body.get("name", "")).strip())
            elif self.path == "/x/agent_link":
                r = _agent_link(str(body.get("agent_id", "")).strip())
            elif self.path == "/x/agent_select":
                aid = str(body.get("agent_id", "")).strip()
                if not any(a.get("id") == aid for a in _agents_load()):
                    r = {"ok": False, "err": "агента нет в реестре"}
                else:
                    _kv_write(CURAGENT_KV, aid)
                    r = {"ok": True, "id": aid}
            else:  # agent_unlink — убрать из реестра визарда (на платформе НЕ удаляем)
                aid = str(body.get("agent_id", "")).strip()
                reg = [a for a in _agents_load() if a.get("id") != aid]
                _kv_write(AGENTS_KV, reg)
                if (_kv_read(CURAGENT_KV, "") or "") == aid:
                    _kv_write(CURAGENT_KV, reg[0]["id"] if reg else "")
                r = {"ok": True, "id": aid}
            self._send({"status": "success" if r.get("ok") else "error",
                        "message": r.get("err", ""), **_agents_state(), "result": r})
            return

        if self.path == "/x/brain_edit":
            # Ф3: правка мозга ТЕКУЩЕГО агента словами (добавить/удалить концепт или правило)
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            cur = _kv_read(CURAGENT_KV, "") or ""
            if not cur:
                self._send({"status": "error", "message": "агент не выбран"}, 400)
                return
            r = _brain_edit(cur, str(body.get("op", "")), str(body.get("text", "")), body.get("id"))
            nm = next((a.get("name") for a in _agents_load() if a.get("id") == cur), cur)
            self._send({"status": "success" if r.get("ok") else "error", "message": r.get("err", ""),
                        "agent": {"id": cur, "name": nm}, **_brain_read(cur)})
            return

        if self.path in ("/x/voice_learn", "/x/voice_try", "/x/knowledge_learn"):
            # Слой знание-и-голос: голос по примерам / примерка / знание компании из текста
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            cur = _kv_read(CURAGENT_KV, "") or ""
            if not cur:
                self._send({"status": "error", "message": "агент не выбран"}, 400)
                return
            if self.path == "/x/voice_learn":
                r = _voice_learn(cur, body.get("samples") if isinstance(body.get("samples"), list) else [])
            elif self.path == "/x/knowledge_learn":
                r = _knowledge_learn(cur, str(body.get("text", "")))
            else:
                r = _voice_try(cur, str(body.get("situation", "")))
            self._send({"status": "success" if r.get("ok") else "error", "message": r.get("err", ""), "result": r})
            return

        if self.path == "/x/trust_refine":
            # Слой доверия, уровень 2: правит определение размытого поля словами → перепрогон → новый светофор.
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            task = str(body.get("task", "")).strip()
            field = str(body.get("field", "")).strip()
            clar = str(body.get("clarification", "")).strip()
            sample = body.get("sample") if isinstance(body.get("sample"), list) else []
            fields = body.get("fields") if isinstance(body.get("fields"), list) else []
            runs = int(body.get("runs", 3) or 3)
            if not task or not field or not clar or not sample or not fields:
                self._send({"status": "error", "message": "нужны task, field, clarification, sample[], fields[]"}, 400)
                return
            out = _trust_refine(task, field, clar, sample[:20], [str(f)[:40] for f in fields][:12], runs)
            ok = isinstance(out, dict) and out.get("ok")
            self._send({"status": "success" if ok else "error", "new_task": (out or {}).get("new_task"),
                        "result": (out or {}).get("result"), "message": (out or {}).get("err")})
            return

        if self.path == "/x/source_generate":
            # Генеративный коннектор: «Подключи любой источник» — Qwen пишет fetch(secret) по описанию,
            # секрет уходит в сейф, код в KV, verify-прогон на устройстве отдаёт ПЕРВЫЕ РЕАЛЬНЫЕ СТРОКИ
            # (гейт доверия перед привязкой). OAuth-площадки сюда не идут — им Composio.
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            client = CLIENT_ID
            endpoint = str(body.get("endpoint", "")).strip()
            description = str(body.get("description", "")).strip()
            if not endpoint and not description:
                self._send({"status": "error", "message": "нужно описание источника и/или эндпоинт"}, 400)
                return
            import hashlib as _h
            gen_id = re.sub(r"[^a-z0-9]", "", str(body.get("gen_id", "")).lower())[:24] or \
                _h.md5((endpoint + description + str(time.time())).encode("utf-8")).hexdigest()[:12]
            # секрет (креды) в сейф под gen_<id>
            secret = body.get("secret") if isinstance(body.get("secret"), dict) else {}
            if secret:
                _store_client_secret(client, "gen_" + gen_id, json.dumps(secret, ensure_ascii=False))
            # Qwen генерит fetch(secret)
            code, gerr = _gen_source_code({"description": description, "endpoint": endpoint,
                                           "auth_kind": body.get("auth_kind", "none"), "row_hint": body.get("row_hint", "")})
            if not code:
                self._send({"status": "error", "message": gerr or "не удалось сгенерировать источник"}, 502)
                return
            # код источника в KV
            rec = {"code": code, "endpoint": endpoint, "description": description,
                   "created_at": datetime.now(timezone.utc).isoformat()}
            api("/api/kv/set", {"key": "gensrc:" + _ns(client) + ":" + _ns(gen_id),
                                "value": json.dumps(rec, ensure_ascii=False), "description": "gensrc"})
            # verify на устройстве → первые реальные строки (гейт доверия)
            out = _run_gen_source(gen_id, "verify", limit=20)
            ok = isinstance(out, dict) and out.get("ok")
            self._send({"status": "success" if ok else "error", "gen_id": gen_id, "kind": "gen:" + gen_id,
                        "code": code, "verify": out})
            return

        if self.path == "/x/knowledge_from_source":
            # Замыкание слоёв: опиши источник → генеративный коннектор вытянул ЖИВЫЕ данные → факты в мозг агента.
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            cur = _kv_read(CURAGENT_KV, "") or ""
            if not cur:
                self._send({"status": "error", "message": "агент не выбран"}, 400)
                return
            client = CLIENT_ID
            endpoint = str(body.get("endpoint", "")).strip()
            description = str(body.get("description", "")).strip()
            if not endpoint and not description:
                self._send({"status": "error", "message": "нужно описание источника и/или эндпоинт"}, 400)
                return
            import hashlib as _h
            gen_id = _h.md5((endpoint + description + str(time.time())).encode("utf-8")).hexdigest()[:12]
            secret = body.get("secret") if isinstance(body.get("secret"), dict) else {}
            if secret:
                _store_client_secret(client, "gen_" + gen_id, json.dumps(secret, ensure_ascii=False))
            code, gerr = _gen_source_code({"description": description, "endpoint": endpoint,
                                           "auth_kind": body.get("auth_kind", "none"), "row_hint": body.get("row_hint", "")})
            if not code:
                self._send({"status": "error", "message": gerr or "не удалось сгенерировать источник"}, 502)
                return
            api("/api/kv/set", {"key": "gensrc:" + _ns(client) + ":" + _ns(gen_id),
                                "value": json.dumps({"code": code, "endpoint": endpoint, "description": description,
                                                     "created_at": datetime.now(timezone.utc).isoformat()},
                                                    ensure_ascii=False), "description": "gensrc"})
            out = _run_gen_source(gen_id, "verify", limit=25)
            rows = out.get("preview") if isinstance(out, dict) else None
            if not (isinstance(out, dict) and out.get("ok") and rows):
                self._send({"status": "error", "message": "источник не отдал живых строк — уточните адрес/ключ",
                            "verify": out}, 502)
                return
            kn = _knowledge_from_rows(cur, rows, description or endpoint)
            nm = next((a.get("name") for a in _agents_load() if a.get("id") == cur), cur)
            # A4: сохранить как ОБНОВЛЯЕМЫЙ источник знаний (мозг не устареет) — по флагу save
            if body.get("save") and kn.get("ok"):
                ivl = int(body.get("interval_hours", 24) or 24)
                rec = {"gen_id": gen_id, "agent_id": cur, "description": description or endpoint,
                       "interval_hours": max(1, ivl), "ids": kn.get("ids", []),
                       "client": CLIENT_ID, "llm_agent": qwen_agent(), "target": HOST_TARGET,   # тику 24/7: где тянуть и чем извлекать
                       "refreshed_at": datetime.now(timezone.utc).isoformat(),
                       "next_due": (datetime.now(timezone.utc) + timedelta(hours=max(1, ivl))).isoformat()}
                _kv_write(_knowsrc_key(cur, gen_id), rec)
                _knowsrc_index_add(cur, gen_id)
                kn["saved_source"] = {"gen_id": gen_id, "interval_hours": rec["interval_hours"]}
            self._send({"status": "success" if kn.get("ok") else "error", "message": kn.get("err", ""),
                        "agent": {"id": cur, "name": nm}, "verify": out, "result": kn})
            return

        if self.path in ("/x/knowledge_refresh", "/x/knowledge_source_remove"):
            # A4: обновить знание из источника (заменить факты свежими) / убрать источник обновления
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            cur = _kv_read(CURAGENT_KV, "") or ""
            gid = str(body.get("gen_id", "")).strip()
            if not cur or not gid:
                self._send({"status": "error", "message": "нужен выбранный агент и gen_id"}, 400)
                return
            if self.path == "/x/knowledge_refresh":
                r = _knowledge_refresh(cur, gid)
                self._send({"status": "success" if r.get("ok") else "error", "message": r.get("err", ""),
                            "result": r, "sources": _knowsrc_list(cur)})
            else:
                rec = _kv_read(_knowsrc_key(cur, gid), None)
                api("/api/kv/delete", {"key": _knowsrc_key(cur, gid)})   # факты в мозге НЕ трогаем — просто перестаём обновлять
                _knowsrc_index_remove(cur, gid)
                self._send({"status": "success", "sources": _knowsrc_list(cur)})
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
                _secidx_mark(CLIENT_ID, kind, validated_ok=ok, err=(out.get("err") if isinstance(out, dict) else None))
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

        if self.path == "/x/sheet_link":
            # «Какой именно Google Sheets?» — вопрос Анвара. Раньше система молча брала таблицу
            # из ранее сохранённого секрета, и владелец не знал КАКУЮ (у него оказался «Семейный
            # бюджет» из старой отладки). Теперь: вставил ссылку → мы её разобрали, СХОДИЛИ в таблицу
            # и показали первые строки. Подтверждает человек, глядя на свои данные.
            # OAuth не нужен: таблица, открытая по ссылке, читается как есть — это самый короткий
            # честный путь, а сервис-аккаунт остаётся для закрытых таблиц.
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            url = str(body.get("url", "")).strip()
            m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
            if not m:
                self._send({"status": "error",
                            "message": "это не похоже на ссылку Google Sheets. Ожидаю адрес вида "
                                       "https://docs.google.com/spreadsheets/d/…"}, 400)
                return
            sheet_id = m.group(1)
            gid = ""
            g = re.search(r"[#&?]gid=(\d+)", url)
            if g:
                gid = g.group(1)
            # читаем ЧЕРЕЗ публичный экспорт прямо здесь — быстрый честный ответ без записи секрета
            try:
                _u = ("https://docs.google.com/spreadsheets/d/" + sheet_id +
                      "/export?format=csv" + (("&gid=" + gid) if gid else ""))
                _rq = urllib.request.Request(_u, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(_rq, timeout=45) as _r:
                    _ct = _r.headers.get("content-type", "")
                    _raw = _r.read().decode("utf-8", "replace")
            except Exception as e:
                self._send({"status": "error", "code": "no_access",
                            "message": "не смог открыть таблицу: " + _scrub(str(e)[:120]) +
                                       ". Откройте доступ «всем, у кого есть ссылка» (только чтение)."}, 502)
                return
            if "text/html" in _ct:
                self._send({"status": "error", "code": "no_access",
                            "message": "таблица закрыта: Google отдал страницу входа вместо данных. "
                                       "Откройте доступ «всем, у кого есть ссылка» (только чтение) и повторите."}, 502)
                return
            import csv as _csv
            import io as _io
            rows = list(_csv.reader(_io.StringIO(_raw)))
            # ищем ПЕРВУЮ непустую строку как заголовок: у живых таблиц сверху бывают пустые строки
            hdr_i, cols = -1, []
            for i, r in enumerate(rows[:15]):
                cells = [str(c).strip() for c in r]
                if sum(1 for c in cells if c) >= 2:
                    hdr_i, cols = i, [c for c in cells if c]
                    break
            preview = [[str(c)[:40] for c in r[:8]] for r in rows[hdr_i + 1:hdr_i + 4]] if hdr_i >= 0 else []
            self._send({"status": "success", "spreadsheet_id": sheet_id, "gid": gid,
                        "url": "https://docs.google.com/spreadsheets/d/" + sheet_id + (("/edit#gid=" + gid) if gid else ""),
                        "columns": cols, "header_row": hdr_i + 1,
                        "rows": max(0, len(rows) - hdr_i - 1) if hdr_i >= 0 else 0,
                        "preview": preview,
                        "warning": "" if cols else
                                   "в первых строках нет заголовков колонок — похоже, это не лист с данными "
                                   "(например, вкладка с инструкцией). Укажите ссылку на нужный лист (gid)."})
            return

        if self.path == "/x/sheet_use":
            # Владелец подтвердил таблицу глазами → сохраняем её в секрет коннектора и привязываем.
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            sheet_id = re.sub(r"[^a-zA-Z0-9-_]", "", str(body.get("spreadsheet_id", "")))[:80]
            gid = re.sub(r"[^0-9]", "", str(body.get("gid", "")))[:12]
            if not sheet_id:
                self._send({"status": "error", "message": "нет id таблицы"}, 400)
                return
            prev = _load_client_secret(CLIENT_ID, "src_gsheets") or {}
            if isinstance(prev, str):
                try:
                    prev = json.loads(prev)
                except Exception:
                    prev = {}
            cfg = dict(prev if isinstance(prev, dict) else {})
            cfg["spreadsheet_id"] = sheet_id      # сервис-аккаунт (если был) не трогаем — он для закрытых таблиц
            cfg["gid"] = gid
            if not _store_client_secret(CLIENT_ID, "src_gsheets", json.dumps(cfg, ensure_ascii=False)):
                self._send({"status": "error", "message": "не удалось сохранить настройку источника"}, 502)
                return
            self._send({"status": "success", "spreadsheet_id": sheet_id, "gid": gid})
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
            # первый pull — наполнить стор данными до первого прогона (доказывает доступ к источнику).
            # Таймаут 60с: привязка не должна висеть — если источник не отвечает, честный быстрый отказ.
            out = _run_source(kind, "pull", sid, skey, timeout=60)
            if not (isinstance(out, dict) and out.get("ok")):
                self._send({"status": "error", "message": "источник не отдал данные (проверьте доступ к таблице/листу и права): " + str((out or {}).get("err") or out)[:150], "result": out}, 502)
                return
            # Привязка ОБЯЗАНА доказать три вещи, иначе это ложный успех (жалоба Анвара:
            # «сказал, что привязал — а что привязал?»):
            #   1) данные пришли (уже проверено выше);
            #   2) известна СХЕМА — без колонок контроль дрифта и адаптеры мертвы молча;
            #   3) известно, ЧТО привязано — владелец должен видеть таблицу, а не слово «Google Sheets».
            _schema = _source_schema(out)
            _rows = (out or {}).get("rows")
            if not _schema:
                self._send({"status": "error", "code": "no_schema",
                            "message": "источник отдал данные, но не назвал колонки — привязку не подтверждаю: "
                                       "без структуры процесс не заметит, если источник её поменяет. "
                                       "Обновите коннектор источника и повторите."}, 502)
                return
            if not _rows:
                self._send({"status": "error", "code": "empty_source",
                            "message": "источник ответил, но строк в нём нет — привязывать нечего. "
                                       "Проверьте лист и диапазон."}, 502)
                return
            s["builds"][-1]["source_file"] = src_path
            s["source"] = {"kind": kind, "basename": basename, "source_key": skey,
                           "refresh": "per_run", "set_at": datetime.now(timezone.utc).isoformat(),
                           "schema": _schema,        # отпечаток структуры для контроля дрифта
                           "rows": _rows,
                           "identity": (out or {}).get("identity") or {}}   # ЧТО именно привязано
            s["updated_at"] = datetime.now(timezone.utc).isoformat()
            sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
            self._send({"status": "success", "source": s["source"], "rows": _rows,
                        "columns": _schema})
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
                      "llm_base_url": CONFIG.get("llm_base_url", ""),
                      "llm_model": CONFIG.get("llm_model", ""),
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
            # индекс коннекторов — только ПОСЛЕ успешной записи секрета (без «призраков»).
            # set_at + несекретный dest (куда шлём, #2); статус проверки проставит тест ниже (#10/#15).
            _secidx_mark(client, connector, set_at=datetime.now(timezone.utc).isoformat(), dest=_dest_hint(connector, value))
            self._send({"status": "success", "connector": connector, "stored": True})
            return

        if self.path == "/x/secret_adopt":
            # адопция найденного external-подключения в общий сейф. Пока один кейс: WhatsApp (GreenAPI),
            # который Travel-пак записал в config.json. Секрет собираем СЕРВЕРНО (в чат/лог не попадает).
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            connector = str(body.get("connector", "")).strip()
            if connector != "whatsapp":
                self._send({"status": "error", "message": "adopt поддерживает пока только whatsapp"}, 400)
                return
            gid, gtok = CONFIG.get("greenapi_id"), CONFIG.get("greenapi_token")
            if not gid or not gtok:
                self._send({"status": "error", "message": "в config.json нет greenapi_id/greenapi_token"}, 404)
                return
            to = str(body.get("to", "")).strip()   # получатель по умолчанию (номер) — опционально
            secret = {"provider": "green", "id_instance": str(gid), "api_token": str(gtok)}
            if to:
                secret["to"] = to
            ok = _store_client_secret(CLIENT_ID, "whatsapp", json.dumps(secret, ensure_ascii=False))
            self._send({"status": "success" if ok else "error", "connector": "whatsapp", "stored": bool(ok)})
            return

        if self.path == "/x/tourvisor_token":
            # Обновить JWT Tourvisor (живёт ~год, берётся в кабинете pro.tourvisor.ru → Интеграции).
            # Пишем в config.json — его читают ta_*-эксперты Travel-пака. Значение не логируем.
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            jwt = str(body.get("jwt", "")).strip()
            if jwt.count(".") != 2 or len(jwt) < 60:
                self._send({"status": "error", "message": "это не похоже на JWT (3 сегмента через точку)"}, 400)
                return
            exp_iso = None
            try:
                import base64 as _b64x
                _p = jwt.split(".")[1]
                _p += "=" * (-len(_p) % 4)
                _e = json.loads(_b64x.urlsafe_b64decode(_p)).get("exp")
                if _e:
                    exp_iso = datetime.fromtimestamp(int(_e), tz=timezone.utc).isoformat()
                    if int(_e) < datetime.now(timezone.utc).timestamp():
                        self._send({"status": "error", "message": "этот токен уже истёк (" + exp_iso[:10] + ") — возьмите свежий в кабинете"}, 400)
                        return
            except Exception:
                pass   # exp не читается — сохраняем без срока (проверится живым тестом)
            cfgp = APP_DIR / "config.json"
            cfg = json.loads(cfgp.read_text(encoding="utf-8"))
            cfg["tourvisor_jwt"] = jwt
            cfgp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            CONFIG["tourvisor_jwt"] = jwt   # горячее обновление без рестарта моста
            self._send({"status": "success", "expires": exp_iso})
            return

        if self.path == "/x/secret_remove":
            if self._blocked_origin():
                self._send({"status": "error", "message": "forbidden origin"}, 403)
                return
            client = CLIENT_ID
            connector = str(body.get("connector", "")).strip()
            r = api("/api/kv/remove", {"key": _secret_kvkey(client, connector)})
            removed = r.get("status") == "success"
            # #4 отзыв ≠ отзыв: индекс правился ДАЖЕ при провале удаления → карточка исчезала, а шифртекст
            # оставался в KV (мнимый disconnect). Индекс трогаем ТОЛЬКО после подтверждённого удаления.
            if not removed:
                self._send({"status": "error", "connector": connector, "removed": False,
                            "message": "секрет не удалён из хранилища — подключение НЕ отозвано, попробуйте ещё раз"}, 502)
                return
            try:
                idx = json.loads((api("/api/kv/get", {"key": _secidx_key(client)}) or {}).get("value") or "{}")
                idx.pop(connector, None)
                api("/api/kv/set", {"key": _secidx_key(client), "value": json.dumps(idx, ensure_ascii=False), "description": "secidx"})
            except Exception:
                pass
            self._send({"status": "success", "connector": connector, "removed": True})
            return

        if self.path == "/x/expert":
            expert = str(body.get("expert_name", ""))
            params = body.get("params") or {}
            # ta_tv_get — read-only тест Tourvisor из карточки подключения (справочник /departures бесплатный)
            if expert not in ("wz_session", "wz_generate_blueprint", "wz_project_spec", "wz_data_reality_check", "ta_tv_get"):
                self._send({"status": "error", "message": "expert not allowed via bridge"}, 403)
                return
            if expert == "ta_tv_get":
                params = {"path": "/departures"}   # фиксируем безопасный путь: только справочник, без платных поисков

            if expert in ("wz_generate_blueprint", "wz_data_reality_check"):
                params.setdefault("api_key", CONFIG.get("llm_api_key", ""))
                params.setdefault("base_url", CONFIG.get("llm_base_url", ""))
                params.setdefault("model", CONFIG.get("llm_model", ""))
                params.setdefault("api_token", CONFIG.get("auth_token", ""))            # платформенная модель, если api_key пуст (клиенту OpenAI-ключ не нужен)
                if expert == "wz_generate_blueprint":
                    _cp = _ensure_catalog_path()
                    if not _catalog_usable(_cp):
                        self._send({"status": "error", "code": "catalog_invalid",
                                    "message": "Каталог возможностей отсутствует или повреждён. Обновите Wizard и снова нажмите «Собрать план»."}, 503)
                        return
                    params.setdefault("catalog_path", str(_cp))
                # #12: язык blueprint следует за языком фактуры интервью (как gen_questions) — иначе всегда русский (US-демо ломался)
                if expert == "wz_generate_blueprint" and not params.get("language"):
                    _sidL = str(params.get("session_id", "")); _txtL = ""
                    try:
                        _spL = SESS_DIR / (_sidL + ".json")
                        if _sidL and _spL.exists():
                            _sdL = json.loads(_spL.read_text(encoding="utf-8"))
                            _txtL = str(_sdL.get("questionnaire_task", "")) + " " + " ".join(
                                str((v.get("answer") if isinstance(v, dict) else v) or "") for v in (_sdL.get("answers") or {}).values())
                    except Exception:
                        _txtL = ""
                    if _txtL.strip():
                        params["language"] = _task_lang(_txtL)
                # LLM-эксперт: ретрай + фолбэк по цепочке Qwen-агентов (устойчивость к флапу бэкенда)
                _llm_result = run_llm_expert(expert, params)
                if expert == "wz_generate_blueprint" and _llm_result.get("status") == "success":
                    _sid_bp = str(params.get("session_id", ""))
                    _bp_path = SESS_DIR / (_sid_bp + "_blueprint.json")
                    try:
                        _bp_doc = json.loads(_bp_path.read_text(encoding="utf-8"))
                    except Exception:
                        _bp_doc = None
                    if not _blueprint_doc_usable(_bp_doc):
                        self._send({"status": "error", "code": "blueprint_not_saved",
                                    "message": "Модель не сохранила полноценный план. Ничего не опубликовано; обновите Wizard и повторите «Собрать план»."}, 502)
                        return
                self._send(_llm_result)
            else:
                res = run_expert(expert, params, glob=True)   # эксперты визарда — global; без флага платформа их не находит ("Expert not found")
                # Ф2: помечаем НОВУЮ сессию текущим агентом (видно, для кого строим) — стемп в файл сессии
                if expert == "wz_session" and str(params.get("action")) == "create":
                    try:
                        pr = parse_expert_result(res) if isinstance(res, dict) else {}
                        sess = pr.get("session") if isinstance(pr.get("session"), dict) else {}
                        sid = sess.get("session_id") or pr.get("session_id") or pr.get("id")
                        cur = _kv_read(CURAGENT_KV, "") or ""
                        sp = SESS_DIR / (str(sid) + ".json") if sid else None
                        if sid and cur and SAFE_ID.match(str(sid)) and sp and sp.exists():
                            sd = json.loads(sp.read_text(encoding="utf-8"))
                            if not sd.get("agent_id"):
                                sd["agent_id"] = cur
                                sd["agent_name"] = next((a.get("name") for a in _agents_load() if a.get("id") == cur), cur)
                                sp.write_text(json.dumps(sd, ensure_ascii=False, indent=2), encoding="utf-8")
                    except Exception:
                        pass
                self._send(res)

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
                "llm_base_url": CONFIG.get("llm_base_url", ""),
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

        elif self.path == "/x/build_answer":
            sid = str(body.get("session_id", ""))
            previous_build_id = str(body.get("build_id", ""))
            answer = str(body.get("answer", "")).strip()[:6000]
            sp = SESS_DIR / (sid + ".json")
            bp = RUNS_DIR / previous_build_id / "build_progress.json"
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            if not SAFE_ID.match(previous_build_id or "") or not bp.exists():
                self._send({"status": "error", "message": "checkpoint сборки не найден"}, 404)
                return
            if not answer:
                self._send({"status": "error", "message": "ответ владельца пуст"}, 400)
                return
            progress = json.loads(bp.read_text(encoding="utf-8"))
            if progress.get("session_id") != sid or progress.get("status") != "waiting_for_owner":
                self._send({"status": "error", "message": "сборка уже не ждёт ответа"}, 409)
                return
            session = json.loads(sp.read_text(encoding="utf-8"))
            waiting = session.get("waiting_build") if isinstance(session.get("waiting_build"), dict) else {}
            if waiting and str(waiting.get("build_id") or "") != previous_build_id:
                self._send({"status": "error", "message": "этот вопрос уже не является текущим"}, 409)
                return
            question = str(progress.get("owner_question") or "").strip()
            if not question:
                self._send({"status": "error", "message": "в checkpoint нет вопроса владельцу"}, 409)
                return
            # Сначала убеждаемся, что продолжение вообще можно запустить. До этих проверок
            # checkpoint остаётся waiting_for_owner, а ответ не считается использованным.
            if not (SESS_DIR / (sid + "_blueprint.json")).exists():
                self._send({"status": "error", "message": "не найден план процесса"}, 409)
                return
            _pf = sample_preflight(sid)
            if not _pf.get("ok"):
                self._send({"status": "error", "code": "sample_required", "message": _pf.get("message")}, 400)
                return
            def save_answer(s):
                apply_owner_clarification(s, question, answer, previous_build_id)

            _update_session(sid, save_answer)
            answer_id = "builder_clarification_" + hashlib.sha256(question.encode("utf-8")).hexdigest()[:12]
            progress["status"] = "resumed"
            progress["owner_answer"] = answer
            progress["resumed_at"] = datetime.now(timezone.utc).isoformat()
            progress["updated_at"] = progress["resumed_at"]
            bp.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
            build_id = _start_build_job(sid)
            progress["next_build_id"] = build_id
            bp.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
            self._send({"status": "success", "build_id": build_id, "answer_id": answer_id,
                        "message": "ответ добавлен в Task Contract; стройка продолжена"})

        elif self.path == "/x/build":
            sid = str(body.get("session_id", ""))
            if not SAFE_ID.match(sid or "") or not (SESS_DIR / (sid + ".json")).exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            _build_bp_path = SESS_DIR / (sid + "_blueprint.json")
            try:
                _build_bp_doc = json.loads(_build_bp_path.read_text(encoding="utf-8"))
            except Exception:
                _build_bp_doc = None
            if not _blueprint_doc_usable(_build_bp_doc):
                self._send({"status": "error", "message": "сначала соберите план процесса"}, 400)
                return
            _pf = sample_preflight(sid)   # WZ-07: честный отказ ДО стройки, не после минут сборки
            if not _pf.get("ok"):
                self._send({"status": "error", "code": "sample_required", "message": _pf.get("message")}, 400)
                return
            build_id = _start_build_job(sid)
            self._send({"status": "success", "build_id": build_id})

        elif self.path == "/x/rebuild":
            # C4.2 (CABINET_TZ §5.1, канон пересборки): авто-Строитель — правка логики процесса из чата.
            # Канон F2: НЕ правим живой код руками — обновляем blueprint + ЗАПИСЫВАЕМ РЕШЕНИЕ в сессию,
            # затем штатная пересборка (_run_build: те же имена экспертов, builds[] += новая запись).
            # На время стройки процесс автоматически на паузе (тик не запустит полусобранное).
            sid = str(body.get("session_id", ""))
            change = str(body.get("change", "")).strip()[:500]
            sp = SESS_DIR / (sid + ".json")
            bpp = SESS_DIR / (sid + "_blueprint.json")
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            if not change:
                self._send({"status": "error", "message": "нет change"}, 400)
                return
            if not bpp.exists():
                self._send({"status": "error", "message": "у процесса нет blueprint — пересборка только через Мастера"}, 400)
                return
            # C6-гард: не пускать параллельную стройку той же сессии (rebuild против rollback)
            _bactive = json.loads(sp.read_text(encoding="utf-8")).get("building")
            if _bactive:
                _bp2 = RUNS_DIR / str(_bactive) / "build_progress.json"
                try:
                    _brun = json.loads(_bp2.read_text(encoding="utf-8")).get("status") == "running"
                except Exception:
                    _brun = False
                if _brun:
                    self._send({"status": "error", "message": "стройка уже идёт — дождитесь её завершения"}, 409)
                    return
            _pf = sample_preflight(sid)   # WZ-07: пересборка тоже гоняет срез — образец нужен ДО
            if not _pf.get("ok"):
                self._send({"status": "error", "code": "sample_required", "message": _pf.get("message")}, 400)
                return
            ag = qwen_agent()
            bdoc = json.loads(bpp.read_text(encoding="utf-8"))
            bp = bdoc.get("blueprint") or {}
            stages = bp.get("stages") or []
            if not stages:
                self._send({"status": "error", "message": "в blueprint нет стадий"}, 400)
                return
            # 1) Qwen выбирает стадию и переписывает её описание с учётом правки
            _st_brief = "\n".join("- id=%s · %s: %s" % (st.get("id"), st.get("title"), str(st.get("business_description", ""))[:140])
                                  for st in stages)
            prompt = ("Ты — Строитель процессов Extella. Владелец просит изменить процесс. Верни ТОЛЬКО JSON:\n"
                      '{"stage_id":"<id затронутой стадии>","new_description":"<НОВОЕ business_description этой стадии: '
                      'прежний смысл + правка владельца, тем же языком>","decision":"<решение одной фразой: что и зачем меняем>"}\n\n'
                      "Стадии процесса:\n" + _st_brief + "\n\nПравка владельца: " + change)
            try:
                res = api("/api/agent/run", {"agent_id": ag, "input": prompt, "run_timeout": 90,
                                             "store": False, "temperature": 0}, timeout=100)
                text = ""
                for it in (res or {}).get("output", []):
                    if isinstance(it, dict) and it.get("type") == "message":
                        for c in it.get("content", []):
                            if isinstance(c, dict) and c.get("type") == "output_text":
                                text += c.get("text", "")
                text = text or (res or {}).get("output_text", "")
                m = re.search(r"\{.*\}", text, re.S)
                v = json.loads(m.group(0)) if m else {}
            except Exception as e:
                self._send({"status": "error", "message": "Строитель не разобрал правку: " + _scrub(str(e)[:120])})
                return
            st_id = str(v.get("stage_id", ""))
            new_desc = str(v.get("new_description", "")).strip()
            decision = str(v.get("decision", change))[:300]
            hit = next((st for st in stages if str(st.get("id")) == st_id), None)
            if not hit or not new_desc:
                self._send({"status": "error", "message": "Строитель не определил стадию — уточните правку"}, 422)
                return
            # C6: снапшот blueprint ДО правки — фундамент отката (последние 5; ~2КБ каждый)
            try:
                _snap = json.loads(bpp.read_text(encoding="utf-8")).get("blueprint")
                def _hist(s):
                    h = s.setdefault("blueprint_history", [])
                    h.append({"at": datetime.now(timezone.utc).isoformat(),
                              "blueprint": _snap, "before_change": change[:200]})
                    s["blueprint_history"] = h[-5:]
                _update_session(sid, _hist)
            except Exception:
                pass
            # 2) blueprint обновлён + решение записано в сессию (канон: решения живут в сессии)
            hit["business_description"] = new_desc[:600]
            bdoc["revised_at"] = datetime.now(timezone.utc).isoformat()
            bpp.write_text(json.dumps(bdoc, ensure_ascii=False, indent=2), encoding="utf-8")
            _dec = {"at": datetime.now(timezone.utc).isoformat(), "change": change,
                    "stage_id": st_id, "stage_title": hit.get("title"), "decision": decision, "by": "builder-chat"}
            _update_session(sid, lambda s: s.setdefault("decisions", []).append(_dec))
            # 3) авто-пауза расписания на время стройки (вернём после)
            _was_active = False
            try:
                gv = api("/api/kv/get", {"key": "sched:" + sid})
                cv = gv.get("value") if isinstance(gv, dict) else None
                if cv:
                    cfg = json.loads(cv)
                    _was_active = bool(cfg.get("active", True))
                    if _was_active:
                        cfg["active"] = False
                        api("/api/kv/set", {"key": "sched:" + sid, "value": json.dumps(cfg, ensure_ascii=False),
                                            "description": "schedule " + sid})
            except Exception:
                _was_active = False
            # 4) штатная пересборка в фоне; после — вернуть расписание
            build_id = "build_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M") + "_" + uuid.uuid4().hex[:4]
            (RUNS_DIR / build_id).mkdir(parents=True, exist_ok=True)
            (RUNS_DIR / build_id / "build_progress.json").write_text(
                json.dumps({"build_id": build_id, "session_id": sid, "status": "running", "stages": [],
                            "kind": "rebuild", "resume_sched": _was_active},   # F1: журнал работы для recovery
                           ensure_ascii=False), encoding="utf-8")

            _update_session(sid, lambda sx: sx.__setitem__("building", build_id))

            def _rebuild_bg():
                _built_ok = False
                try:
                    _run_build(sid, build_id)
                    try:
                        _built_ok = json.loads((RUNS_DIR / build_id / "build_progress.json").read_text(encoding="utf-8")).get("status") == "built"
                    except Exception:
                        _built_ok = False
                finally:
                    _update_session(sid, lambda sx: sx.pop("building", None))
                    if not _built_ok:   # #23: стройка упала → откатить blueprint к снапшоту (иначе план впереди собранного кода)
                        try:
                            _hist = json.loads((SESS_DIR / (sid + ".json")).read_text(encoding="utf-8")).get("blueprint_history") or []
                            if _hist and _hist[-1].get("blueprint"):
                                bpp.write_text(json.dumps({"blueprint": _hist[-1]["blueprint"],
                                                           "reverted_at": datetime.now(timezone.utc).isoformat()},
                                                          ensure_ascii=False, indent=2), encoding="utf-8")
                        except Exception:
                            pass
                    if _was_active:   # resume: возвращаем расписание, next_due вперёд
                        try:
                            gv2 = api("/api/kv/get", {"key": "sched:" + sid})
                            cv2 = gv2.get("value") if isinstance(gv2, dict) else None
                            if cv2:
                                cfg2 = json.loads(cv2)
                                cfg2["active"] = True
                                _iv = int(cfg2.get("interval_min", 0) or 0)
                                if _iv:
                                    cfg2["next_due_ts"] = (datetime.now(timezone.utc) + timedelta(minutes=_iv)).isoformat()
                                api("/api/kv/set", {"key": "sched:" + sid, "value": json.dumps(cfg2, ensure_ascii=False),
                                                    "description": "schedule " + sid})
                        except Exception:
                            pass

            threading.Thread(target=_rebuild_bg, daemon=True).start()
            self._send({"status": "success", "build_id": build_id, "stage_id": st_id,
                        "stage_title": hit.get("title"), "decision": decision, "paused_for_build": _was_active})

        elif self.path == "/x/rollback":
            # C6: откат последней правки — восстановить blueprint из снапшота + штатная пересборка.
            # «Правка человека всегда обратима»: builds[] копит версии, здесь — кнопка к ним.
            sid = str(body.get("session_id", ""))
            sp = SESS_DIR / (sid + ".json")
            bpp = SESS_DIR / (sid + "_blueprint.json")
            if not SAFE_ID.match(sid or "") or not sp.exists() or not bpp.exists():
                self._send({"status": "error", "message": "session/blueprint not found"}, 404)
                return
            s = json.loads(sp.read_text(encoding="utf-8"))
            hist = s.get("blueprint_history") or []
            if not hist:
                self._send({"status": "error", "message": "нет сохранённых версий для отката"}, 400)
                return
            # C6-гард: не пускать параллельную стройку той же сессии (rebuild против rollback)
            _bactive = json.loads(sp.read_text(encoding="utf-8")).get("building")
            if _bactive:
                _bp2 = RUNS_DIR / str(_bactive) / "build_progress.json"
                try:
                    _brun = json.loads(_bp2.read_text(encoding="utf-8")).get("status") == "running"
                except Exception:
                    _brun = False
                if _brun:
                    self._send({"status": "error", "message": "стройка уже идёт — дождитесь её завершения"}, 409)
                    return
            snap = hist[-1]
            bdoc = json.loads(bpp.read_text(encoding="utf-8"))
            bdoc["blueprint"] = snap.get("blueprint") or bdoc.get("blueprint")
            bdoc["revised_at"] = datetime.now(timezone.utc).isoformat()
            bpp.write_text(json.dumps(bdoc, ensure_ascii=False, indent=2), encoding="utf-8")
            _dec = {"at": datetime.now(timezone.utc).isoformat(),
                    "change": "откат правки: «" + str(snap.get("before_change", ""))[:120] + "»",
                    "decision": "восстановлена версия blueprint от " + str(snap.get("at", ""))[:16].replace("T", " "),
                    "by": "rollback"}

            def _mu(sx):
                sx.setdefault("decisions", []).append(_dec)
                sx["blueprint_history"] = (sx.get("blueprint_history") or [])[:-1]
            _update_session(sid, _mu)
            # авто-пауза + пересборка + resume — как в /x/rebuild
            _was_active = False
            try:
                gv = api("/api/kv/get", {"key": "sched:" + sid})
                cv = gv.get("value") if isinstance(gv, dict) else None
                if cv:
                    cfg = json.loads(cv)
                    _was_active = bool(cfg.get("active", True))
                    if _was_active:
                        cfg["active"] = False
                        api("/api/kv/set", {"key": "sched:" + sid, "value": json.dumps(cfg, ensure_ascii=False),
                                            "description": "schedule " + sid})
            except Exception:
                _was_active = False
            build_id = "build_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M") + "_" + uuid.uuid4().hex[:4]
            (RUNS_DIR / build_id).mkdir(parents=True, exist_ok=True)
            (RUNS_DIR / build_id / "build_progress.json").write_text(
                json.dumps({"build_id": build_id, "session_id": sid, "status": "running", "stages": [],
                            "kind": "rollback", "resume_sched": _was_active},   # F1: журнал работы для recovery
                           ensure_ascii=False), encoding="utf-8")

            _update_session(sid, lambda sx: sx.__setitem__("building", build_id))

            def _rb_bg():
                try:
                    _run_build(sid, build_id)
                finally:
                    _update_session(sid, lambda sx: sx.pop("building", None))
                    if _was_active:
                        try:
                            gv2 = api("/api/kv/get", {"key": "sched:" + sid})
                            cv2 = gv2.get("value") if isinstance(gv2, dict) else None
                            if cv2:
                                cfg2 = json.loads(cv2)
                                cfg2["active"] = True
                                _iv = int(cfg2.get("interval_min", 0) or 0)
                                if _iv:
                                    cfg2["next_due_ts"] = (datetime.now(timezone.utc) + timedelta(minutes=_iv)).isoformat()
                                api("/api/kv/set", {"key": "sched:" + sid, "value": json.dumps(cfg2, ensure_ascii=False),
                                                    "description": "schedule " + sid})
                        except Exception:
                            pass

            threading.Thread(target=_rb_bg, daemon=True).start()
            self._send({"status": "success", "build_id": build_id, "restored_from": snap.get("at"),
                        "decision": _dec["decision"]})

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
            # WZ-09/AC-20 (ТЗ v2): это ОДОБРЕНИЕ, не запуск — Run создаёт только /x/run_process.
            # Ответ говорит это явно, чтобы UI не рисовал «запущено» без прогона.
            self._send({"status": "success", "approved": True, "run_created": False,
                        "message": "процесс одобрен; реальный прогон — /x/run_process",
                        "experts": builds[-1].get("experts", [])})

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
            _tp = _target_preflight(s, mode="manual")   # T2 + A5: требования и дрифт устройства — ДО прогона
            if _tp.get("ok"):
                _tp = _placement_preflight(s, mode="manual")   # A1: устройство КАЖДОЙ стадии на связи
            if not _tp.get("ok"):
                # A3: заблокированный прогон оставляет след — иначе оператор видит тишину
                _record_blocked(sid, _tp.get("code") or "preflight", _tp.get("message") or "прогон заблокирован")
                self._send({"status": "error", "code": _tp.get("code"), "message": _tp.get("message")}, 412)
                return
            orch = builds[-1]["orchestrator"]
            lb = builds[-1]   # F2: действующая сборка (params_contract решает, передавать ли rules/fields)
            # Агентная стройка принимает явную папку результата. На проверочном прогоне её
            # создаёт Строитель, а повторный запуск из кабинета раньше передавал только source_file —
            # доказанное решение закономерно отвечало «Не указан output_dir». Отдельный каталог
            # на каждый прогон не смешивает отчёты и остаётся доступен для _persist_run_reports.
            _agentic_run_id = ""
            _agentic_output_dir = ""
            if int(lb.get("agentic_contract", 0) or 0) >= 1:
                _agentic_run_id = "run_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
                _aout = RUNS_DIR / "process_runs" / sid / _agentic_run_id
                _aout.mkdir(parents=True, exist_ok=True)
                _agentic_output_dir = str(_aout)
            if isinstance(orch, dict):
                orch = orch.get("expert_name")
            if orch == "ci_run_pipeline":
                # API-based процесс (источники по сети, без source_file): гоняем server-side через run_expert
                # (надёжный синтез Qwen — локальный exec в HTTP-хендлере обрывает длинный /api/agent/run).
                # agent_id = живой Qwen клиента (старый fine-tune agent_iVWW… удалён с платформы → 404)
                res, _att = _run_expert_resilient("ci_run_pipeline", {"agent_id": qwen_agent(), "deliver": "none",
                                 "api_token": CONFIG.get("auth_token", "")}, wait=240)
                if not isinstance(res, dict) or res.get("status") != "success":
                    self._send({"status": "error", "message": "run: " + str(res)[:180], "attempts": _att})
                    return
                digest = res.get("digest_md") or res.get("digest_preview", "")
                _save_digest(sid, digest)   # C3: виджет «Последний результат»
                run_rec = {"trigger": "manual", "at": datetime.now(timezone.utc).isoformat(), "status": res.get("status"),
                           "attempts": _att, "findings": res.get("findings"), "digest_source": res.get("digest_source")}
                s = _update_session(sid, lambda s: s.setdefault("runs", []).append(run_rec))
                self._send({"status": "success", "run": run_rec, "digest": digest,
                            "findings": res.get("findings"), "gaps": res.get("knowledge_gaps")})
                return
            if orch == "wz_flow_run":
                # Задача, собранная Композитором и сохранённая как автоматизация (B-lite):
                # прогон = wz_flow_run по flow_id из билда; результат — бриф (digest).
                fid = str(builds[-1].get("flow_id") or "")
                if not fid:
                    self._send({"status": "error", "message": "у задачи нет flow_id"}, 400)
                    return
                _fl_params = {"flow_id": fid, "agent_id": qwen_agent(), "api_token": CONFIG.get("auth_token", "")}
                if s.get("rules"):
                    _fl_params["rules"] = json.dumps(s["rules"], ensure_ascii=False)
                if s.get("fields"):
                    _fl_params["fields"] = json.dumps(s["fields"], ensure_ascii=False)
                res, _att = _run_expert_resilient("wz_flow_run", _fl_params, wait=260)
                ok = isinstance(res, dict) and res.get("status") == "success"
                digest = ((res or {}).get("digest_md") or (res or {}).get("digest") or "") if ok else ""
                _save_digest(sid, digest)   # C3
                _run_at = datetime.now(timezone.utc).isoformat()
                run_rec = {"trigger": "manual", "at": _run_at, "attempts": _att,
                           "status": ((res or {}).get("run_status") or (res or {}).get("status") or "error"),
                           "digest_source": "flow", "flow_id": fid}
                s = _update_session(sid, lambda s: s.setdefault("runs", []).append(run_rec))
                if not ok:
                    self._send({"status": "error", "run": run_rec,
                                "message": _scrub((res or {}).get("message", str(res)[:180]) if isinstance(res, dict) else str(res)[:180])})
                    return
                # получатели кабины: короткое уведомление по шаблону (сам бриф остаётся в приложении)
                delivered = None
                recips = _recipients(s)
                if recips:
                    msg = _render_msg(s.get("message_template"), s.get("client_name") or sid, None, None)
                    delivered = []
                    for deliver in recips:
                        # идемпотентность: один и тот же отчёт в один канал за этот прогон — не дублируем
                        if not _deliver_once(sid + "|" + _run_at + "|" + deliver):
                            delivered.append({"channel": deliver, "ok": True, "skipped": "already_delivered"})
                            continue
                        dr = api("/api/expert/run", {"expert_name": "wz_connector_" + deliver, "global": True, "target": HOST_TARGET,
                                                     "params": {"api_token": CONFIG["auth_token"], "client": CLIENT_ID, "mode": "send", "text": msg}}, 60)
                        dout = dr.get("result", dr)
                        if isinstance(dout, str):
                            try:
                                dout = json.loads(dout)
                            except Exception:
                                dout = {}
                        delivered.append({"channel": deliver, "ok": bool(isinstance(dout, dict) and dout.get("ok")),
                                          "err": ((dout or {}).get("err") if isinstance(dout, dict) else None)})
                self._send({"status": "success", "run": run_rec, "digest": digest,
                            "warnings": (res or {}).get("warnings") or [], "delivered": delivered})
                return
            src = body.get("source_file") or builds[-1].get("source_file")
            if not src:
                # API-процесс без файла (напр. ta_run_lead_pipeline: лиды из каналов, не выгрузка) —
                # запускаем оркестратор как есть; раньше тут была ошибка «нет исходного файла».
                _op = {"api_token": CONFIG.get("auth_token", ""), "agent_id": qwen_agent()}
                if int(lb.get("params_contract", 0) or 0) >= 1:   # F2: только контрактным (старые упадут на лишних kwargs)
                    if s.get("rules"):
                        _op["rules_json"] = json.dumps(_rules_payload(s), ensure_ascii=False)
                    if s.get("fields"):
                        _op["fields_json"] = json.dumps(s["fields"], ensure_ascii=False)
                res, _att = _run_expert_resilient(orch, _op, wait=600)
                ok = isinstance(res, dict) and str(res.get("status", "")) in ("success", "partial")
                digest = ((res or {}).get("digest_md") or (res or {}).get("digest") or "") if isinstance(res, dict) else ""
                _save_digest(sid, digest)   # C3
                summ = (res or {}).get("summary") if isinstance(res, dict) else None
                run_rec = {"trigger": "manual", "at": datetime.now(timezone.utc).isoformat(),
                           "status": (res or {}).get("status", "error") if isinstance(res, dict) else "error",
                           "attempts": _att,
                           "summary": summ if isinstance(summ, str) else (json.dumps(summ, ensure_ascii=False)[:300] if summ else None),
                           "api_based": True}
                s = _update_session(sid, lambda s: s.setdefault("runs", []).append(run_rec))
                if not ok:
                    self._send({"status": "error", "run": run_rec,
                                "message": _scrub((res or {}).get("message", str(res)[:200]) if isinstance(res, dict) else str(res)[:200])})
                    return
                self._send({"status": "success", "run": run_rec, "digest": digest,
                            "result": {k: v for k, v in (res or {}).items() if k in ("processed", "leads", "drafts", "found", "sent", "summary", "status")}})
                return
            if s.get("source"):
                # процесс-на-источнике: свежий pull → прогон на хостинге с source_key (резолвер материализует)
                _si = s["source"]
                _skey = _si.get("source_key") or _file_key(sid, _si.get("basename", ""))
                _pout = _run_source(_si.get("kind"), "pull", sid, _skey)
                if not (isinstance(_pout, dict) and _pout.get("ok")):
                    self._send({"status": "error", "message": "источник не отдал данные: " + str((_pout or {}).get("err") or _pout)[:170]})
                    return
                # дрифт структуры источника: колонки изменились с момента привязки → прогон по чужой схеме = мусор.
                # ЛОМАЮЩИЙ дрифт (исчезли колонки) — стоп до прогона; мягкий (добавились) — предупреждение.
                _drift = _schema_drift(_si.get("schema"), _source_schema(_pout))
                if _drift.get("breaking"):
                    # AC-05: дрифт — это не тупик. Предлагаем адаптер «новая выгрузка → поля процесса»,
                    # человек подтверждает одним действием, процесс едет дальше. Останавливаемся всё равно:
                    # молча считать по чужой схеме нельзя.
                    _prop = _adapter_propose(_source_schema(_pout), _si.get("schema") or [])
                    _msg = ("источник изменил структуру: исчезли колонки " +
                            ", ".join(_drift.get("removed", [])) + ". ")
                    _msg += ("Похоже, их просто переименовали — предлагаю подстроить процесс под новую выгрузку."
                             if _prop.get("map") else "Сопоставить новые колонки с полями процесса не удалось — проверьте источник.")
                    _record_blocked(sid, "source_drift", _msg)   # A3: след в истории → процесс всплывёт в инбоксе
                    self._send({"status": "error", "code": "source_drift", "message": _msg, "drift": _drift,
                                "adapter_proposal": _prop.get("map") or {},
                                "adapter_unmatched": _prop.get("unmatched") or [],
                                "columns_now": _source_schema(_pout)}, 409)
                    return
                _fp = {"api_token": CONFIG["auth_token"], "source_file": src,
                       "source_key": _skey, "target": HOST_TARGET}
                if _agentic_output_dir:
                    _fp["output_dir"] = _agentic_output_dir
                    _fp["run_id"] = _agentic_run_id
                _pmap = _placement_resolve(s) if int(lb.get("placement_contract", 0) or 0) >= 1 else {}
                if _pmap:   # A1: карту понимают только оркестраторы новой сборки — старые упадут на лишнем kwarg
                    _fp["placement_json"] = json.dumps(_pmap, ensure_ascii=False)
                _adp = _adapter_active(sid) if int(lb.get("adapter_contract", 0) or 0) >= 1 else {}
                if _adp.get("map"):   # AC-05: подстройка выгрузки под поля процесса
                    _fp["adapter_json"] = json.dumps({"map": _adp["map"]}, ensure_ascii=False)
                _rspec = s.get("report_spec") if int(lb.get("report_contract", 0) or 0) >= 1 else None
                if isinstance(_rspec, dict) and _rspec:   # фирменный PDF по спеке вида
                    _fp["report_spec_json"] = json.dumps(_rspec, ensure_ascii=False)
                if int(lb.get("params_contract", 0) or 0) >= 1:   # F2
                    if s.get("rules"):
                        _fp["rules_json"] = json.dumps(_rules_payload(s), ensure_ascii=False)
                    if s.get("fields"):
                        _fp["fields_json"] = json.dumps(s["fields"], ensure_ascii=False)
                res, _att = _run_expert_resilient(orch, _fp, wait=900)
            else:
                _drift = {"drift": False}
                _fp2 = {"api_token": CONFIG["auth_token"], "source_file": src}
                if _agentic_output_dir:
                    _fp2["output_dir"] = _agentic_output_dir
                    _fp2["run_id"] = _agentic_run_id
                _pmap = _placement_resolve(s) if int(lb.get("placement_contract", 0) or 0) >= 1 else {}
                if _pmap:   # A1: карту понимают только оркестраторы новой сборки — старые упадут на лишнем kwarg
                    _fp2["placement_json"] = json.dumps(_pmap, ensure_ascii=False)
                _adp = _adapter_active(sid) if int(lb.get("adapter_contract", 0) or 0) >= 1 else {}
                if _adp.get("map"):   # AC-05: подстройка выгрузки под поля процесса
                    _fp2["adapter_json"] = json.dumps({"map": _adp["map"]}, ensure_ascii=False)
                _rspec = s.get("report_spec") if int(lb.get("report_contract", 0) or 0) >= 1 else None
                if isinstance(_rspec, dict) and _rspec:   # фирменный PDF по спеке вида
                    _fp2["report_spec_json"] = json.dumps(_rspec, ensure_ascii=False)
                if int(lb.get("params_contract", 0) or 0) >= 1:   # F2
                    if s.get("rules"):
                        _fp2["rules_json"] = json.dumps(_rules_payload(s), ensure_ascii=False)
                    if s.get("fields"):
                        _fp2["fields_json"] = json.dumps(s["fields"], ensure_ascii=False)
                res, _att = _run_expert_resilient(orch, _fp2, wait=900)
            # WZ-10 (ТЗ v2 §25): финал прогона честный — success без сводки и счётчиков не бывает
            if isinstance(res, dict) and res.get("status") == "success" \
               and res.get("total_count") is None and not res.get("summary"):
                res["status"] = "partial"
                res["message"] = "оркестратор не вернул сводку (summary/total_count) — проверьте процесс"
            # Мусорное веб-обогащение (поиск по числам/заголовкам) НЕ имеет права выехать как «success»:
            # честно понижаем в «требует доводки» и НЕ доставляем результат наружу (Гульжан, 20.07).
            _wj = _web_junk_reason(res) if isinstance(res, dict) and res.get("status") in ("success", "partial") else ""
            if _wj:
                res["status"] = "needs_review"
                res["needs_review_reason"] = _wj
            _persist_run_reports(sid, res)   # отчёт → долговечная папка + стор (переживает /tmp; скачивается с любого устройства)
            summ = res.get("summary") if isinstance(res, dict) else None
            run_rec = {"trigger": "manual", "at": datetime.now(timezone.utc).isoformat(),
                       "status": (res or {}).get("status", "unknown"), "attempts": _att,
                       "summary": summ, "total_count": (res or {}).get("total_count"),
                       "total_sum": (res or {}).get("total_sum"),
                       "report_md": (res or {}).get("report_md"), "report_xlsx": (res or {}).get("report_xlsx"),
                       "source_file": src}
            if _drift.get("drift"):   # мягкий дрифт (добавились колонки) — не блок, но помечаем прогон
                run_rec["source_drift"] = {"added": _drift.get("added", []), "removed": _drift.get("removed", [])}
            if (res or {}).get("needs_review_reason"):   # WZ-B02 → Exception Inbox: причина словами
                run_rec["needs_review_reason"] = str(res["needs_review_reason"])[:200]
            s = _update_session(sid, lambda s: s.setdefault("runs", []).append(run_rec))
            _dg = _run_digest(res)
            _save_digest(sid, _dg)   # C3: и процесс-на-файле оставляет отчёт (виджет/«Открыть отчёт»/расписание)
            # доставка результата в канал (как по расписанию) — чтобы РУЧНОЙ запуск тоже слал, а не только тик
            delivered = None
            recips = _recipients(s)   # несколько получателей: шлём результат в каждый подключённый канал
            if recips and (res or {}).get("status") == "success":
                tc = (res or {}).get("total_count"); ts = (res or {}).get("total_sum")
                _nm = s.get("client_name") or sid
                msg = _render_msg(s.get("message_template"), _nm, tc, ts)   # шаблон автоматизации (кабина «Шаблон»)
                delivered = []
                # ДОКУМЕНТ ВЛОЖЕНИЕМ и при ручном запуске — иначе оформленный отчёт остаётся
                # лежать на устройстве, а получателю уходит один текст.
                _doc = ""
                for _k in ("report_pdf", "report_docx", "report_pptx", "report_xlsx"):
                    if (res or {}).get(_k):
                        _doc = str(res[_k])
                        break
                for deliver in recips:
                    _dp = {"api_token": CONFIG["auth_token"], "client": CLIENT_ID, "mode": "send", "text": msg}
                    if _doc and deliver == "telegram":
                        _dp["mode"], _dp["file_path"] = "send_document", _doc
                    elif _doc and deliver == "email":
                        _dp["file_path"], _dp["subject"] = _doc, str(_nm)[:120]
                    dr = api("/api/expert/run", {"expert_name": "wz_connector_" + deliver, "global": True, "target": HOST_TARGET,
                                                 "params": _dp}, 90)
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
                    delivered.append({"channel": deliver, "ok": bool(isinstance(dout, dict) and dout.get("ok")),
                                      "err": ((dout or {}).get("err") if isinstance(dout, dict) else None)})
            _rs = (run_rec or {}).get("status")   # честный статус: раньше слали success даже при error оркестратора
            _herr = _run_error_human(res) if _rs not in ("success", "partial") else None
            if _herr:
                # причина ДОЛЖНА оставаться в истории: раньше в записи прогона был только
                # «error», и через час никто уже не мог сказать, что именно сломалось
                try:
                    def _mut_err(ss):
                        for _r in reversed(ss.get("runs") or []):
                            if _r.get("at") == (run_rec or {}).get("at"):
                                _r["error"] = _herr
                                break
                    _update_session(sid, _mut_err)
                except Exception:
                    pass
            self._send({"status": "success" if _rs in ("success", "partial") else "error",
                        "run": run_rec, "delivered": delivered, "digest": _dg,
                        "error": _herr,
                        "message": ((_herr or {}).get("message") or str(run_rec.get("needs_review_reason") or "прогон не дал результата")
                                    if _rs not in ("success", "partial") else None)})

        elif self.path == "/x/schedule":
            sid = str(body.get("session_id", ""))
            sp = SESS_DIR / (sid + ".json")
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            s = json.loads(sp.read_text(encoding="utf-8"))
            period = str(body.get("period", ""))[:40]
            interval_min = int(body.get("interval_min", 0) or 0)
            if period:
                _tp = _target_preflight(s, mode="schedule")   # T2: local_only не пускаем на хостинг
                if _tp.get("ok"):
                    _tp = _placement_preflight(s, mode="schedule")   # A1
                if not _tp.get("ok"):
                    self._send({"status": "error", "code": _tp.get("code"), "message": _tp.get("message")}, 412)
                    return
                _vd = ((s.get("builds") or [{}])[-1].get("audit") or {}).get("verdict", "")
                if _vd == "escalate" and not body.get("confirmed"):   # одобрение≠запуск: тот же гейт, что в /x/deploy
                    self._send({"status": "error", "code": "escalate",
                                "message": "проверка безопасности требует вмешательства команды — расписание заблокировано"}, 403)
                    return
            lb = (s.get("builds") or [{}])[-1]
            orch = lb.get("orchestrator")
            src = lb.get("source_file")
            if period and int(lb.get("agentic_contract", 0) or 0) >= 1 and src and Path(src).is_dir():
                self._send({"status": "error", "code": "local_bundle_schedule_unsupported",
                            "message": "процесс принимает пакет из нескольких локальных файлов. "
                                       "Ручной прогон уже работает, но расписание на хостинге не видит папку "
                                       "этого устройства. Подключите источник пакетов или дождитесь "
                                       "device-local планировщика; данные никуда не отправлены."}, 409)
                return
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
                _fid = str(lb.get("flow_id") or "")   # composed-задача Композитора (flow вместо файла)
                # процесс по расписанию гоняем на хостинге 24/7; flow — НЕ пиним (локальная модель на устройстве клиента)
                _tgt = str(body.get("target", "" if _fid else HOST_TARGET))
                s["schedule"] = {"period": period, "interval_min": interval_min,
                                 "set_at": datetime.now(timezone.utc).isoformat(), "orchestrator": orch,
                                 "target": _tgt}
                _pc = int(lb.get("params_contract", 0) or 0)   # F2: тик передаёт rules/fields только контрактным
                _deliver = str(body.get("deliver", "") or "")   # напр. "telegram" — куда слать результат прогона
                _rcpts = _recipients(s) or ([_deliver] if _deliver else [])   # несколько получателей → тик шлёт в каждый
                # общий стор для тика планировщика на always-on устройстве
                kvval = {"session_id": sid, "period": period, "interval_min": interval_min,
                         "orchestrator": orch, "source_file": src, "source_basename": _bn,
                         "source_key": _skey, "target": _tgt, "active": True,
                         "deliver": _deliver, "recipients": _rcpts,
                         "message_template": s.get("message_template") or "",
                         "name": s.get("client_name") or sid, "client": CLIENT_ID,
                         "next_due_ts": datetime.now(timezone.utc).isoformat(),
                         "runs": ((api("/api/kv/get", {"key": kvkey}) or {}).get("value") and
                                  _safe_runs(api("/api/kv/get", {"key": kvkey}))) or []}
                if s.get("source"):
                    kvval["source"] = s["source"]   # B3: тик сделает свежий pull источника ПЕРЕД прогоном
                if _fid:
                    kvval["flow_id"] = _fid            # тик запустит wz_flow_run по плану из KV
                    kvval["agent_id"] = qwen_agent()   # живой Qwen клиента для синтеза брифа
                if s.get("rules"):
                    kvval["rules"] = s["rules"]        # «Правила и поля» — тик применит к синтезу
                if s.get("rules_struct"):
                    kvval["rules_struct"] = s["rules_struct"]   # F2: скомпилированные фильтры — оркестратору
                if s.get("fields"):
                    kvval["fields"] = s["fields"]
                if _pc:
                    kvval["params_contract"] = _pc     # F2: тик передаст rules/fields процессному оркестратору
                kvval["rules_agent"] = qwen_agent()    # A6: скоуп правил процесса — тик читает их с платформы сам
                # A1: карту размещения тик получает УЖЕ РАЗВЁРНУТОЙ в реальные target'ы — он живёт на хостинге
                # и рефов моста не знает. Разворачиваем здесь, в KV кладём только то, что нужно исполнению.
                _pmap = _placement_resolve(s) if int(lb.get("placement_contract", 0) or 0) >= 1 else {}
                if _pmap:
                    kvval["placement"] = _pmap
                _kr = api("/api/kv/set", {"key": kvkey, "value": json.dumps(kvval, ensure_ascii=False),
                                          "description": "schedule " + sid})
                if not _kr or (isinstance(_kr, dict) and _kr.get("status") == "error"):   # #7: не отдавать success, если расписание не сохранилось
                    self._send({"status": "error", "message": "не удалось сохранить расписание (KV): " + str(_kr)[:120]}, 502)
                    return
                _sched_index_update(add=sid)      # внести sid в индекс активных расписаний
            s["updated_at"] = datetime.now(timezone.utc).isoformat()
            sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
            self._send({"status": "success", "schedule": s.get("schedule")})

        elif self.path == "/x/recipients":
            # Кабина «Настройка»: несколько получателей результата (добавить/убрать канал без Мастера).
            sid = str(body.get("session_id", ""))
            sp = SESS_DIR / (sid + ".json")
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            raw = body.get("recipients") or []
            clean, seen = [], set()
            for x in raw:
                k = str(x).strip().lower()
                if k and re.match(r"^[a-z0-9_]+$", k) and k not in seen:
                    seen.add(k); clean.append(k)
            _update_session(sid, lambda s: s.__setitem__("recipients", clean))
            # если процесс на расписании — патчим общий KV, чтобы тик сразу слал в новый список
            g = api("/api/kv/get", {"key": "sched:" + sid})
            if isinstance(g, dict) and g.get("value"):
                try:
                    cfg = json.loads(g["value"])
                    cfg["recipients"] = clean
                    cfg["deliver"] = clean[0] if clean else ""
                    api("/api/kv/set", {"key": "sched:" + sid, "value": json.dumps(cfg, ensure_ascii=False),
                                        "description": "schedule " + sid})
                except Exception:
                    pass
            self._send({"status": "success", "recipients": clean})

        elif self.path == "/x/automation_delete":
            # Удаление автоматизации из UI = АРХИВ (не насовсем): сессия+спутники → sessions_archive,
            # снимаем расписание, убираем из витрины. Восстановимо перемещением файлов обратно.
            import shutil as _sh
            sid = str(body.get("session_id", ""))
            if not SAFE_ID.match(sid or "") or not (SESS_DIR / (sid + ".json")).exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            arch = SESS_DIR.parent / "sessions_archive"
            arch.mkdir(exist_ok=True)
            moved = 0
            with _sess_lock(sid):
                for f in list(SESS_DIR.glob(sid + "*")):   # сессия + _blueprint/_spec/_files/…
                    try:
                        _sh.move(str(f), str(arch / f.name))
                        moved += 1
                    except Exception:
                        pass
            # снять расписание (KV + индекс активных), чтобы тик её больше не трогал
            try:
                api("/api/kv/remove", {"key": "sched:" + sid})
                _sched_index_update(remove=sid)
            except Exception:
                pass
            # C1-фикс (ревью CABINET_TZ): снять и ВХОДЯЩИЕ — иначе архивированная автоматизация
            # продолжает принимать и отвечать в Telegram (inbound:<sid> + индекс + hookmap жили дальше)
            try:
                _ig = api("/api/kv/get", {"key": "inbound:" + sid})
                _icv = _ig.get("value") if isinstance(_ig, dict) else None
                if _icv:
                    try:
                        _icj = json.loads(_icv)
                    except Exception:
                        _icj = {}
                    if _icj.get("route_token"):
                        api("/api/kv/remove", {"key": "hookmap:" + _icj["route_token"]})
                    api("/api/kv/remove", {"key": "inbound:" + sid})
                    _inbound_index_update(remove=sid)
            except Exception:
                pass
            # убрать карточку из витрины _mkt_automations (если публиковалась)
            try:
                _mc = api("/api/kv/get", {"key": "_mkt_automations", "global": True})
                _v = _mc.get("value") if isinstance(_mc, dict) else None
                if _v:
                    cat = json.loads(_v)
                    before = len(cat.get("items", []))
                    cat["items"] = [it for it in cat.get("items", []) if it.get("sessionId") != sid]
                    if len(cat["items"]) != before:
                        api("/api/kv/set", {"key": "_mkt_automations", "value": json.dumps(cat, ensure_ascii=False),
                                            "description": "mkt automations", "global": True})
            except Exception:
                pass
            _registry_refresh_async()   # состав возможностей изменился — реестр вслед
            self._send({"status": "success", "archived": moved})

        elif self.path == "/x/pause":
            # C1 (CABINET_TZ §3.2): Пауза/Возобновить — ничего не удаляет. Гасит АВТОзапуски:
            # sched:<sid>.active + inbound.active + hookmap.active. Ручной запуск остаётся доступен.
            # Resume: next_due вперёд (без catch-up-шторма); входящим ставится drain_once —
            # первый тик дренирует бэклог БЕЗ ответов (канон: не спамить по старым сообщениям).
            sid = str(body.get("session_id", ""))
            sp = SESS_DIR / (sid + ".json")
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            paused = bool(body.get("paused", True))
            out = {"sched": None, "inbound": None}
            # 1) расписание
            try:
                gv = api("/api/kv/get", {"key": "sched:" + sid})
                cv = gv.get("value") if isinstance(gv, dict) else None
                if cv:
                    cfg = json.loads(cv)
                    cfg["active"] = (not paused)
                    if not paused:
                        _iv = int(cfg.get("interval_min", 0) or 0)
                        # Resume: следующий запуск через интервал, не мгновенный шторм — даже при пустом interval сдвигаем на +1 мин (#20)
                        cfg["next_due_ts"] = (datetime.now(timezone.utc) + timedelta(minutes=max(1, _iv))).isoformat()
                    api("/api/kv/set", {"key": "sched:" + sid, "value": json.dumps(cfg, ensure_ascii=False),
                                        "description": "schedule " + sid})
                    out["sched"] = "paused" if paused else "active"
            except Exception as e:
                out["sched"] = "error: " + _scrub(str(e)[:80])
            # 2) входящие (+ webhook-карта — живёт отдельным ключом, гасим оба)
            try:
                ig = api("/api/kv/get", {"key": "inbound:" + sid})
                iv = ig.get("value") if isinstance(ig, dict) else None
                if iv:
                    ic = json.loads(iv)
                    ic["active"] = (not paused)
                    if not paused:
                        ic["drain_once"] = True   # тик: один дренаж бэклога без ответов
                    api("/api/kv/set", {"key": "inbound:" + sid, "value": json.dumps(ic, ensure_ascii=False),
                                        "description": "inbound " + sid})
                    if ic.get("route_token"):
                        try:
                            hg = api("/api/kv/get", {"key": "hookmap:" + ic["route_token"]})
                            hv = hg.get("value") if isinstance(hg, dict) else None
                            hc = json.loads(hv) if hv else {}
                            hc["active"] = (not paused)
                            api("/api/kv/set", {"key": "hookmap:" + ic["route_token"],
                                                "value": json.dumps(hc, ensure_ascii=False),
                                                "description": "hookmap"})
                        except Exception:
                            pass
                    out["inbound"] = "paused" if paused else "active"
            except Exception as e:
                out["inbound"] = "error: " + _scrub(str(e)[:80])
            # 3) сессия — источник статуса paused для UI (per-sid lock от гонок)
            def _mut(s):
                s["paused"] = paused
                s["paused_at" if paused else "resumed_at"] = datetime.now(timezone.utc).isoformat()
            _update_session(sid, _mut)
            # 4) витрина identity.status — best-effort кэш (истина в sched/inbound; потеря записи допустима)
            try:
                _mc = api("/api/kv/get", {"key": "_mkt_automations", "global": True})
                _v = _mc.get("value") if isinstance(_mc, dict) else None
                if _v:
                    cat = json.loads(_v)
                    hit = False
                    for it in cat.get("items", []):
                        if it.get("sessionId") == sid:
                            it["status"] = "paused" if paused else "active"
                            hit = True
                    if hit:
                        api("/api/kv/set", {"key": "_mkt_automations", "value": json.dumps(cat, ensure_ascii=False),
                                            "description": "mkt automations", "global": True})
            except Exception:
                pass
            self._send({"status": "success", "paused": paused, **out})

        elif self.path == "/x/flow_save":
            # Создание (инкр.3, B-lite): сохранить собранную Композитором ЗАДАЧУ (flow) как автоматизацию.
            # Сессия-обёртка с orchestrator=wz_flow_run → задача появляется в «Мои автоматизации» и в кабине.
            fid = str(body.get("flow_id", "")).strip()
            if not fid or not SAFE_ID.match(fid):
                self._send({"status": "error", "message": "нет корректного flow_id"}, 400)
                return
            name = str(body.get("name", "")).strip()[:80] or ("Задача " + fid)
            desc = str(body.get("description", "")).strip()[:200]
            comps = [str(x)[:60] for x in (body.get("components") or []) if str(x).strip()][:12]
            sid = "wz_" + datetime.now(timezone.utc).strftime("%Y%m%d") + "_fl" + re.sub(r"[^a-z0-9]", "", fid.lower())[-6:]
            sp = SESS_DIR / (sid + ".json")
            if sp.exists():   # идемпотентность: повторное сохранение того же flow в тот же день — не дублируем
                self._send({"status": "success", "session_id": sid, "existing": True})
                return
            now = datetime.now(timezone.utc).isoformat()
            # C5: в experts — РЕАЛЬНЫЕ имена блоков из flow (для публикации пака);
            # человеческие ярлыки чипов — отдельно в components_human (для плиток витрины)
            _real = []
            try:
                _fg = api("/api/kv/get", {"key": "flow:" + fid})
                _fv = _fg.get("value") if isinstance(_fg, dict) else None
                for _st in (json.loads(_fv) if _fv else {}).get("steps") or []:
                    _en = str(_st.get("expert") or "")
                    if _en and _en not in _real:
                        _real.append(_en)
            except Exception:
                _real = []
            s = {"session_id": sid, "client_name": name, "stage": "launched", "goal": desc,
                 "created_at": now, "updated_at": now,
                 "builds": [{"orchestrator": "wz_flow_run", "flow_id": fid, "experts": (_real or comps),
                             "components_human": comps,
                             "audit": {"verdict": "allow"}, "built_at": now, "composed": True}],
                 "log": [{"ts": now, "event": "saved composed flow " + fid + " as automation"}]}
            sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
            _registry_refresh_async()   # новая автоматизация — реестр вслед
            self._send({"status": "success", "session_id": sid})

        elif self.path == "/x/gen_panel":
            # §7bis ступень 3: Строитель генерит доменные ПОЛЯ из blueprint (Qwen). Схема кэшируется в сессии
            # (panel_manifest); значения владельца живут в s['fields'] (та же связка, что кормит синтез).
            sid = str(body.get("session_id", ""))
            sp = SESS_DIR / (sid + ".json")
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            bpp = SESS_DIR / (sid + "_blueprint.json")
            bp = {}
            if bpp.exists():
                try:
                    bp = json.loads(bpp.read_text(encoding="utf-8")).get("blueprint", {}) or {}
                except Exception:
                    bp = {}
            from wz_llm import gen_panel_manifest
            mani = gen_panel_manifest(str(bp.get("goal") or bp.get("summary") or ""), bp.get("stages") or [])
            if not mani:
                self._send({"status": "error", "message": "Qwen не вернул полей — попробуйте ещё раз"})
                return
            manifest = dict(mani, generated_at=datetime.now(timezone.utc).isoformat())
            _update_session(sid, lambda s: s.__setitem__("panel_manifest", manifest))
            self._send({"status": "success", "panel_manifest": manifest})
            return

        elif self.path == "/x/rules":
            # Кабинет «Правила и поля»: бизнес-правила словами + факты-поля per-automation (ступень 2 §7bis).
            # Хранятся в сессии; для flow-задач применяются к синтезу брифа (wz_flow_run OWNER RULES).
            sid = str(body.get("session_id", ""))
            sp = SESS_DIR / (sid + ".json")
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            rules = [str(x).strip()[:200] for x in (body.get("rules") or []) if str(x).strip()][:20]
            fields_in = body.get("fields") or {}
            fields = {}
            if isinstance(fields_in, dict):
                for k, v in list(fields_in.items())[:20]:
                    k2, v2 = str(k).strip()[:60], str(v).strip()[:200]
                    if k2 and v2:
                        fields[k2] = v2

            # F2: текстовое правило компилируется в машинные фильтры ОДИН раз при записи
            # (на прогонах оркестратор применяет структуры детерминированно).
            known = []
            try:
                s_now = json.loads(sp.read_text(encoding="utf-8"))
                for rr in reversed(s_now.get("runs") or []):
                    summ = rr.get("summary")
                    if isinstance(summ, str):
                        try:
                            summ = json.loads(summ)
                        except Exception:
                            summ = None
                    if isinstance(summ, dict):
                        known = [str(k)[3:] for k in summ.keys() if str(k).startswith("by_")]
                        if known:
                            break
            except Exception:
                known = []
            _cf = _compile_rule_filters(rules, known)
            structs, _cwhy = _cf["filters"], _cf["why"]


            # A6: пишем на платформу (источник истины), сессия остаётся КЭШЕМ для офлайна
            _psync = _proc_rules_push(sid, rules, qwen_agent()) if qwen_agent() else {"ok": False, "why": "нет агента"}

            def _mut(s):
                s["rules"], s["fields"], s["rules_struct"] = rules, fields, structs
                s["rules_synced"] = bool(_psync.get("ok"))   # честно: правила уехали на платформу или лежат только тут
            _update_session(sid, _mut)
            # процесс на расписании — тик должен применять свежие правила без пере-schedule
            g = api("/api/kv/get", {"key": "sched:" + sid})
            if isinstance(g, dict) and g.get("value"):
                try:
                    cfg = json.loads(g["value"])
                    cfg["rules"], cfg["fields"], cfg["rules_struct"] = rules, fields, structs
                    api("/api/kv/set", {"key": "sched:" + sid, "value": json.dumps(cfg, ensure_ascii=False),
                                        "description": "schedule " + sid})
                except Exception:
                    pass
            self._send({"status": "success", "rules": rules, "fields": fields, "rules_struct": structs,
                        "synced": bool(_psync.get("ok")),
                        "filters_note": _cwhy,   # почему правило не стало жёстким фильтром — владелец должен это видеть
                        "sync_note": "" if _psync.get("ok") else
                                     "правила сохранены на этом устройстве; на платформу не уехали (" +
                                     str(_psync.get("why") or "нет связи") + ")"})

        elif self.path == "/x/message_template":
            # Кабина «Шаблон сообщения»: текст доставки per-automation (плейсхолдеры {name}{count}{sum}{date}).
            sid = str(body.get("session_id", ""))
            sp = SESS_DIR / (sid + ".json")
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            tpl = str(body.get("template", ""))[:2000]
            s = _update_session(sid, lambda s: s.__setitem__("message_template", tpl))
            # если на расписании — патчим KV, чтобы тик слал по новому шаблону
            g = api("/api/kv/get", {"key": "sched:" + sid})
            if isinstance(g, dict) and g.get("value"):
                try:
                    cfg = json.loads(g["value"])
                    cfg["message_template"] = tpl
                    api("/api/kv/set", {"key": "sched:" + sid, "value": json.dumps(cfg, ensure_ascii=False),
                                        "description": "schedule " + sid})
                except Exception:
                    pass
            # превью с демо-значениями — чтобы UI показал, как выйдет
            preview = _render_msg(tpl, s.get("client_name") or sid, 128, 26000000)
            self._send({"status": "success", "template": tpl, "preview": preview})

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
            _vd = (lb.get("audit") or {}).get("verdict", "")
            if _vd == "escalate" and not body.get("confirmed"):   # одобрение≠запуск: не включать приём для заблокированного аудитом процесса
                self._send({"status": "error", "code": "escalate",
                            "message": "проверка безопасности требует вмешательства команды — приём входящих заблокирован"}, 403)
                return
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

        elif self.path == "/x/archive_restore":
            # Возврат из архива: переносим сессию и её спутников обратно. Расписание и входящие
            # НЕ включаем — процесс возвращается «на паузе», иначе он молча начнёт слать письма
            # клиенту через месяц после удаления.
            import shutil as _sh
            sid = str(body.get("session_id", ""))
            arch = SESS_DIR.parent / "sessions_archive"
            if not SAFE_ID.match(sid or "") or not (arch / (sid + ".json")).exists():
                self._send({"status": "error", "message": "в архиве нет такой автоматизации"}, 404)
                return
            if (SESS_DIR / (sid + ".json")).exists():
                self._send({"status": "error", "code": "conflict",
                            "message": "автоматизация с таким id уже есть в списке — возврат перезаписал бы её"}, 409)
                return
            moved = 0
            for f in list(arch.glob(sid + "*")):
                try:
                    _sh.move(str(f), str(SESS_DIR / f.name))
                    moved += 1
                except Exception:
                    pass
            if not moved:
                self._send({"status": "error", "message": "не удалось вернуть файлы"}, 500)
                return
            try:   # вернулась на паузе — владелец сам решит, когда включать
                _update_session(sid, lambda ss: ss.__setitem__("paused", True))
            except Exception:
                pass
            self._send({"status": "success", "session_id": sid, "files": moved,
                        "note": "вернулась на паузе: расписание и приём входящих не включены"})

        elif self.path == "/x/cli_wrap":
            # Ручной запуск того же пути (для уже установленных программ и для отладки).
            tool = re.sub(r"[^A-Za-z0-9_.-]", "", str(body.get("tool", "")))[:40]
            if not tool:
                self._send({"status": "error", "message": "не указана программа"}, 400)
                return
            r = _cli_wrap_flow(tool, str(body.get("purpose", ""))[:200])
            if not r.get("ok"):
                self._send({"status": "error", "stage": r.get("stage"), "expert": r.get("expert"),
                            "message": "обёртка не готова (" + str(r.get("stage")) + "): " + str(r.get("why"))[:200]}, 502)
            else:
                self._send({"status": "success", **r})

        elif self.path == "/x/report_spec_ask":
            # Правка отчёта СЛОВАМИ. Ничего не применяем: возвращаем предложение и что изменится —
            # тот же гейт человека, что у адаптеров источников.
            sid = str(body.get("sid", ""))
            sp = SESS_DIR / (sid + ".json")
            if not sid or not sp.exists():
                self._send({"status": "error", "message": "нет сессии"}, 400)
                return
            phrase = str(body.get("message", "")).strip()[:400]
            if not phrase:
                self._send({"status": "error", "message": "нет просьбы"}, 400)
                return
            s = json.loads(sp.read_text(encoding="utf-8"))
            # колонки берём из источника, а если его нет — из последнего прогона (сводка by_*)
            fields = list((s.get("source") or {}).get("schema") or [])
            if not fields:
                for rr in reversed(s.get("runs") or []):
                    _sm = rr.get("summary")
                    if isinstance(_sm, str):
                        try:
                            _sm = json.loads(_sm)
                        except Exception:
                            _sm = None
                    if isinstance(_sm, dict):
                        fields = [str(k)[3:] for k in _sm if str(k).startswith("by_")]
                        if fields:
                            break
            r = _report_spec_from_words(s.get("report_spec") or {}, phrase, fields)
            if not r.get("spec"):
                self._send({"status": "error", "message": r.get("why") or "не удалось спроектировать отчёт"}, 502)
                return
            self._send({"status": "success", "spec": r["spec"], "changes": r.get("changes") or [],
                        "rejected": r.get("rejected") or [], "fields": fields,
                        "supported": int(((s.get("builds") or [{}])[-1]).get("report_contract", 0) or 0) >= 1})

        elif self.path == "/x/report_spec_ask_preview":
            # Превью ДО применения: владелец должен увидеть ДОКУМЕНТ, а не список изменений словами.
            # Рисуем по числам ПОСЛЕДНЕГО ПРОГОНА — это его настоящий отчёт, а не выдуманный образец.
            sid = str(body.get("sid", ""))
            sp = SESS_DIR / (sid + ".json")
            if not sid or not sp.exists():
                self._send({"status": "error", "message": "нет сессии"}, 400)
                return
            spec = body.get("spec") if isinstance(body.get("spec"), dict) else {}
            s = json.loads(sp.read_text(encoding="utf-8"))
            summ, at = None, ""
            for rr in reversed(s.get("runs") or []):
                _sm = rr.get("summary")
                if isinstance(_sm, str):
                    try:
                        _sm = json.loads(_sm)
                    except Exception:
                        _sm = None
                if isinstance(_sm, dict) and any(str(k).startswith("by_") for k in _sm):
                    summ, at = _sm, rr.get("at", "")
                    break
            if not summ:
                self._send({"status": "error", "code": "no_runs",
                            "message": "процесс ещё ни разу не отработал — показывать в превью нечего. "
                                       "Запустите его, и я нарисую отчёт на ваших числах."}, 409)
                return
            want = [(v.get("group_by"), v.get("title")) for v in (spec.get("views") or []) if v.get("group_by")]
            if not want:
                want = [(str(k)[3:], "По полю «" + str(k)[3:] + "»") for k in summ if str(k).startswith("by_")][:3]
            sections, missing = [], []
            for g, title in want[:4]:
                items = summ.get("by_" + str(g))
                if isinstance(items, dict) and items:
                    sections.append({"title": title or g, "items": dict(list(items.items())[:12])})
                else:
                    missing.append(str(g))
            if not sections:
                self._send({"status": "error", "code": "no_sections",
                            "message": "в последнем прогоне нет разрезов по выбранным полям: " + ", ".join(missing)}, 409)
                return
            _spec = dict(spec)
            _spec["total"] = summ.get("total_count")
            # превью в ТОМ ЖЕ формате, который выбрал владелец: показывать PDF тому,
            # кто просил Word, — значит показывать не тот документ
            _fmt = str(spec.get("format") or "pdf").lower()
            _exp = {"docx": "fmt_report_docx", "pptx": "fmt_report_pptx"}.get(_fmt, "fmt_report_pdf")
            _ext = {"fmt_report_docx": ".docx", "fmt_report_pptx": ".pptx"}.get(_exp, ".pdf")
            _out = str(RUNS_DIR / ("preview_" + _ns(sid) + _ext))
            res = run_expert(_exp,
                             {"sections_json": json.dumps(sections, ensure_ascii=False),
                              "spec_json": json.dumps(_spec, ensure_ascii=False),
                              "output_path": _out}, wait=180, glob=True)
            if isinstance(res, str):
                try:
                    res = json.loads(res)
                except Exception:
                    try:
                        import ast as _ast
                        res = _ast.literal_eval(res)
                    except Exception:
                        res = {}
            if not (isinstance(res, dict) and res.get("status") == "success" and Path(_out).exists()):
                self._send({"status": "error",
                            "message": "превью не собралось: " + str((res or {}).get("message") or res)[:160]}, 502)
                return
            self._send({"status": "success", "path": _out, "bytes": Path(_out).stat().st_size,
                        "format": _fmt, "based_on": at, "missing": missing,
                        "sections": [x["title"] for x in sections]})

        elif self.path == "/x/report_spec_set":
            sid = str(body.get("sid", ""))
            sp = SESS_DIR / (sid + ".json")
            if not sid or not sp.exists():
                self._send({"status": "error", "message": "нет сессии"}, 400)
                return
            spec = body.get("spec")
            if not isinstance(spec, dict):
                self._send({"status": "error", "message": "спека отчёта должна быть объектом"}, 400)
                return
            _update_session(sid, lambda ss: ss.__setitem__("report_spec", spec))
            s = json.loads(sp.read_text(encoding="utf-8"))
            _sup = int(((s.get("builds") or [{}])[-1]).get("report_contract", 0) or 0) >= 1
            self._send({"status": "success", "spec": spec, "supported": _sup,
                        "note": "" if _sup else "процесс собран до появления оформителя — применится после пересборки"})

        elif self.path == "/x/adapter_confirm":
            # AC-05 ГЕЙТ: адаптер применяется, только когда человек его подтвердил. Автоприменение
            # запрещено намеренно — иначе система сама решит, что «Сумма» это «Скидка», и никто не заметит.
            sid = str(body.get("sid", ""))
            sp = SESS_DIR / (sid + ".json")
            if not sid or not sp.exists():
                self._send({"status": "error", "message": "нет сессии"}, 400)
                return
            s = json.loads(sp.read_text(encoding="utf-8"))
            raw = body.get("map") or {}
            fmap = {str(k).strip(): str(v).strip() for k, v in raw.items()
                    if str(k).strip() and str(v).strip()}
            if not fmap:
                self._send({"status": "error", "message": "пустой маппинг — нечего подтверждать"}, 400)
                return
            res = _adapter_save(sid, fmap, body.get("columns") or [], str(body.get("note") or "подтверждено владельцем"))
            if not res.get("ok"):
                self._send({"status": "error", "message": "адаптер не сохранился: " + res.get("why", "")}, 502)
                return
            # новая выгрузка стала нормой: обновляем эталон схемы, иначе дрифт сработает снова
            if body.get("columns"):
                def _mut(ss):
                    if isinstance(ss.get("source"), dict):
                        ss["source"]["schema"] = sorted(str(c) for c in body["columns"])
                _update_session(sid, _mut)
            _sup = int(((s.get("builds") or [{}])[-1]).get("adapter_contract", 0) or 0) >= 1
            self._send({"status": "success", "v": res["v"], "map": fmap, "supported": _sup,
                        "note": "" if _sup else "процесс собран до появления адаптеров — применится после пересборки"})

        elif self.path == "/x/adapter_rollback":
            sid = str(body.get("sid", ""))
            if not sid or not (SESS_DIR / (sid + ".json")).exists():
                self._send({"status": "error", "message": "нет сессии"}, 400)
                return
            d = _adapter_load(sid)
            want = int(body.get("v") or 0)
            if not any(int(v.get("v") or 0) == want for v in (d.get("versions") or [])):
                self._send({"status": "error", "message": "такой версии адаптера нет"}, 400)
                return
            d["active"] = want
            api("/api/kv/set", {"key": _adapter_key(sid), "value": json.dumps(d, ensure_ascii=False),
                                "description": "adapter " + sid})
            self._send({"status": "success", "active": want})

        elif self.path == "/x/placement_set":
            # A1: человек утверждает карту. Пишем РЕФЫ (device id в сессию не попадает).
            sid = str(body.get("sid", ""))
            sp = SESS_DIR / (sid + ".json")
            if not sid or not sp.exists():
                self._send({"status": "error", "message": "нет сессии"}, 400)
                return
            s = json.loads(sp.read_text(encoding="utf-8"))
            known = {d.get("ref") for d in _targets_live()}
            raw = body.get("map") or {}
            stages = set(_placement_stages(s))
            m = {str(k): str(v) for k, v in raw.items()
                 if str(k) in stages and str(v) in known}     # чужие стадии/несуществующие устройства не принимаем
            bad = [str(k) for k in raw if str(k) in stages and str(raw[k]) not in known]
            if bad:
                self._send({"status": "error", "message": "устройство не найдено для стадий: " + ", ".join(bad[:4])}, 400)
                return
            s["placement"] = {"map": m, "confirmed": bool(body.get("confirm")),
                              "set_at": datetime.now(timezone.utc).isoformat()}
            s["updated_at"] = s["placement"]["set_at"]
            sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
            self._send({"status": "success", "map": m, "confirmed": s["placement"]["confirmed"],
                        "preflight": _placement_preflight(s)})

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
            _bp_agent = {}
            try:
                _bp_agent = json.loads(bpp.read_text(encoding="utf-8")).get("blueprint", {})
                pname = _bp_agent.get("process_name", pname)
            except Exception:
                pass
            src = (builds[-1] or {}).get("source_file") or ""
            _agent_params = {"source_file": src}
            if int((builds[-1] if builds else {}).get("agentic_contract", 0) or 0) >= 1:
                # Чат production-agent обязан соблюдать тот же контракт, что и кабинет.
                # Иначе агент вызывал доказанного эксперта без output_dir и получал ложную ошибку.
                _agent_params.update({"output_dir": "/tmp/extella_" + sid + "_agent",
                                      "run_id": "agent_chat"})
            _agent_params_text = json.dumps(_agent_params, ensure_ascii=False)
            instr = ("Ты — рабочий агент процесса «" + pname + "» на платформе Extella.\n\n"
                     "# Как запускать процесс\nВесь процесс — один оркестратор. Чтобы сформировать результат, вызови "
                     "run_expert: expert_name=\"" + orch + "\", global=true, params=" + _agent_params_text + ". "
                     "Он проходит всю цепочку и возвращает summary + отчёт .md/.xlsx.\n"
                     "# Результат\nЦитируй ФАКТИЧЕСКИЕ числа из summary (total_count, total_sum, разбивки by_). Не выдумывай. "
                     "Ошибку оркестратора покажи как есть.\n"
                     "# Дисциплина\nОдин инструмент за ход; цитируй фактический результат; без псевдо-вызовов.\n"
                     "# Границы\nТолько чтение; наружу ничего не пишешь/не отправляешь. Заморожен (F2): не меняешь эксперты/правила. "
                     "Изменения процесса — через Строителя (сессия " + sid + ").\n# Стиль\nДеловой русский, кратко, с цифрами.")
            upd = api("/api/agent/update", {"agent_id": agent_id, "instructions": instr})
            if isinstance(upd, dict) and upd.get("id") == agent_id:
                _stage_concepts = [str(x.get("title") or "").strip() for x in (_bp_agent.get("stages") or [])
                                   if isinstance(x, dict) and str(x.get("title") or "").strip()]
                _concepts = [
                    "Назначение: " + str(_bp_agent.get("goal") or _bp_agent.get("summary") or pname)[:330],
                    "Этапы процесса: " + " → ".join(_stage_concepts)[:320],
                ]
                _src_names = [str(x) for x in ((builds[-1] if builds else {}).get("source_files") or []) if str(x)]
                if _src_names:
                    _concepts.append("Входной пакет: " + ", ".join(_src_names)[:330])
                # Builder отдаёт сюда только память, подтверждённую полной приёмкой всего процесса.
                # Candidate/rejected и память красной стройки в build record отсутствуют по контракту.
                _verified = [x for x in ((builds[-1] if builds else {}).get("verified_memory") or [])
                             if isinstance(x, dict) and x.get("status") == "verified"]
                _learned_concepts = [str(x.get("text") or "").strip() for x in _verified
                                     if x.get("kind") == "concept" and str(x.get("text") or "").strip()]
                _learned_rules = [str(x.get("text") or "").strip() for x in _verified
                                  if x.get("kind") == "rule" and str(x.get("text") or "").strip()]
                _concepts.extend(_learned_concepts)
                # Упаковка = не только instructions. Предметные знания идут в concepts, ограничения
                # владельца — в rules того же агента; выученные правила имеют отдельный тег, поэтому
                # никакая синхронизация не может автоматически отменить прямое правило владельца.
                _cpack = _proc_concepts_push(sid, _concepts, agent_id)
                _rpack = _proc_rules_push(sid, s.get("rules") or [], agent_id)
                _lrpack = _proc_learned_rules_push(sid, _learned_rules, agent_id)
                s["production_agent"] = {"agent_id": agent_id, "name": upd.get("name"),
                                         "orchestrator": orch, "deployed_at": datetime.now(timezone.utc).isoformat(),
                                         "package": {"experts": list((builds[-1] if builds else {}).get("experts") or []),
                                                     "concepts": _cpack, "rules": _rpack,
                                                     "learned_rules": _lrpack,
                                                     "verified_memory_count": len(_verified)}}
                s["updated_at"] = datetime.now(timezone.utc).isoformat()
                sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
                self._send({"status": "success", "agent_id": agent_id, "name": upd.get("name"),
                            "orchestrator": orch, "package": s["production_agent"]["package"]})
            else:
                self._send({"status": "error", "message": "не удалось перепрошить агента: " + str(upd)[:200]})

        elif self.path == "/x/publish":
            # публикация автоматизации в магазин: wz_publish_pack генерит пак (репо+карточка) и кладёт в _mkt_automations
            sid = str(body.get("session_id", ""))
            sp = SESS_DIR / (sid + ".json")
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            s = json.loads(sp.read_text(encoding="utf-8"))
            builds = s.get("builds") or []
            lb = builds[-1] if builds else {}
            orch = lb.get("orchestrator")
            if isinstance(orch, dict):
                orch = orch.get("expert_name")
            if not orch:  # старые билды хранят оркестратор в build_plan.plan.orchestrator (часто dict)
                try:
                    _pl = json.loads((SESS_DIR / (sid + "_build_plan.json")).read_text(encoding="utf-8"))
                    _o = (_pl.get("plan") or {}).get("orchestrator") or _pl.get("orchestrator")
                    orch = _o.get("expert_name") if isinstance(_o, dict) else _o
                except Exception:
                    pass
            if not orch:
                self._send({"status": "error", "message": "у процесса нет оркестратора — сначала соберите его"}, 400)
                return
            # C5: композиция публикуется ЧЕСТНО — код блоков по РЕАЛЬНЫМ именам из flow:<id>.steps
            # (в builds.experts у старых flow-сессий лежали ярлыки чипов → пак был бы пустышкой)
            _fid_pub = str(lb.get("flow_id") or "")
            _flow_pub = None
            if _fid_pub:
                try:
                    _fg = api("/api/kv/get", {"key": "flow:" + _fid_pub})
                    _fv = _fg.get("value") if isinstance(_fg, dict) else None
                    _flow_pub = json.loads(_fv) if _fv else None
                except Exception:
                    _flow_pub = None
                if not _flow_pub or not _flow_pub.get("steps"):
                    self._send({"status": "error", "message": "flow композиции не найден — пересоберите её (Доводка)"}, 400)
                    return
                experts = []
                for _st in _flow_pub.get("steps") or []:
                    _en = str(_st.get("expert") or "")
                    if _en and _en not in experts:
                        experts.append(_en)
                if orch not in experts:
                    experts.append(orch)   # wz_flow_run — оркестратор композиций, тоже глобальный эксперт
            else:
                experts = list(lb.get("experts") or [])
                if orch not in experts:
                    experts.append(orch)
            pname = s.get("client_name", "process"); pdesc = ""
            try:
                bp = json.loads((SESS_DIR / (sid + "_blueprint.json")).read_text(encoding="utf-8")).get("blueprint", {})
                pname = bp.get("process_name", pname)
                pdesc = bp.get("summary") or bp.get("goal") or ""
            except Exception:
                pass
            import re as _re
            slug = _re.sub(r"[^a-z0-9]+", "-", str(pname).lower()).strip("-") or sid
            pack_id = ("extella-" + slug)[:48]
            agent_id = ((s.get("production_agent") or {}).get("agent_id")) or CONFIG.get("agent_id", "")
            owner = str(body.get("github_owner", "") or CONFIG.get("github_owner", "") or "AnvarBakiyev")
            # publish бежит ЛОКАЛЬНО в процессе моста (Mac Анвара: тут gh/git/файлы), НЕ на VPS HOST_TARGET
            # Личность агента для витрины (Qwen по блупринту) — индивидуальные emoji/цвет/слоган/умения.
            # Мягкий фолбэк: при пустом ответе publish возьмёт свою эвристику.
            _idv = {}
            try:
                _idv = _gen_identity(pname, pdesc, experts) or {}
            except Exception:
                _idv = {}
            try:
                _pub = _load_expert_fn("wz_publish_pack")
                r = _pub(pack_id=pack_id, name=pname, description=pdesc, experts=",".join(experts),
                         orchestrator=orch, agent_id=agent_id, github_owner=owner,
                         push=bool(body.get("push", True)), api_token=CONFIG.get("auth_token", ""),
                         session_id=sid,
                         emoji=_idv.get("emoji", ""), accent=_idv.get("accent", ""),
                         category=_idv.get("category", ""), capabilities=_idv.get("capabilities", ""),
                         tagline=_idv.get("tagline", ""))
            except Exception as _e:
                self._send({"status": "error", "message": "publish exec: " + str(_e)[:200]})
                return
            if isinstance(r, dict) and r.get("status") == "success":
                # C5: для композиции — дописать в пак flow/flow.json + install-блок восстановления
                # (install.py пака: регистрирует блоки → восстанавливает flow:<id> в KV → карточку →
                # доустанавливает локальные модели). Это и есть «композиция → продукт».
                if _fid_pub and _flow_pub:
                    try:
                        import subprocess as _sp2, os as _os
                        _root = Path.home() / "extella_wizard" / "published" / pack_id
                        (_root / "flow").mkdir(exist_ok=True)
                        _fj = dict(_flow_pub); _fj["_flow_id"] = _fid_pub
                        (_root / "flow" / "flow.json").write_text(json.dumps(_fj, ensure_ascii=False, indent=2), encoding="utf-8")
                        _restore = (
                            '\n# --- composed flow: восстановить план композиции и карточку витрины ---\n'
                            '_fp=os.path.join(HERE,"flow","flow.json")\n'
                            'if os.path.exists(_fp):\n'
                            ' _fj=json.load(open(_fp,encoding="utf-8")); _fid=_fj.pop("_flow_id","")\n'
                            ' if _fid:\n'
                            '  api("/api/kv/set",{"key":"flow:"+_fid,"value":json.dumps(_fj,ensure_ascii=False),"description":"composer flow"})\n'
                            # витрина — ТОЛЬКО под agent_extella_default (урок теней _mkt_): свой HDR для каталога
                            '  _H2=dict(HDR); _H2["X-Agent-Id"]="agent_extella_default"\n'
                            '  def api2(p,b):\n'
                            '   _rq=urllib.request.Request(BASE+p,data=json.dumps(b).encode(),headers=_H2,method="POST")\n'
                            '   return json.loads(urllib.request.urlopen(_rq,timeout=90).read().decode())\n'
                            '  try:\n'
                            '   _cur=api2("/api/kv/get",{"key":"_mkt_automations","global":True}).get("value")\n'
                            '   _cat=json.loads(_cur) if _cur else {"items":[]}\n'
                            '  except Exception:\n'
                            '   _cat={"items":[]}\n'
                            '  _card={"id":"flow-"+_fid,"name":_fj.get("name",""),"type":"process","description":str(_fj.get("task",""))[:200],\n'
                            '         "orchestrator":"wz_flow_run","runParams":{"flow_id":_fid},"composed":True,\n'
                            '         "components":[x.get("expert") for x in _fj.get("steps",[])]}\n'
                            '  _cat["items"]=[i for i in _cat.get("items",[]) if i.get("id")!=_card["id"]]\n'
                            '  _cat["items"].insert(0,_card)\n'
                            '  api2("/api/kv/set",{"key":"_mkt_automations","value":json.dumps(_cat,ensure_ascii=False),"description":"automations catalog","global":True})\n'
                            '  print("OK composed flow: flow:"+_fid)\n'
                            '  for _m in (_fj.get("installed") or []):\n'
                            '   _mn=(_m.get("model") or _m.get("name")) if isinstance(_m,dict) else None\n'
                            '   if _mn:\n'
                            '    try:\n'
                            '     api("/api/expert/run",{"expert_name":"cap_localmodel_install","global":True,"params":{"model":_mn}})\n'
                            '     print("OK model:",_mn)\n'
                            '    except Exception as _e:\n'
                            '     print("WARN model",_mn,str(_e)[:50])\n')
                        _ip = _root / "install.py"
                        if _ip.exists() and "_flow_id" not in _ip.read_text(encoding="utf-8"):
                            _ip.write_text(_ip.read_text(encoding="utf-8") + _restore, encoding="utf-8")
                        _sp2.run(["git", "add", "-A"], cwd=str(_root), capture_output=True)
                        _sp2.run(["git", "commit", "-q", "-m", "composed flow: план + восстановление в install"],
                                 cwd=str(_root), capture_output=True)
                        if bool(body.get("push", True)):
                            _sp2.run(["git", "push", "-q"], cwd=str(_root), capture_output=True,
                                     env={**_os.environ, "PATH": "/opt/homebrew/bin:/usr/local/bin:" + _os.environ.get("PATH", "")})
                    except Exception:
                        pass   # пак уже создан; flow-довесок best-effort — не валим публикацию
                s["published"] = {"pack_id": pack_id, "repo_url": r.get("repo_url"),
                                  "at": datetime.now(timezone.utc).isoformat()}
                s["updated_at"] = datetime.now(timezone.utc).isoformat()
                sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
                _registry_refresh_async()   # публикация — реестр вслед
                self._send({"status": "success", "pack_id": pack_id, "repo_url": r.get("repo_url"),
                            "card_registered": r.get("card_registered"), "experts": r.get("experts_written"),
                            "composed_flow": bool(_fid_pub)})
            else:
                self._send({"status": "error", "message": "публикация не удалась: " + str(r)[:200]})

        elif self.path == "/x/classify_intent":
            # Роутинг по намерению (инкр.5-full): Qwen решает task vs бизнес-процесс. Дополняет эвристику UI.
            task = str(body.get("task", "")).strip()
            if not task:
                self._send({"status": "error", "message": "нет task"}, 400)
                return
            ag = qwen_agent()
            if not ag:
                self._send({"status": "error", "message": "нет Qwen-агента"}, 503)
                return
            prompt = ("Классифицируй запрос пользователя на автоматизацию. Верни ТОЛЬКО JSON без пояснений:\n"
                      '{"kind":"process"|"task","confidence":<0..1>,"reason":"<коротко по-русски>"}\n\n'
                      "process = БИЗНЕС-ПРОЦЕСС компании: живёт постоянно, обычно регулярность + данные из систем "
                      "(1С/CRM/Битрикс/база) + получатели/роли (директор, отдел); нужен продовый агент 24/7.\n"
                      "task = РАЗОВАЯ/повторяемая задача из готовых блоков: разбор файлов, дайджест, поиск, отчёт.\n\n"
                      "Запрос: " + task[:600])
            try:
                res = api("/api/agent/run", {"agent_id": ag, "input": prompt, "run_timeout": 60,
                                             "store": False, "temperature": 0}, timeout=70)
                text = ""
                for it in (res or {}).get("output", []):
                    if isinstance(it, dict) and it.get("type") == "message":
                        for c in it.get("content", []):
                            if isinstance(c, dict) and c.get("type") == "output_text":
                                text += c.get("text", "")
                text = text or (res or {}).get("output_text", "")
                m = re.search(r"\{.*\}", text, re.S)
                v = json.loads(m.group(0)) if m else {}
                kind = "process" if str(v.get("kind", "")).lower().startswith("proc") else "task"
                conf = float(v.get("confidence", 0) or 0)
                self._send({"status": "success", "kind": kind, "confidence": max(0.0, min(1.0, conf)),
                            "reason": str(v.get("reason", ""))[:200]})
            except Exception as e:
                self._send({"status": "error", "message": _scrub(str(e)[:150])})
            return

        elif self.path == "/x/gen_questions":
            # P2 (Анвар/партнёр): АДАПТИВНОЕ интервью — вопросы генерируются Qwen ПОД задачу,
            # а не фиксированный список. Ядро blueprint гарантируется домержем (данные/частота/успех).
            # Формат ответов не меняется ({qid:{question,answer}}) — blueprint читает пары как текст.
            sid = str(body.get("session_id", ""))
            task = str(body.get("task", "")).strip()[:600]
            sp = SESS_DIR / (sid + ".json")
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            if not task:
                self._send({"status": "error", "message": "опишите задачу словами"}, 400)
                return
            ag = qwen_agent()
            lang = _task_lang(task)
            if lang == "ru":
                prompt = ("Ты — интервьюер Extella: готовишь стройку ИИ-процесса под задачу владельца. "
                          "Верни ТОЛЬКО JSON-массив из 6–8 вопросов конспекта, КОНКРЕТНЫХ ИМЕННО ДЛЯ ЭТОЙ задачи "
                          "(не общих). Формат: [{\"id\":\"<snake_case>\",\"q\":\"<вопрос>\",\"hint\":\"<подсказка-пример>\"}]\n"
                          "Обязательно покрой: откуда данные и в каком формате; как часто нужен результат; "
                          "что будет успехом. Остальные вопросы — специфичные для домена задачи "
                          "(термины, объёмы, исключения, кто участвует, на что опирается).\n\nЗадача: " + task)
            else:
                prompt = ("You are the Extella interviewer preparing to build an AI process for the owner's task. "
                          "Return ONLY a JSON array of 6-8 brief questions SPECIFIC TO THIS task (not generic). "
                          "Format: [{\"id\":\"<snake_case>\",\"q\":\"<question>\",\"hint\":\"<hint with example>\"}]\n"
                          "Must cover: where the data comes from and its format; how often the result is needed; "
                          "what success looks like. The rest — domain-specific questions.\n\nTask: " + task)
            try:
                res = api("/api/agent/run", {"agent_id": ag, "input": prompt, "run_timeout": 90,
                                             "store": False, "temperature": 0}, timeout=100)
                text = ""
                for it in (res or {}).get("output", []):
                    if isinstance(it, dict) and it.get("type") == "message":
                        for c in it.get("content", []):
                            if isinstance(c, dict) and c.get("type") == "output_text":
                                text += c.get("text", "")
                text = text or (res or {}).get("output_text", "")
                m = re.search(r"\[.*\]", text, re.S)
                qs = json.loads(m.group(0)) if m else []
            except Exception as e:
                self._send({"status": "error", "message": "Qwen не собрал вопросы: " + _scrub(str(e)[:120])})
                return
            clean = []
            seen_ids = set()
            for q in qs:
                if not isinstance(q, dict):
                    continue
                qid = re.sub(r"[^a-z0-9_]", "", str(q.get("id", "")).lower())[:32]
                qq = str(q.get("q", "")).strip()[:160]
                if not qid or not qq or qid in seen_ids:
                    continue
                seen_ids.add(qid)
                clean.append({"id": qid, "q": qq, "hint": str(q.get("hint", ""))[:160]})
            if len(clean) < 4:
                self._send({"status": "error", "message": "вопросы не собрались — оставлен стандартный конспект"}, 422)
                return
            # ядро blueprint: гарантируем данные/частоту/успех (если Qwen не покрыл — домерж)
            _core = ([("data_sources", "Откуда данные?", "Системы-источники и формат выгрузки (Excel, CSV…)"),
                      ("frequency", "Как часто нужен результат?", "Разово / ежедневно / еженедельно / ежемесячно"),
                      ("success", "Что будет успехом?", "Метрики и критерии доверия"),
                      ("knowledge_base", "Правовая / нормативная база", "Закон, кодекс, регламент, договор — или «не применимо»")]
                     if lang == "ru" else
                     [("data_sources", "Where does the data come from?", "Source systems and export format (Excel, CSV…)"),
                      ("frequency", "How often is the result needed?", "One-off / daily / weekly / monthly"),
                      ("success", "What does success look like?", "Metrics and trust criteria"),
                      ("knowledge_base", "Legal / regulatory basis", "Law, code, regulation, contract — or 'not applicable'")])
            _blob = " ".join((c["id"] + " " + c["q"]).lower() for c in clean)
            for cid, cq, ch in _core:
                _kw = {"data_sources": ["данн", "источник", "data", "source", "формат", "выгруз"],
                       "frequency": ["част", "расписан", "регуляр", "frequen", "часто", "schedule", "how often"],
                       "success": ["успех", "критери", "метрик", "success", "metric"],
                       "knowledge_base": ["правов", "нормат", "кодекс", "закон", "регламент", "legal", "regulat", "law", "compliance"]}[cid]
                if not any(k in _blob for k in _kw):
                    clean.append({"id": cid, "q": cq, "hint": ch})
            clean = clean[:10]
            def _save_questionnaire(s):
                s["questionnaire"] = clean
                s["questionnaire_task"] = task
                s.pop("data_check", None)
            _update_session(sid, _save_questionnaire)
            self._send({"status": "success", "questions": clean, "count": len(clean)})

        elif self.path == "/x/cab_chat":
            # C4 (CABINET_TZ §5.1): чат автоматизации — классификатор глубины правки.
            # НЕ применяет ничего сам: возвращает {depth, op, args, summary}; UI показывает карточку
            # подтверждения и жмёт СУЩЕСТВУЮЩИЕ эндпоинты (гейт доверия: превью → подтверждение).
            sid = str(body.get("session_id", ""))
            message = str(body.get("message", "")).strip()[:600]
            sp = SESS_DIR / (sid + ".json")
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            if not message:
                self._send({"status": "error", "message": "нет message"}, 400)
                return
            s = json.loads(sp.read_text(encoding="utf-8"))
            ag = qwen_agent()
            if not ag:
                self._send({"status": "error", "message": "нет Qwen-агента"}, 503)
                return
            _rc = ", ".join(_recipients(s)) or "не заданы"
            _pd = (s.get("schedule") or {}).get("period") or "запуск вручную"
            _kind = "композиция из готовых блоков" if (s.get("builds") or [{}])[-1].get("flow_id") else "построенный бизнес-процесс"
            prompt = ("Ты — маршрутизатор правок автоматизации Extella. Верни ТОЛЬКО JSON без пояснений:\n"
                      '{"depth":"light"|"medium"|"strong","op":"recipients"|"schedule"|"template"|"pause"|"resume"|"brain"|"other",'
                      '"args":{...},"summary":"<что будет сделано, одной фразой, тем же языком что фраза владельца>"}\n\n'
                      "light = настройка обвязки, БЕЗ изменения логики:\n"
                      "- получатели результата → op=recipients, args.recipients = ПОЛНЫЙ НОВЫЙ список из [telegram,email,slack,whatsapp,sms] (учти текущих!)\n"
                      "- расписание → op=schedule, args.period ∈ [Каждый час,Ежедневно,Еженедельно,Ежемесячно] или \"\" чтобы снять\n"
                      "- текст сообщения получателям → op=template, args.template=<новый текст, плейсхолдеры {name}{count}{sum}{date}>\n"
                      "- остановить/включить → op=pause | op=resume\n"
                      "- ВИД ОТЧЁТА (заголовок, разрезы/группировки, что за главное число, цвет, подпись) "
                      "→ op=report, args.message=<фраза владельца целиком>\n"
                      "МОЗГ АГЕНТА (знания и правила САМОГО АГЕНТА, не про шаги процесса) → op=brain:\n"
                      "- ФАКТ/справка о бизнесе, «запомни/учти/у нас … = …», регламент, значение, определение "
                      "→ args.brain_op=add_concept, args.text=<что запомнить, полной фразой>\n"
                      "- ПОВЕДЕНИЕ агента, «всегда/никогда/обращайся/подписывай/не делай без подтверждения» "
                      "→ args.brain_op=add_rule, args.text=<правило поведения, полной фразой>\n"
                      "  (op=brain — это про то, что агент ЗНАЕТ или КАК себя ведёт, а не про колонки/фильтры результата)\n"
                      "medium = новое поле/правило/фильтр/колонка процесса, влияющие на расчёт или содержание результата (op=other).\n"
                      "strong = изменить сами шаги/этапы/источники процесса (op=other).\n"
                      "При сомнении между light и medium — выбирай medium (честность важнее удобства).\n\n"
                      "Автоматизация: «" + str(s.get("client_name") or sid)[:60] + "» (" + _kind + "). "
                      "Текущие получатели: " + _rc + ". Расписание: " + _pd + ".\n"
                      "Фраза владельца: " + message)
            try:
                res = api("/api/agent/run", {"agent_id": ag, "input": prompt, "run_timeout": 60,
                                             "store": False, "temperature": 0}, timeout=70)
                text = ""
                for it in (res or {}).get("output", []):
                    if isinstance(it, dict) and it.get("type") == "message":
                        for c in it.get("content", []):
                            if isinstance(c, dict) and c.get("type") == "output_text":
                                text += c.get("text", "")
                text = text or (res or {}).get("output_text", "")
                m = re.search(r"\{.*\}", text, re.S)
                v = json.loads(m.group(0)) if m else {}
                depth = str(v.get("depth", "medium")).lower()
                if depth not in ("light", "medium", "strong"):
                    depth = "medium"
                op = str(v.get("op", "other")).lower()
                if op not in ("recipients", "schedule", "template", "pause", "resume", "brain", "other"):
                    op = "other"
                args = v.get("args") if isinstance(v.get("args"), dict) else {}
                # санитария light-аргументов (никакого свободного исполнения)
                if op == "recipients":
                    args = {"recipients": [x for x in (args.get("recipients") or [])
                                           if str(x).strip().lower() in ("telegram", "email", "slack", "whatsapp", "sms")]}
                elif op == "schedule":
                    _p = str(args.get("period", ""))
                    args = {"period": _p if _p in ("Каждый час", "Ежедневно", "Еженедельно", "Ежемесячно", "") else "Ежедневно"}
                elif op == "template":
                    args = {"template": str(args.get("template", ""))[:2000]}
                elif op == "brain":
                    _bop = str(args.get("brain_op", "")).lower()
                    _txt = str(args.get("text", "")).strip()[:2000]
                    if _bop not in ("add_concept", "add_rule") or not _txt:
                        op, args = "other", {}   # неполная правка мозга → в общий разбор
                    else:
                        args = {"brain_op": _bop, "text": _txt}
                        depth = "light"   # правка мозга применяется сразу по подтверждению, как обвязка
                else:
                    args = {}
                self._send({"status": "success", "depth": depth, "op": op, "args": args,
                            "summary": str(v.get("summary", ""))[:200]})
            except Exception as e:
                self._send({"status": "error", "message": _scrub(str(e)[:150])})
            return

        elif self.path == "/x/compose":
            # Композитор: задача словами -> wz_auto_compose (server-side, надёжно) -> план+карточка
            task = str(body.get("task", "")).strip()
            if not task:
                self._send({"status": "error", "message": "опиши задачу"}, 400)
                return
            ctask = task + _compose_directive(_task_lang(task))
            res = None
            for _att in range(2):   # устойчивость к флапу бэкенда Qwen (ретрай)
                res = run_expert("wz_auto_compose", {"task": ctask, "agent_id": qwen_agent(), "api_token": CONFIG.get("auth_token", "")},
                                 wait=200, glob=True)
                if isinstance(res, dict) and res.get("status") == "success":
                    break
                time.sleep(2)
            if isinstance(res, dict):
                res["missing_human"] = [_human_missing(m) for m in (res.get("missing") or [])]
            self._send(res if isinstance(res, dict) else {"status": "error", "message": str(res)[:200]})

        elif self.path == "/x/compose_chat":
            # Полный чат по сборке: пользователь доводит flow словами → пересборка с накопленным контекстом.
            # C2 (кабинет композиции): с flow_id доводится СОХРАНЁННАЯ композиция — task берётся из
            # flow:<id> (там уже накоплены прошлые уточнения), пересборка ПЕРЕЗАПИСЫВАЕТ тот же flow_id
            # (стабильные карточка/расписание/сессия — без мусора новых id на каждое сообщение).
            task = str(body.get("task", "")).strip()
            flow_id = str(body.get("flow_id", "")).strip()
            refinements = [str(x).strip() for x in (body.get("refinements") or []) if str(x).strip()][:20]
            message = str(body.get("message", "")).strip()
            if flow_id and not SAFE_ID.match(flow_id):
                self._send({"status": "error", "message": "bad flow_id"}, 400)
                return
            if flow_id and not task:
                try:
                    _fg = api("/api/kv/get", {"key": "flow:" + flow_id})
                    _fv = _fg.get("value") if isinstance(_fg, dict) else None
                    task = str((json.loads(_fv) if _fv else {}).get("task") or "").strip()
                except Exception:
                    task = ""
            if not task or not message:
                self._send({"status": "error", "message": "нет task/message"}, 400)
                return
            allref = refinements + [message]
            # язык = язык ПОСЛЕДНЕЙ реплики пользователя (решение Анвара) — не исходной задачи:
            # русская доводка англоязычной композиции возвращает русскую карточку, и наоборот
            lang = _task_lang(message)
            if lang == "ru":
                full_task = task + "\n\nУчти уточнения пользователя (в порядке важности, последнее — главное):\n- " + "\n- ".join(allref) + _compose_directive("ru")
            else:
                full_task = task + "\n\nApply the user's refinements (most important last):\n- " + "\n- ".join(allref) + _compose_directive("en")
            _cparams = {"task": full_task, "agent_id": qwen_agent(), "api_token": CONFIG.get("auth_token", "")}
            if flow_id:
                _cparams["reuse_flow_id"] = flow_id
            res = None
            for _att in range(2):   # флап бэкенда Qwen отпускает за секунды → короткий ретрай
                res = run_expert("wz_auto_compose", _cparams, wait=200, glob=True)
                if isinstance(res, dict) and res.get("status") == "success":
                    break
                time.sleep(2)
            if not isinstance(res, dict) or res.get("status") != "success":
                self._send({"status": "error",
                            "reply": "Не удалось пересобрать — попробуйте сформулировать иначе.",
                            "message": _scrub((res or {}).get("message", str(res)[:180]) if isinstance(res, dict) else str(res)[:180])})
                return
            card = res.get("card") or {}
            miss_h = [_human_missing(m) for m in (res.get("missing") or [])]
            comps = card.get("components") or [s.get("expert") for s in (res.get("steps") or [])]
            reply = "Пересобрал: «" + str(card.get("name") or task)[:60] + "» — " + str(len(comps)) + " блок(ов)."
            reply += (" Осталось: " + "; ".join(miss_h[:3]) + ".") if miss_h else " Можно запускать или сохранить."
            res["missing_human"] = miss_h
            res["reply"] = reply
            # C2: доводка сохранённой композиции — освежить сессию кабинета (имя/описание/состав)
            _sidu = str(body.get("session_id", ""))
            if flow_id and _sidu and SAFE_ID.match(_sidu) and (SESS_DIR / (_sidu + ".json")).exists():
                def _mu(s):
                    if card.get("name"):
                        s["client_name"] = str(card["name"])[:80]
                    if card.get("description"):
                        s["goal"] = str(card["description"])[:200]
                    lb = (s.get("builds") or [{}])[-1]
                    lb["experts"] = comps[:12]
                    lb["revised_at"] = datetime.now(timezone.utc).isoformat()
                try:
                    _update_session(_sidu, _mu)
                except Exception:
                    pass
            self._send(res)

        elif self.path == "/x/digest":
            # C3: последний дайджест прогона для виджета «Последний результат» (пишут тик и /x/run_process)
            sid = str(body.get("session_id", "")).strip()
            if not sid or not SAFE_ID.match(sid):
                self._send({"status": "error", "message": "нет session_id"}, 400)
                return
            try:
                g = api("/api/kv/get", {"key": "digest:" + sid})
                v = g.get("value") if isinstance(g, dict) else None
                d = json.loads(v) if v else None
            except Exception:
                d = None
            if not d or not d.get("digest"):
                self._send({"status": "empty"})
                return
            self._send({"status": "success", "at": d.get("at"), "digest": d.get("digest")})

        elif self.path == "/x/first_win":
            # Онбординг «первая победа за 5 минут»: живой демо-прогон на синтетике → реальный отчёт.
            # Никогда не тупик: если платформенный эксперт не ответил — отдаём тот же отчёт локально.
            _fwres = None
            try:
                _fwres = run_expert("wz_first_win_demo", {"example": str(body.get("example", "ads"))}, wait=45, glob=True)
            except Exception:
                _fwres = None
            _fwdg = _run_digest(_fwres) if isinstance(_fwres, dict) else ""
            if not _fwdg:
                _fwdg = ("## Ежедневная отчётность и контроль рекламных бюджетов\n\n"
                         "_Демо на синтетических данных_\n\n"
                         "**Итого:** 12 позиций по 6 клиентам · расход **673 000 ₸**. Остаток бюджета критический у 2 клиентов.\n\n"
                         "### Остаток бюджета по клиентам\n| Клиент | Остаток | Статус |\n|---|---|---|\n"
                         "| Альфа-Трейд | 8% | 🔴 пополнить |\n| Зета-Авто | 12% | 🔴 пополнить |\n"
                         "| Бета-Логистика | 21% | 🟡 следить |\n| Дельта-Фуд | 47% | 🟢 ок |\n\n"
                         "### На подтверждение\n- **Бюджет почти исчерпан** — Альфа-Трейд, Зета-Авто: нужно пополнение\n\n"
                         "_Это то, что процесс собирает и присылает каждый день сам. Соберите такой под свои данные._")
            self._send({"status": "success", "digest": _fwdg, "synthetic": True})

        elif self.path == "/x/flow":
            # C2: состав сохранённой композиции для вкладки «Состав» кабинета (steps/installed/missing)
            fid = str(body.get("flow_id", "")).strip()
            if not fid or not SAFE_ID.match(fid):
                self._send({"status": "error", "message": "нет flow_id"}, 400)
                return
            try:
                g = api("/api/kv/get", {"key": "flow:" + fid})
                v = g.get("value") if isinstance(g, dict) else None
                f = json.loads(v) if v else None
            except Exception:
                f = None
            if not f:
                self._send({"status": "error", "message": "flow не найден"}, 404)
                return
            self._send({"status": "success", "flow_id": fid, "name": f.get("name"), "task": f.get("task"),
                        "steps": f.get("steps") or [], "installed": f.get("installed") or [],
                        "missing": f.get("missing") or [],
                        "missing_human": [x for x in ([_human_missing(m) for m in (f.get("missing") or [])]) if x],
                        "composed_at": f.get("composed_at")})

        elif self.path == "/x/run_flow":
            # Прогон собранной композитором автоматизации -> полный бриф в приложение
            fid = str(body.get("flow_id", "")).strip()
            if not fid:
                self._send({"status": "error", "message": "нет flow_id"}, 400)
                return
            res = run_expert("wz_flow_run", {"flow_id": fid, "agent_id": qwen_agent(), "api_token": CONFIG.get("auth_token", "")},
                             wait=260, glob=True)
            if isinstance(res, dict) and res.get("status") == "success":
                self._send({"status": "success", "digest": res.get("digest_md") or res.get("digest") or "",
                            "run_status": res.get("run_status", "success"), "degraded": bool(res.get("degraded")),
                            "warnings": res.get("warnings") or [], "delivery_error": _scrub(res.get("delivery_error") or ""),
                            "stages": res.get("stages"), "delivered": res.get("delivered")})
            else:
                self._send({"status": "error", "message": _scrub((res or {}).get("message", str(res)[:200]) if isinstance(res, dict) else str(res)[:200])})

        elif self.path == "/x/cap_search":
            # Композитор ищет способность ВНЕ каталога по живым источникам (HF/npm-MCP/GitHub/Smithery)
            q = str(body.get("query", "")).strip()
            if not q:
                self._send({"status": "error", "message": "нет query"}, 400)
                return
            res = run_expert("wz_capability_search",
                             {"query": q, "kinds": str(body.get("kinds", "")), "limit": int(body.get("limit", 3) or 3)},
                             wait=90, glob=True)
            if isinstance(res, dict):
                self._send({"status": res.get("status", "success"), "candidates": res.get("candidates") or [],
                            "count": res.get("count", 0), "message": _scrub(res.get("message", ""))})
            else:
                self._send({"status": "error", "message": _scrub(str(res)[:200])})

        elif self.path == "/x/cap_install":
            # Установка ПОСЛЕ клик-подтверждения + регистрация в «Мои»
            kind = str(body.get("kind", "")).strip(); ref = str(body.get("install_ref", "")).strip()
            method = str(body.get("method", "")).strip()
            if not kind or not ref:
                self._send({"status": "error", "message": "нужны kind и install_ref"}, 400)
                return
            # CLI: витрина «Инструменты» (тулбар) шлёт kind=cli БЕЗ method → wz_capability_install
            # брал бы «manual» = только ссылка, бинарь НЕ ставится, обёртка падает. Ставим бинарь
            # РЕЗОЛВЕРОМ инструмента, если он есть: он знает СОСТАВНЫЕ рецепты (ocr=tesseract+ocrmypdf+
            # языки; pandoc=pip; calibre/libreoffice=cask), которых голый «brew install» не повторит.
            # Нет резолвера — честный brew. Иначе клик «поставить» ничего не устанавливал (20.07).
            if kind.lower() == "cli" and not method:
                _t = re.sub(r"[^A-Za-z0-9_.-]", "", str(ref).split("/")[-1])[:40]
                _resolver = "cap_" + re.sub(r"[^a-z0-9_]", "_", _t.lower()) + "_resolver"
                if _expert_exists(_resolver):
                    try:
                        rr = run_expert(_resolver, {"confirm_install": "yes"}, wait=320, glob=True)
                        _rs = rr if isinstance(rr, dict) else {}
                        if not isinstance(rr, dict):
                            try:
                                _rs = json.loads(rr) if isinstance(rr, str) else {}
                            except Exception:
                                _rs = {}
                    except Exception:
                        _rs = {}
                    # резолвер сам поставил бинарь → оборачиваем и в каталог, минуя wz_capability_install
                    _wrap = _cli_wrap_flow(_t, str(body.get("desc", "")) or str(body.get("title", "")))
                    _registry_refresh_async()
                    self._send({"status": "success", "install_status": _rs.get("status", "installed"),
                                "wrapper": _wrap, "message": _scrub(str(_rs.get("message", ""))[:200])})
                    return
                method = "brew"   # нет резолвера — ставим напрямую через Homebrew
            res = run_expert("wz_capability_install",
                             {"kind": kind, "install_ref": ref, "method": method,
                              "pkg_type": str(body.get("pkg_type", "")), "title": str(body.get("title", "")),
                              "desc": str(body.get("desc", "")), "url": str(body.get("url", "")),
                              "source": str(body.get("source", ""))}, wait=320, glob=True)
            if isinstance(res, dict):
                _wrap = None
                if res.get("status") in ("success", "installing"):
                    _registry_refresh_async()   # установка способности — реестр вслед
                    # ЗАМЫКАЕМ ЦЕПОЧКУ: поставили программу — сразу делаем эксперта-обёртку,
                    # проверяем живым запуском и вносим в каталог Композитора. Без этого шага
                    # программа стоит на устройстве, но Композитор ею пользоваться не может
                    # (разрыв, найденный с Анваром на figlet).
                    if str(kind).lower() == "cli":
                        _tool = re.sub(r"[^A-Za-z0-9_.-]", "", str(ref).split("/")[-1])[:40]
                        _wrap = _cli_wrap_flow(_tool, str(body.get("desc", "")) or str(body.get("title", "")))
                    elif str(kind).lower() == "mcp":
                        # Тот же разрыв, что был у программ: сервер подключён, mcp_call умеет его
                        # звать — а Композитор не может, потому что в каталоге блоков ничего нет.
                        _sid = ""
                        try:
                            _sid = str((((res.get("installed") or {}).get("detail")) or {}).get("server_id") or "")
                        except Exception:
                            _sid = ""
                        if _sid:
                            _wrap = _mcp_wrap_flow(_sid)
                        else:
                            _wrap = {"ok": False, "why": "сервер подключён, но не вернул своего идентификатора — "
                                                         "блоки не собраны, инструменты доступны только агенту"}
                self._send({"status": res.get("status", "success"), "install_status": res.get("install_status"),
                            "registered_in_my": res.get("registered_in_my"), "installed": res.get("installed"),
                            "wrapper": _wrap,   # что стало с обёрткой: собрана / не прошла проверку / не делалась
                            "message": _scrub(res.get("message", ""))})
            else:
                self._send({"status": "error", "message": _scrub(str(res)[:200])})

        elif self.path == "/x/mcp_wrap":
            # Обернуть инструменты MCP-сервера в блоки Композитора. Нужен отдельно от cap_install,
            # потому что ВСТРОЕННЫЕ серверы (fetch/time/git/filesystem) ничего не устанавливают —
            # они доступны сразу, и без этого пути их нечем внести в каталог.
            srv = re.sub(r"[^A-Za-z0-9_.-]", "", str(body.get("server", "")).strip())[:40]
            if not srv:
                self._send({"status": "error", "message": "нужен server"}, 400)
                return
            only = body.get("tools") or None
            if only is not None and not isinstance(only, list):
                self._send({"status": "error", "message": "tools — список имён инструментов"}, 400)
                return
            r = _mcp_wrap_flow(srv, only)
            if r.get("ok"):
                _registry_refresh_async()   # состав возможностей изменился — реестр вслед
            self._send({"status": "success" if r.get("ok") else "error", "server": srv,
                        "wrapped": r.get("wrapped") or [], "skipped": r.get("skipped") or [],
                        "message": r.get("why", "")})

        elif self.path == "/x/device_status":
            # Единая картина: что установлено × что работает и не упадёт. Одно состояние на
            # способность. Для раздела «Программы» тулбара и Студии — один источник правды.
            d = _device_status()
            self._send({"status": "success", "rows": d["rows"], "summary": d["summary"],
                        "blocks": d["blocks"]})

        elif self.path == "/x/blocks_health":
            # Битые блоки: остались в каталоге, а способность под ними исчезла. Показываем, не
            # удаляя — как ghostscript, у которого сняли brew, а блок сжатия PDF остался жить.
            h = _catalog_broken()
            self._send({"status": "success", "broken": h["broken"], "checked": h["checked"]})

        elif self.path == "/x/block_remove":
            bid = str(body.get("id", "")).strip()
            if not bid:
                self._send({"status": "error", "message": "нужен id блока"}, 400)
                return
            r = _block_remove(bid)
            if r.get("ok"):
                _registry_refresh_async()
            self._send({"status": "success" if r.get("ok") else "error",
                        "id": bid, "blocks": r.get("blocks"), "message": r.get("why", "")})

        elif self.path == "/x/installed":
            # «Что у меня установлено и что из этого уже работает в автоматизациях».
            inv = _installed_inventory()
            self._send({"status": "success", "items": inv["items"], "unused": inv["unused"],
                        "blocks": inv["blocks"], "stale": inv.get("stale") or []})

        elif self.path == "/x/make_block":
            # Один вход для всех типов: человеку незачем знать, что программа, MCP и приложение
            # оборачиваются по-разному. Внутри — три уже проверенные цепочки.
            kind = str(body.get("kind", "")).strip().lower()
            ref = str(body.get("ref", "")).strip()[:80]
            purpose = str(body.get("purpose", ""))[:200]
            if kind not in ("app", "mcp", "cli") or not ref:
                self._send({"status": "error", "message": "нужны kind (app|mcp|cli) и ref"}, 400)
                return
            r = _make_one_block(kind, ref, purpose)
            if r.get("ok"):
                _registry_refresh_async()
            self._send({"status": "success" if r.get("ok") else "error", "kind": kind, "ref": ref,
                        "made": r["made"], "message": r["why"]})

        elif self.path == "/x/app_wrap":
            # Приложение → блок Композитора. Пятый и последний тип способностей: программы, модели,
            # MCP уже замкнуты, скиллы намеренно живут правилом в мозге агента.
            aid = str(body.get("app_id", "")).strip()[:80]
            if not aid:
                self._send({"status": "error", "message": "нужен app_id"}, 400)
                return
            r = _app_wrap_flow(aid, str(body.get("purpose", ""))[:200])
            if r.get("ok"):
                _registry_refresh_async()   # состав возможностей изменился — реестр вслед
            self._send({"status": "success" if r.get("ok") else "error", "app": aid,
                        "expert": r.get("expert", ""), "stage": r.get("stage", ""),
                        "sample": r.get("sample", ""), "message": r.get("why", "")})

        elif self.path == "/x/mcp_tools":
            # Что сервер вообще умеет — до всякой обёртки. Отвечает честно: недоступен, пуст
            # или инструменты без схемы (такие обернуть нельзя, угадывать поля мы отказались).
            srv = re.sub(r"[^A-Za-z0-9_.-]", "", str(body.get("server", "")).strip())[:40]
            if not srv:
                self._send({"status": "error", "message": "нужен server"}, 400)
                return
            t = _mcp_tools(srv)
            self._send({"status": "success" if t["tools"] else "error",
                        "server": srv, "tools": t["tools"],
                        "no_schema": t.get("no_schema") or [], "message": t.get("why", "")})

        elif self.path == "/x/cspl_create":
            # CSPL Studio: генеративный builder — «создай язык словами». БЕЗОПАСНОСТЬ ПО ПОСТРОЕНИЮ:
            # Qwen проектирует НЕ код, а декларативную СПЕЦИФИКАЦИЮ (данные) поверх проверенного ядра
            # report_dsl: обязательные колонки, дефолтная программа, замороженные поля. Исполняемый
            # код не генерится вовсе → детерминизм и вет ядра наследуются. Регистрация — только
            # через fixtures-гейт (позитив+негатив, сгенерённые Qwen'ом, прогнанные вживую).
            desc = str(body.get("description", "")).strip()[:600]
            if len(desc) < 15:
                self._send({"status": "error", "message": "опишите язык подробнее (что за отчёт, какие колонки обязательны, какой фильтр по умолчанию)"}, 400)
                return
            ag = qwen_agent()
            if not ag:
                self._send({"status": "error", "message": "нет Qwen-агента"}, 503)
                return
            prompt = ("Спроектируй НОВЫЙ предметный язык отчётов для бизнес-пользователя на базе ядра report_dsl. "
                      "Верни ТОЛЬКО JSON без пояснений:\n"
                      '{"handler_id":"cspl_<латинский_слаг_до_24_символов>","title":"<короткое название языка>",'
                      '"description":"<одна фраза: для чего язык>",'
                      '"spec":{"required_columns":["<колонки данных, без которых программа невалидна>"],'
                      '"default_program":{"report":"<дефолтное название отчёта>","columns":["..."],'
                      '"filter":{"field":"...","op":">|>=|<|<=|==|contains","value":0} или null,'
                      '"group_by":"..." или null,"totals":["..."],"out":"both"},'
                      '"locked_fields":["<поля программы, которые пользователю менять нельзя, напр. filter>"]},'
                      '"fixtures":[{"name":"positive","program":{<программа>},"records":[<5-6 записей-объектов '
                      "с РУССКИМИ колонками из default_program>],\"expect\":\"success\"},"
                      '{"name":"negative","program":{<программа БЕЗ одной обязательной колонки в columns>},'
                      '"records":[],"expect":"invalid"}]}\n'
                      "Названия колонок — языком владельца. Данные вымышленные.\n\nОписание языка: " + desc)
            try:
                res = api("/api/agent/run", {"agent_id": ag, "input": prompt, "run_timeout": 120,
                                             "store": False, "temperature": 0.2}, timeout=140)
                text = ""
                for it in (res or {}).get("output", []):
                    if isinstance(it, dict) and it.get("type") == "message":
                        for c in it.get("content", []):
                            if isinstance(c, dict) and c.get("type") == "output_text":
                                text += c.get("text", "")
                text = text or (res or {}).get("output_text", "")
                m = re.search(r"\{.*\}", text, re.S)
                design = json.loads(m.group(0)) if m else {}
            except Exception as e:
                self._send({"status": "error", "message": "Qwen не спроектировал язык: " + _scrub(str(e)[:120])})
                return
            hid = str(design.get("handler_id", "")).strip()
            spec = design.get("spec") or {}
            fixtures = design.get("fixtures") or []
            if not re.match(r"^cspl_[a-z0-9_]{2,24}$", hid) or hid in ("cspl_report_dsl", "cspl_pipeline_dsl"):
                self._send({"status": "error", "message": "плохой handler_id от проектировщика: " + _scrub(hid[:40])})
                return
            if not (isinstance(spec.get("default_program"), dict) and isinstance(spec.get("required_columns"), list)):
                self._send({"status": "error", "message": "спецификация неполна (default_program/required_columns)"})
                return
            try:
                _rg = json.loads((api("/api/kv/get", {"key": "cspl:registry"}) or {}).get("value") or "{}")
            except Exception:
                _rg = {}
            if hid in (_rg.get("handlers") or {}):
                self._send({"status": "error", "message": "язык с таким id уже есть: " + hid}, 409)
                return
            # fixtures-гейт: позитив и негатив гоняются ВЖИВУЮ через derived-компиляцию
            fx_results = []
            fx_fail = 0
            for fx in fixtures[:4]:
                out = _cspl_compile_derived(spec, fx.get("program") or {}, fx.get("records") or [],
                                            action="compile" if fx.get("expect") == "success" else "validate")
                ok = out.get("status") == ("success" if fx.get("expect") == "success" else "invalid")
                fx_results.append({"name": str(fx.get("name", "fx"))[:40], "ok": ok, "got": out.get("status")})
                if not ok:
                    fx_fail += 1
            if fx_fail or len(fx_results) < 2:
                self._send({"status": "error", "message": "fixtures не прошли — язык НЕ зарегистрирован (канон вета)",
                            "fixtures": fx_results})
                return
            handlers = _rg.setdefault("handlers", {})
            handlers[hid] = {"handler_id": hid, "version": "1.0.0", "kind": "derived",
                             "base": "cspl_report_dsl",
                             "title": str(design.get("title", hid))[:80],
                             "description": str(design.get("description", ""))[:200],
                             "compiles_to": ["md", "xlsx"], "spec": spec,
                             "fixtures": fx_results,
                             "program_example": (fixtures[0].get("program") if fixtures else spec.get("default_program"))}
            api("/api/kv/set", {"key": "cspl:registry", "value": json.dumps({"v": 0, "handlers": handlers}, ensure_ascii=False),
                                "description": "CSPL Studio registry v0"})
            _registry_refresh_async()
            self._send({"status": "success", "handler_id": hid, "title": handlers[hid]["title"],
                        "fixtures": fx_results, "spec": spec})

        elif self.path == "/x/cspl_compile":
            # CSPL Studio S1: компиляция программы на зарегистрированном языке (cspl:registry).
            # Handler — детерминированный код без LLM; некорректная программа отклоняется до исполнения.
            handler = str(body.get("handler_id", "")).strip()
            if not re.match(r"^cspl_[a-z0-9_]+$", handler or ""):
                self._send({"status": "error", "message": "handler_id должен быть cspl_*"}, 400)
                return
            if handler == "cspl_pipeline_dsl":
                # S2: pipeline_dsl — компилятор живёт В МОСТУ (_make_orchestrator = рендер _ORCH_TEMPLATE),
                # артефакт компиляции — исполняемый эксперт <ns>_run_pipeline на платформе.
                prog = body.get("program") if isinstance(body.get("program"), dict) else {}
                perrs = []
                ns = str(prog.get("pipeline", "")).strip()
                if not re.match(r"^[a-z][a-z0-9]{1,15}$", ns or ""):
                    perrs.append({"field": "pipeline", "message": "имя конвейера: латиница/цифры, 2-16 символов (станет префиксом эксперта)"})
                stages = prog.get("stages")
                if not (isinstance(stages, list) and stages and all(isinstance(x, str) and re.match(r"^[A-Za-z0-9_]+$", x) for x in stages)):
                    perrs.append({"field": "stages", "message": "обязателен непустой список имён экспертов-стадий"})
                kp = prog.get("kp_stages") or []
                if not (isinstance(kp, list) and all(isinstance(x, str) for x in kp)):
                    perrs.append({"field": "kp_stages", "message": "kp_stages должен быть списком имён"})
                elif isinstance(stages, list) and any(x not in stages for x in kp):
                    perrs.append({"field": "kp_stages", "message": "каждая kp-стадия обязана входить в stages"})
                # глубокая валидация: стадии должны СУЩЕСТВОВАТЬ на платформе (это ценность языка)
                if not perrs:
                    for st in stages:
                        try:
                            g = api("/api/expert/get", {"name": st, "global": True}, 30)
                            code_len = len((g or {}).get("expert_code") or ((g or {}).get("expert") or {}).get("code") or "")
                        except Exception:
                            code_len = -1
                        if code_len <= 0:
                            perrs.append({"field": "stages", "message": "эксперт не найден на платформе: " + st})
                if perrs:
                    self._send({"status": "invalid", "handler": "cspl_pipeline_dsl", "errors": perrs})
                    return
                if str(body.get("action", "compile")) == "validate":
                    self._send({"status": "valid", "handler": "cspl_pipeline_dsl"})
                    return
                from wz_build import _make_orchestrator as _mk
                import hashlib as _hl
                nm, sv, code = _mk(ns, stages, str(prog.get("work_dir") or ("/tmp/" + ns + "_run")),
                                   session_id=str(prog.get("session_id") or ""),
                                   kp_stages=kp, want_code=True)
                if not nm:
                    self._send({"status": "error", "message": "компиляция не сохранилась: " + _scrub(str(sv)[:150])})
                    return
                _registry_refresh_async()   # новый исполняемый артефакт — реестр вслед
                self._send({"status": "success", "handler": "cspl_pipeline_dsl", "compiled": "expert",
                            "orchestrator": nm, "code_sha256": _hl.sha256(code.encode("utf-8")).hexdigest(),
                            "params_contract": 1})
                return
            try:
                _rg = json.loads((api("/api/kv/get", {"key": "cspl:registry"}) or {}).get("value") or "{}")
                if handler not in (_rg.get("handlers") or {}):
                    self._send({"status": "error", "message": "язык не зарегистрирован в cspl:registry — сначала регистрация с fixtures"}, 404)
                    return
            except Exception:
                self._send({"status": "error", "message": "cspl:registry недоступен"}, 503)
                return
            _h = (_rg.get("handlers") or {}).get(handler) or {}
            if _h.get("kind") == "derived":
                # derived-язык: спецификация поверх ядра, кода нет — компилирует мост через ядро
                _prog = body.get("program") if isinstance(body.get("program"), dict) else {}
                _recs = body.get("records") if isinstance(body.get("records"), list) else []
                self._send(_cspl_compile_derived(_h.get("spec") or {}, _prog, _recs,
                                                 action=str(body.get("action", "compile"))))
                return
            prog = body.get("program")
            recs = body.get("records")
            params = {"action": str(body.get("action", "compile")),
                      "program_json": json.dumps(prog, ensure_ascii=False) if isinstance(prog, (dict, list)) else str(prog or ""),
                      "output_dir": "/tmp/cspl_" + handler}
            if isinstance(recs, list):
                params["records_json"] = json.dumps(recs, ensure_ascii=False)
            elif body.get("input_path"):
                params["input_path"] = str(body.get("input_path"))[:300]
            res = run_expert(handler, params, wait=180, glob=True)
            if isinstance(res, str):
                try:
                    res = json.loads(res)
                except Exception:
                    try:
                        import ast as _ast
                        res = _ast.literal_eval(res)   # платформа отдаёт result питоновским repr
                    except Exception:
                        res = {"status": "error", "message": str(res)[:200]}
            self._send(res if isinstance(res, dict) else {"status": "error", "message": str(res)[:200]})

        elif self.path == "/x/target_requirements":
            # Мультитаргет T2: требования процесса к устройству (кабинет → Настройка).
            # {apps: ["1C", ...], local_only: bool, device: "<slug>"|""} — preflight проверяет их
            # перед каждым прогоном/расписанием (паттерн WZ-07: честный отказ до, не падение после).
            sid = str(body.get("session_id", ""))
            sp = SESS_DIR / (sid + ".json")
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            req = {}
            apps = [str(a).strip()[:40] for a in (body.get("apps") or []) if str(a).strip()][:10]
            if apps:
                req["apps"] = apps
            if body.get("local_only"):
                req["local_only"] = True
            dev = str(body.get("device", "")).strip()[:40]
            if dev and re.match(r"^[a-z0-9-]+$", dev):
                req["device"] = dev

            def _mutr(s2):
                if req:
                    s2["target_requirements"] = req
                else:
                    s2.pop("target_requirements", None)
            _update_session(sid, _mutr)
            self._send({"status": "success", "target_requirements": req or None})

        elif self.path == "/x/listener_cleanup":
            # Закрыть ТОЛЬКО сирот листенера (PPID=1 и команда bin/extella-l*). Живой штатный
            # листенер (дитя uv-лаунчера Extella.app) не трогается по построению.
            import subprocess as _sp
            import signal as _sig
            killed = []
            try:
                out = _sp.run(["ps", "-axo", "pid,ppid,command"], capture_output=True, text=True, timeout=10).stdout
                for ln in out.splitlines()[1:]:
                    if "bin/extella-l" not in ln or "grep" in ln:
                        continue
                    parts = ln.split(None, 2)
                    if len(parts) >= 3 and parts[1] == "1":
                        try:
                            os.kill(int(parts[0]), _sig.SIGTERM)
                            killed.append(int(parts[0]))
                        except Exception:
                            pass
                self._send({"status": "success", "killed": killed,
                            "message": ("закрыто сирот: %d" % len(killed)) if killed else "сирот нет — всё чисто"})
            except Exception as e:
                self._send({"status": "error", "message": _scrub(str(e)[:120])}, 500)

        elif self.path == "/x/launchagent_action":
            # Управление LaunchAgent'ом устройства: stop | start | enable | disable.
            # Жёсткий гейт: label обязан существовать файлом в ~/Library/LaunchAgents (только
            # пользовательские агенты; системные /Library и демоны недосягаемы по построению).
            import subprocess as _sp
            label = str(body.get("label", "")).strip()
            action = str(body.get("action", "")).strip()
            if not re.match(r"^[\w\.\-]{3,80}$", label) or action not in ("stop", "start", "enable", "disable"):
                self._send({"status": "error", "message": "нужны label (имя из ~/Library/LaunchAgents) и action: stop|start|enable|disable"}, 400)
                return
            plist = Path.home() / "Library" / "LaunchAgents" / (label + ".plist")
            if not plist.exists():
                self._send({"status": "error", "message": "агент не найден в ~/Library/LaunchAgents: " + label}, 404)
                return
            uid = str(os.getuid())
            steps = {"stop":    [["launchctl", "bootout", "gui/" + uid + "/" + label]],
                     "disable": [["launchctl", "bootout", "gui/" + uid + "/" + label],
                                 ["launchctl", "disable", "gui/" + uid + "/" + label]],
                     "enable":  [["launchctl", "enable", "gui/" + uid + "/" + label],
                                 ["launchctl", "bootstrap", "gui/" + uid, str(plist)]],
                     "start":   [["launchctl", "enable", "gui/" + uid + "/" + label],
                                 ["launchctl", "bootstrap", "gui/" + uid, str(plist)]]}[action]
            log = []
            for cmd in steps:
                try:
                    r = _sp.run(cmd, capture_output=True, text=True, timeout=20)
                    log.append({"cmd": " ".join(cmd[1:3]), "rc": r.returncode,
                                "err": (r.stderr or "").strip()[:120] or None})
                except Exception as e:
                    log.append({"cmd": " ".join(cmd[1:3]), "rc": -1, "err": str(e)[:120]})
            # идемпотентность: bootout уже выгруженного (rc=3) и bootstrap уже загруженного — не ошибка
            ok = all(s["rc"] in (0, 3, 17) or "already" in str(s.get("err", "")).lower() for s in log)
            self._send({"status": "success" if ok else "error", "label": label, "action": action, "log": log})

        elif self.path == "/x/team_invite":
            # Команда v0 (решение Анвара: «пригласить друга — добавить Гульжан; роли позже»).
            # Приглашение = ИМЕННОЙ токен этого аккаунта (/api/token/generate, префикс invite:).
            # Полный токен отдаётся ОДИН раз в UI для передачи лично; в логи не пишется.
            # Отзыв — /x/team_revoke в один клик. Роли/права — платформенный вопрос (PLATFORM_ASKS A4).
            name = re.sub(r"[^\w \-А-Яа-яЁё]", "", str(body.get("name", "")).strip())[:40]
            if len(name) < 2:
                self._send({"status": "error", "message": "укажите имя приглашённого (2-40 символов)"}, 400)
                return
            r = api("/api/token/generate", {"name": "invite: " + name})
            tok_val = (r or {}).get("token") or ((r or {}).get("result") or {}).get("token") if isinstance(r, dict) else None
            if not tok_val:
                self._send({"status": "error", "message": "платформа не выдала токен: " + _scrub(str(r)[:120])})
                return
            self._send({"status": "success", "name": name, "token": tok_val,
                        "note": "токен показывается один раз — передайте лично"})

        elif self.path == "/x/team_revoke":
            name = str(body.get("name", "")).strip()
            created_at = str(body.get("created_at", "")).strip()
            if not name:
                self._send({"status": "error", "message": "нет имени участника"}, 400)
                return
            r = api("/api/token/list", {})
            items = r.get("tokens") or r.get("results") or []
            hit = next((t for t in items if isinstance(t, dict) and t.get("name") == name
                        and (not created_at or str(t.get("created_at", "")).startswith(created_at))
                        and not t.get("revoked")), None)
            if not hit:
                self._send({"status": "error", "message": "активный токен участника не найден"}, 404)
                return
            rr = api("/api/token/revoke", {"token": hit.get("token")})
            self._send({"status": "success", "revoked": name,
                        "platform": (rr or {}).get("status", "ok")})

        elif self.path == "/x/registry_rebuild":
            # ручной пересбор реестра (кнопка/отладка): фоном, ответ сразу
            _registry_refresh_async()
            self._send({"status": "success", "started": True,
                        "message": "пересбор реестра запущен — /x/registry отдаст свежий через ~минуту"})

        elif self.path == "/x/my_library":
            # Вкладка «Мои»: composer-установки (KV) + реально стоящее на устройстве (Ollama-модели, MCP-аллоулист)
            items = []
            try:
                cur = api("/api/kv/get", {"key": "_mkt_installed"})
                val = cur.get("value") if isinstance(cur, dict) else None
                if val:
                    items = (json.loads(val) or {}).get("items", [])
            except Exception:
                items = []
            seen = {(it.get("kind"), it.get("id")) for it in items}
            # локальные модели с устройства (мост крутится на Маке)
            try:
                import urllib.request as _u
                with _u.urlopen("http://localhost:11434/api/tags", timeout=5) as r:
                    for m in json.loads(r.read().decode()).get("models", []):
                        mid = m.get("name", "")
                        if mid and ("model", mid) not in seen:
                            seen.add(("model", mid))
                            items.append({"kind": "model", "id": mid, "title": mid, "status": "installed",
                                          "source": "ollama", "method": "ollama",
                                          "how": "Локальная модель на устройстве — приватно, через Ollama.",
                                          "detail": {"size_gb": round((m.get("size", 0) or 0) / 1e9, 1)}})
            except Exception:
                pass
            # подключённые MCP-серверы (аллоулист)
            try:
                al = Path.home() / ".extella_mcp" / "allowlist.json"
                if al.exists():
                    data = json.loads(al.read_text(encoding="utf-8"))
                    servers = data.get("servers", data) if isinstance(data, dict) else {}
                    for sid, meta in (servers.items() if isinstance(servers, dict) else []):
                        if ("mcp", sid) not in seen:
                            seen.add(("mcp", sid))
                            items.append({"kind": "mcp", "id": sid, "title": (meta or {}).get("title", sid),
                                          "status": "installed", "source": "mcp", "method": "mcp_connect",
                                          "how": "MCP-сервер подключён — доступен агенту как инструмент."})
            except Exception:
                pass
            self._send({"status": "success", "count": len(items), "items": items})

        elif self.path == "/x/cap_remove":
            # Удалить способность: СНАЧАЛА с устройства (модель Ollama / MCP-аллоулист / brew),
            # ПОТОМ запись из «Мои» (_mkt_installed). Иначе на компе пользователя остаётся свалка.
            kind = str(body.get("kind", "")).strip(); rid = str(body.get("id", "")).strip()
            method = str(body.get("method", "")).strip()
            if not kind or not rid:
                self._send({"status": "error", "message": "нужны kind и id"}, 400)
                return
            device_removed = None; device_msg = ""; freed = ""
            if kind in ("model", "mcp") or method in ("ollama", "mcp_connect", "brew"):
                du = run_expert("wz_capability_uninstall",
                                {"kind": kind, "ref": rid, "method": method}, wait=150, glob=True)
                if isinstance(du, dict):
                    device_removed = bool(du.get("device_removed"))
                    device_msg = _scrub(du.get("message", "")); freed = du.get("freed", "")
                    if not device_removed:
                        # с устройства не снялось — запись НЕ трём, честно говорим почему
                        self._send({"status": "error", "device_removed": False,
                                    "message": device_msg or "не удалилось с устройства"})
                        return
            try:
                cur = api("/api/kv/get", {"key": "_mkt_installed"})
                val = cur.get("value") if isinstance(cur, dict) else None
                cat = json.loads(val) if val else {"items": []}
            except Exception:
                cat = {"items": []}
            before = len(cat.get("items", []))
            cat["items"] = [it for it in cat.get("items", []) if not (it.get("kind") == kind and it.get("id") == rid)]
            r = api("/api/kv/set", {"key": "_mkt_installed", "value": json.dumps(cat, ensure_ascii=False),
                                    "description": "composer-installed capabilities (Мои)"})
            ok = isinstance(r, dict) and r.get("status") != "error"
            self._send({"status": "success" if ok else "error",
                        "removed": before - len(cat["items"]),
                        "device_removed": device_removed, "freed": freed,
                        "device_message": device_msg,
                        "message": _scrub((r or {}).get("message", "")) if not ok else ""})

        elif self.path == "/x/configure":
            # NL-config: описание словами → Qwen fine-tune → ci:config (эксперт ci_configure, локально в мосту)
            txt = str(body.get("text", "")).strip()
            if not txt:
                self._send({"status": "error", "message": "пустой текст"}, 400)
                return
            try:
                _cfgfn = _load_expert_fn("ci_configure")
                r = _cfgfn(text=txt, api_token=CONFIG.get("auth_token", ""))  # agent_id по умолчанию = Qwen fine-tune
            except Exception as _e:
                self._send({"status": "error", "message": "configure exec: " + str(_e)[:200]})
                return
            self._send(r if isinstance(r, dict) else {"status": "error", "message": "нет ответа"})

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
            s.pop("data_check", None)
            s.setdefault("log", []).append({"ts": now, "event": "file attached: " + fname})
            s["updated_at"] = now
            sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
            self._send({"status": "success", "path": str(fpath), "name": fname})

        elif self.path == "/x/gen_sample":
            # Синтетический файл-образец (ТЗ v2 §20.3, срез v0): реальных данных нет — Qwen строит
            # CSV по интервью с ЗАРАНЕЕ известными контрольными кейсами (дубликат/пропуск/граница).
            # Маркировка synthetic обязательна (имя файла + флаг в сессии): синтетика ≠ прод-данные,
            # приёмка процесса на реальных данных остаётся за владельцем.
            sid = str(body.get("session_id", ""))
            sp = SESS_DIR / (sid + ".json")
            if not SAFE_ID.match(sid or "") or not sp.exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            s = json.loads(sp.read_text(encoding="utf-8"))
            ans = s.get("answers") or {}
            ctx = "\n".join((str(v.get("question", "")) + " — " + str(v.get("answer", "")))[:200]
                            for v in list(ans.values())[:10] if isinstance(v, dict))
            ctx = ctx or (s.get("goal") or s.get("client_name") or "")
            ag = qwen_agent()
            if not ag:
                self._send({"status": "error", "message": "нет Qwen-агента"}, 503)
                return
            prompt = ("Сгенерируй СИНТЕТИЧЕСКИЙ CSV-образец данных для автоматизации бизнес-процесса.\n"
                      "Верни ТОЛЬКО CSV: первая строка — заголовки, разделитель запятая, затем 10–12 строк "
                      "данных. Без пояснений, без markdown-заборов.\n"
                      "Колонки выведи из описания процесса (языком владельца). Обязательно включи "
                      "контрольные кейсы: 1 строку-дубликат, 1 строку с пропущенным значением, "
                      "1 строку с пограничным значением (ноль или очень большое число). "
                      "Все данные вымышленные — никаких реальных ИИН/БИН/счетов/имён.\n\n"
                      "Процесс:\n" + ctx[:1500])
            try:
                res = api("/api/agent/run", {"agent_id": ag, "input": prompt, "run_timeout": 90,
                                             "store": False, "temperature": 0.3}, timeout=100)
                text = ""
                for it in (res or {}).get("output", []):
                    if isinstance(it, dict) and it.get("type") == "message":
                        for c in it.get("content", []):
                            if isinstance(c, dict) and c.get("type") == "output_text":
                                text += c.get("text", "")
                text = text or (res or {}).get("output_text", "")
                text = re.sub(r"^```[a-zA-Z]*\s*|```\s*$", "", text.strip(), flags=re.M).strip()
                lines = [l for l in text.splitlines() if l.strip()]
                if len(lines) < 4 or "," not in lines[0]:
                    self._send({"status": "error", "message": "Qwen не вернул валидный CSV — попробуйте ещё раз"}, 502)
                    return
                fdir = SESS_DIR / (sid + "_files")
                fdir.mkdir(parents=True, exist_ok=True)
                fname = "synthetic_sample.csv"
                fpath2 = fdir / fname
                fpath2.write_text("\n".join(lines) + "\n", encoding="utf-8")
                threading.Thread(target=_sync_file_to_store, args=(sid, str(fpath2)), daemon=True).start()
                now = datetime.now(timezone.utc).isoformat()

                def _mut(x):
                    fl = [f for f in x.get("files", []) if f.get("name") != fname]
                    fl.append({"name": fname, "path": str(fpath2), "size": fpath2.stat().st_size,
                               "uploaded_at": now, "synthetic": True})
                    x["files"] = fl
                    x.pop("data_check", None)
                    x.setdefault("log", []).append({"ts": now, "event": "synthetic sample generated"})
                _update_session(sid, _mut)
                self._send({"status": "success", "name": fname, "rows": len(lines) - 1,
                            "synthetic": True, "preview": "\n".join(lines[:4])})
            except Exception as e:
                self._send({"status": "error", "message": _scrub(str(e)[:150])})

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
                              "ПО ХОДУ разговора — после КАЖДОЙ реплики клиента с фактурой по любой из 9 тем "
                              "(боль, процесс сейчас, объёмы, данные, ПДн, частота, успех, правовая/справочная "
                              "база, владелец) сразу вызывай wz_session save_answers (session_id выше) с "
                              "накопленными темами; не жди конца интервью. Эксперт wz_session — ГЛОБАЛЬНЫЙ: "
                              "вызывай его с global:true, иначе платформа ответит «Expert not found». "
                              "Если вызов всё же упал — скажи клиенту нажать «⤵ Перенести ответы в конспект», "
                              "не выдумывай, что сохранил. Конспект на экране клиента обновляется сам после "
                              "каждого твоего сохранения — это главная ценность. Если клиент отвечает «не знаю», "
                              "«сам найди» или не может назвать детали — НЕ повторяй тот же вопрос и не останавливайся: "
                              "возьми проверяемые поля из приложенного образца/blueprint, явно назови своё предположение "
                              "и перейди к следующему пробелу. Отвечай кратко, это узкая чат-панель.]\n\n")
            # НАША память разговора: платформа не держит контекст, поэтому подаём агенту всю
            # стенограмму сессии + новое сообщение → один непрерывный чат на сессию.
            history_block = ""
            prior = _chat_load(sid)
            if prior:
                lines = ["Клиент: " + str(t.get("text", ""))[:2000] if t.get("role") == "user"
                         else "Ты (помощник): " + str(t.get("text", ""))[:2000] for t in prior]
                hist = "\n".join(lines)
                if len(hist) > 9000:                 # ограничиваем размер: держим свежий хвост
                    hist = "…(начало разговора свёрнуто)…\n" + hist[-9000:]
                history_block = ("[ИСТОРИЯ ТЕКУЩЕГО РАЗГОВОРА этой сессии — помни контекст, НЕ переспрашивай "
                                 "уже отвеченное, продолжай с того же места]\n" + hist +
                                 "\n[Конец истории. НОВОЕ сообщение клиента — ниже.]\n\n")
            def _ans_count():
                try:
                    _p = SESS_DIR / (sid + ".json")
                    if SAFE_ID.match(sid or "") and _p.exists():
                        return len((json.loads(_p.read_text(encoding="utf-8")).get("answers") or {}))
                except Exception:
                    pass
                return 0
            _ac0 = _ans_count()   # #17: answers_count ДО хода агента — UI сверит, реально ли сработал автосейв
            payload = {"agent_id": CONFIG["agent_id"],
                       "input": surface_note + history_block + enriched,
                       "run_timeout": 180, "store": True}
            res, text, _chat_attempts = _run_chat_agent(payload)
            if not text:
                # Сырой ответ платформы наружу не отдаём: человеку нужен ПОНЯТНЫЙ ответ
                # и действие, а техническая расшифровка — отдельным полем для нас.
                _h = _llm_error_human(res)
                self._send({"status": "error",
                            "message": _h or "Помощник не ответил. Повторите вопрос — если повторится, "
                                             "скажите нам, что именно спрашивали.",
                            "detail": _scrub(str(res)[:300]), "attempts": _chat_attempts})
                return
            if text:                              # записываем обмен в стенограмму сессии (память чата)
                _chat_add_exchange(sid, user_input, text)
            _ac1 = _ans_count()   # #17: сверка — если фактура была, а ответов не прибавилось, UI предложит «Перенести в конспект»
            self._send({"status": "success", "text": text, "response_id": res.get("id"),
                        "answers_count": _ac1, "answers_saved": bool(_ac1 > _ac0),
                        "attempts": _chat_attempts})
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
                for _pv in APP_DIR.glob("*.prev"):   # многофайл: восстановить ВСЕ файлы прошлой версии
                    shutil.copy2(str(_pv), str(APP_DIR / _pv.name[:-5]))   # отрезаем ".prev"
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

    # F1: разобрать сирот прошлого процесса (стройки, убитые рестартом) — фоном, не блокируя bind
    threading.Thread(target=lambda: print("recovery: осиротевших строек разобрано:", _recover_orphan_builds()), daemon=True).start()
    threading.Thread(target=lambda: print("recovery: сессий разблокировано:", _unstick_sessions()), daemon=True).start()
    threading.Thread(target=lambda: print("janitor: осиротевших привязок вычищено:", _janitor_orphan_bindings()), daemon=True).start()
    print("Extella Adoption Wizard bridge v%s on http://127.0.0.1:%d (owner=%s)" % (BRIDGE_VERSION, port, owner))
    srv.serve_forever()
