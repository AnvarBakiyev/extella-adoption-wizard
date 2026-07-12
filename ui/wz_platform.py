"""Платформенный клиент моста Визарда — низкоуровневый слой общения с Extella API.

Выделено из server.py (Фаза 1, шов #1). Содержит: конфиг, заголовки, скраб секретов,
вызов API, разбор результата эксперта, run_expert (в т.ч. deferred-поллинг), выбор Qwen-агента.
Зависит ТОЛЬКО от config.json + стандартной библиотеки — ничего из server.py не импортит.
server.py импортит отсюда: CONFIG, BASE, HEADERS, _scrub, api, parse_expert_result, run_expert, qwen_agent.
"""
import json
import time
import ast
import urllib.request
import urllib.error
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG = json.loads((APP_DIR / "config.json").read_text(encoding="utf-8"))
BASE = "https://api.extella.ai"
HEADERS = {"X-Auth-Token": CONFIG["auth_token"], "Content-Type": "application/json",
           "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}


def qwen_agents():
    """Цепочка Qwen-агентов для keyless-LLM: основной → фолбэки. Источник — config.llm_agents (список)
    ИЛИ [llm_agent_id, agent_id]. Порядок = приоритет (первый = основной; это и есть выбор пользователя).
    НИКОГДА не включаем agent_extella_default (это Claude, платно). Устойчивость к флапу бэкенда агента:
    если основной не отвечает (ngrok-туннель лёг) — берём следующего."""
    chain = CONFIG.get("llm_agents")
    if not isinstance(chain, list) or not chain:
        chain = [CONFIG.get("llm_agent_id"), CONFIG.get("agent_id")]
    seen, out = set(), []
    for a in chain:
        if a and a != "agent_extella_default" and a not in seen:
            seen.add(a)
            out.append(a)
    return out or [CONFIG.get("agent_id", "")]


def qwen_agent():
    """Основной Qwen-агент (первый в цепочке). Для мест, где фолбэк не нужен."""
    ch = qwen_agents()
    return ch[0] if ch else CONFIG.get("agent_id", "")


def _scrub(s):
    """Секреты не наружу (чат/лог/UI): вырезаем auth_token из любых сообщений (canon: scrub)."""
    try:
        s = str(s)
        tok = CONFIG.get("auth_token", "")
        if tok and len(tok) >= 6:
            s = s.replace(tok, "***")
        return s
    except Exception:
        return str(s)


def api(endpoint, payload, timeout=180):
    req = urllib.request.Request(BASE + endpoint, data=json.dumps(payload).encode(),
                                 headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # тело ошибки может отразить запрос с токеном -> скрабим, наружу не отдаём секрет
        return {"status": "error", "http_code": e.code, "message": _scrub(e.read().decode()[:500])}
    except Exception as e:
        return {"status": "error", "message": _scrub(str(e)[:300])}


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
    return parse_expert_result(res)   # СИНХРОННЫЙ результат (task_id пуст)
