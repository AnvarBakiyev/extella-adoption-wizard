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
FILE_CHUNK = 8000            # размер чанка base64 в KV (крупные значения KV нестабильны → чанкуем)
HOST_TARGET = "85800354-f7b7-449f-b526-9357cd91f780"  # managed-хостинг VPS (PS.kz) — куда пиннить процессы 24/7
SCHED_INDEX_KEY = "sched:__index__"  # индекс активных расписаний (список sid) — тик читает его вместо прохода по всему KV
INBOUND_INDEX_KEY = "inbound:__index__"  # индекс процессов с включённым приёмом входящих (B2) — тик читает его
BRIDGE_VERSION = "3.98"       # версия моста; /x/health отдаёт её, single-instance по ней решает «свежий/старый»
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
        s = json.loads(sp.read_text(encoding="utf-8"))
        mutate(s)
        s["updated_at"] = datetime.now(timezone.utc).isoformat()
        sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        return s


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


def _rules_payload(s):
    """F2: полезная нагрузка rules_json прогона — текстовые правила (их читают кодогенные стадии)
    + скомпилированные структурные фильтры {field,op,value} (их детерминированно применяет оркестратор)."""
    return list(s.get("rules") or []) + [r for r in (s.get("rules_struct") or []) if isinstance(r, dict)]


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


# Фаза 1 шов #3: кластер стройки вынесен в wz_build.py
from wz_build import _run_build, sample_preflight


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
            out = [{"connector": k, "set_at": v} for k, v in sorted(idx.items())]
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
            self._send(json.loads(CATALOG_PATH.read_text(encoding="utf-8")))
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
                    "components": lb.get("components_human") or lb.get("experts") or [],   # C5: ярлыки для плиток
                    "params_contract": int(lb.get("params_contract", 0) or 0),   # F2: правила влияют на прогоны?
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
                # LLM-эксперт: ретрай + фолбэк по цепочке Qwen-агентов (устойчивость к флапу бэкенда)
                self._send(run_llm_expert(expert, params))
            else:
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

        elif self.path == "/x/build":
            sid = str(body.get("session_id", ""))
            if not SAFE_ID.match(sid or "") or not (SESS_DIR / (sid + ".json")).exists():
                self._send({"status": "error", "message": "session not found"}, 404)
                return
            if not (SESS_DIR / (sid + "_blueprint.json")).exists():
                self._send({"status": "error", "message": "сначала соберите план процесса"}, 400)
                return
            _pf = sample_preflight(sid)   # WZ-07: честный отказ ДО стройки, не после минут сборки
            if not _pf.get("ok"):
                self._send({"status": "error", "code": "sample_required", "message": _pf.get("message")}, 400)
                return
            build_id = "build_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M") + "_" + uuid.uuid4().hex[:4]
            (RUNS_DIR / build_id).mkdir(parents=True, exist_ok=True)
            (RUNS_DIR / build_id / "build_progress.json").write_text(
                json.dumps({"build_id": build_id, "session_id": sid, "status": "running", "stages": [],
                            "kind": "build", "resume_sched": False},   # F1: журнал работы для recovery
                           ensure_ascii=False), encoding="utf-8")
            threading.Thread(target=_run_build, args=(sid, build_id), daemon=True).start()
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
                try:
                    _run_build(sid, build_id)
                finally:
                    _update_session(sid, lambda sx: sx.pop("building", None))
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
            orch = builds[-1]["orchestrator"]
            lb = builds[-1]   # F2: действующая сборка (params_contract решает, передавать ли rules/fields)
            if isinstance(orch, dict):
                orch = orch.get("expert_name")
            if orch == "ci_run_pipeline":
                # API-based процесс (источники по сети, без source_file): гоняем server-side через run_expert
                # (надёжный синтез Qwen — локальный exec в HTTP-хендлере обрывает длинный /api/agent/run).
                # agent_id = живой Qwen клиента (старый fine-tune agent_iVWW… удалён с платформы → 404)
                res = run_expert("ci_run_pipeline", {"agent_id": qwen_agent(), "deliver": "none",
                                 "api_token": CONFIG.get("auth_token", "")}, wait=240, glob=True)
                if not isinstance(res, dict) or res.get("status") != "success":
                    self._send({"status": "error", "message": "run: " + str(res)[:180]})
                    return
                digest = res.get("digest_md") or res.get("digest_preview", "")
                _save_digest(sid, digest)   # C3: виджет «Последний результат»
                run_rec = {"trigger": "manual", "at": datetime.now(timezone.utc).isoformat(), "status": res.get("status"),
                           "findings": res.get("findings"), "digest_source": res.get("digest_source")}
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
                res = run_expert("wz_flow_run", _fl_params, wait=260, glob=True)
                ok = isinstance(res, dict) and res.get("status") == "success"
                digest = ((res or {}).get("digest_md") or (res or {}).get("digest") or "") if ok else ""
                _save_digest(sid, digest)   # C3
                run_rec = {"trigger": "manual", "at": datetime.now(timezone.utc).isoformat(),
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
                res = run_expert(orch, _op, wait=600, glob=True)
                ok = isinstance(res, dict) and str(res.get("status", "")) in ("success", "partial")
                digest = ((res or {}).get("digest_md") or (res or {}).get("digest") or "") if isinstance(res, dict) else ""
                _save_digest(sid, digest)   # C3
                summ = (res or {}).get("summary") if isinstance(res, dict) else None
                run_rec = {"trigger": "manual", "at": datetime.now(timezone.utc).isoformat(),
                           "status": (res or {}).get("status", "error") if isinstance(res, dict) else "error",
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
                _fp = {"api_token": CONFIG["auth_token"], "source_file": src,
                       "source_key": _skey, "target": HOST_TARGET}
                if int(lb.get("params_contract", 0) or 0) >= 1:   # F2
                    if s.get("rules"):
                        _fp["rules_json"] = json.dumps(_rules_payload(s), ensure_ascii=False)
                    if s.get("fields"):
                        _fp["fields_json"] = json.dumps(s["fields"], ensure_ascii=False)
                res = run_expert(orch, _fp, wait=900, target=HOST_TARGET, glob=True)
            else:
                _fp2 = {"api_token": CONFIG["auth_token"], "source_file": src}
                if int(lb.get("params_contract", 0) or 0) >= 1:   # F2
                    if s.get("rules"):
                        _fp2["rules_json"] = json.dumps(_rules_payload(s), ensure_ascii=False)
                    if s.get("fields"):
                        _fp2["fields_json"] = json.dumps(s["fields"], ensure_ascii=False)
                res = run_expert(orch, _fp2, wait=900)
            # WZ-10 (ТЗ v2 §25): финал прогона честный — success без сводки и счётчиков не бывает
            if isinstance(res, dict) and res.get("status") == "success" \
               and res.get("total_count") is None and not res.get("summary"):
                res["status"] = "partial"
                res["message"] = "оркестратор не вернул сводку (summary/total_count) — проверьте процесс"
            summ = res.get("summary") if isinstance(res, dict) else None
            run_rec = {"trigger": "manual", "at": datetime.now(timezone.utc).isoformat(),
                       "status": (res or {}).get("status", "unknown"),
                       "summary": summ, "total_count": (res or {}).get("total_count"),
                       "total_sum": (res or {}).get("total_sum"),
                       "report_md": (res or {}).get("report_md"), "report_xlsx": (res or {}).get("report_xlsx"),
                       "source_file": src}
            if (res or {}).get("needs_review_reason"):   # WZ-B02 → Exception Inbox: причина словами
                run_rec["needs_review_reason"] = str(res["needs_review_reason"])[:200]
            s = _update_session(sid, lambda s: s.setdefault("runs", []).append(run_rec))
            # доставка результата в канал (как по расписанию) — чтобы РУЧНОЙ запуск тоже слал, а не только тик
            delivered = None
            recips = _recipients(s)   # несколько получателей: шлём результат в каждый подключённый канал
            if recips and (res or {}).get("status") == "success":
                tc = (res or {}).get("total_count"); ts = (res or {}).get("total_sum")
                _nm = s.get("client_name") or sid
                msg = _render_msg(s.get("message_template"), _nm, tc, ts)   # шаблон автоматизации (кабина «Шаблон»)
                delivered = []
                for deliver in recips:
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
                    delivered.append({"channel": deliver, "ok": bool(isinstance(dout, dict) and dout.get("ok")),
                                      "err": ((dout or {}).get("err") if isinstance(dout, dict) else None)})
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
                api("/api/kv/set", {"key": kvkey, "value": json.dumps(kvval, ensure_ascii=False),
                                    "description": "schedule " + sid})
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
                        if _iv:   # Resume: следующий запуск через интервал, не мгновенный шторм
                            cfg["next_due_ts"] = (datetime.now(timezone.utc) + timedelta(minutes=_iv)).isoformat()
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

            # F2: текстовое правило компилируется в машинные фильтры {field,op,value} ОДИН раз при записи
            # (Qwen интерпретирует здесь; на прогонах оркестратор применяет структуры детерминированно).
            # Qwen упал/не фильтр → structs=[] — честная деградация: текст останется, учтётся пересборкой.
            structs = []
            if rules:
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
                ag = qwen_agent()
                if ag:
                    try:
                        prompt = ("Преврати правила владельца бизнес-процесса в машинные фильтры записей данных.\n"
                                  "Верни ТОЛЬКО JSON-массив без пояснений: "
                                  '[{"field":"<имя поля данных>","op":">"|">="|"<"|"<="|"=="|"contains","value":<число или строка>}]\n'
                                  "Бери только правила-фильтры (какие записи показывать). Сортировку, оформление, "
                                  "доставку, сроки — пропускай. Ничего не фильтрует — верни [].\n"
                                  + ("Известные поля данных: " + ", ".join(known[:12]) + "\n" if known else "")
                                  + "Правила:\n- " + "\n- ".join(rules))
                        res = api("/api/agent/run", {"agent_id": ag, "input": prompt, "run_timeout": 60,
                                                     "store": False, "temperature": 0}, timeout=70)
                        text = ""
                        for it in (res or {}).get("output", []):
                            if isinstance(it, dict) and it.get("type") == "message":
                                for c in it.get("content", []):
                                    if isinstance(c, dict) and c.get("type") == "output_text":
                                        text += c.get("text", "")
                        text = text or (res or {}).get("output_text", "")
                        m = re.search(r"\[.*\]", text, re.S)
                        for v in (json.loads(m.group(0)) if m else []):
                            if isinstance(v, dict) and str(v.get("field", "")).strip() \
                               and str(v.get("op", "")) in (">", ">=", "<", "<=", "==", "contains"):
                                structs.append({"field": str(v["field"]).strip()[:60],
                                                "op": str(v["op"]), "value": v.get("value")})
                    except Exception:
                        structs = []
            structs = structs[:10]

            def _mut(s):
                s["rules"], s["fields"], s["rules_struct"] = rules, fields, structs
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
            self._send({"status": "success", "rules": rules, "fields": fields, "rules_struct": structs})

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
            _update_session(sid, lambda s: (s.__setitem__("questionnaire", clean),
                                            s.__setitem__("questionnaire_task", task)))
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
                      '{"depth":"light"|"medium"|"strong","op":"recipients"|"schedule"|"template"|"pause"|"resume"|"other",'
                      '"args":{...},"summary":"<что будет сделано, одной фразой, тем же языком что фраза владельца>"}\n\n'
                      "light = настройка обвязки, БЕЗ изменения логики:\n"
                      "- получатели результата → op=recipients, args.recipients = ПОЛНЫЙ НОВЫЙ список из [telegram,email,slack,whatsapp,sms] (учти текущих!)\n"
                      "- расписание → op=schedule, args.period ∈ [Каждый час,Ежедневно,Еженедельно,Ежемесячно] или \"\" чтобы снять\n"
                      "- текст сообщения получателям → op=template, args.template=<новый текст, плейсхолдеры {name}{count}{sum}{date}>\n"
                      "- остановить/включить → op=pause | op=resume\n"
                      "medium = новое поле/правило/фильтр/колонка, влияющие на расчёт или содержание результата (op=other).\n"
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
                if op not in ("recipients", "schedule", "template", "pause", "resume", "other"):
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
            if not kind or not ref:
                self._send({"status": "error", "message": "нужны kind и install_ref"}, 400)
                return
            res = run_expert("wz_capability_install",
                             {"kind": kind, "install_ref": ref, "method": str(body.get("method", "")),
                              "pkg_type": str(body.get("pkg_type", "")), "title": str(body.get("title", "")),
                              "desc": str(body.get("desc", "")), "url": str(body.get("url", "")),
                              "source": str(body.get("source", ""))}, wait=320, glob=True)
            if isinstance(res, dict):
                if res.get("status") in ("success", "installing"):
                    _registry_refresh_async()   # установка способности — реестр вслед
                self._send({"status": res.get("status", "success"), "install_status": res.get("install_status"),
                            "registered_in_my": res.get("registered_in_my"), "installed": res.get("installed"),
                            "message": _scrub(res.get("message", ""))})
            else:
                self._send({"status": "error", "message": _scrub(str(res)[:200])})

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
                              "каждого твоего сохранения — это главная ценность. Отвечай кратко, это узкая чат-панель.]\n\n")
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
            payload = {"agent_id": CONFIG["agent_id"],
                       "input": surface_note + history_block + enriched,
                       "run_timeout": 180, "store": True}
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
            if text:                              # записываем обмен в стенограмму сессии (память чата)
                _chat_add_exchange(sid, user_input, text)
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
    print("Extella Adoption Wizard bridge v%s on http://127.0.0.1:%d (owner=%s)" % (BRIDGE_VERSION, port, owner))
    srv.serve_forever()
