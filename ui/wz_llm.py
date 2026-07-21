"""LLM / агент-роутинг моста Визарда (Фаза 1, шов #2).

Выделено из server.py. Содержит:
  - design_agent()        — выбор Qwen-модели для ПРОЕКТИРОВАНИЯ (blueprint/план): ёмкий вывод;
  - _gen_identity(...)     — личность агента для витрины (Qwen → emoji/accent/tagline/caps/category);
  - _llm_backend_down(...) — распознать «бэкенд Qwen-агента лёг» (флап ngrok / пустой вывод);
  - run_llm_expert(...)    — LLM-эксперт с ретраем и ФОЛБЭКОМ по цепочке Qwen-агентов.
Зависит ТОЛЬКО от wz_platform (CONFIG, api, run_expert, qwen_agent, qwen_agents) + stdlib.
Правило канона: клиентский LLM — ТОЛЬКО Qwen, никогда Claude (agent_extella_default).
"""
import time
import json
from wz_platform import CONFIG, api, run_expert, qwen_agent, qwen_agents


def gen_panel_manifest(goal, stages):
    """§7bis ступень 3: Qwen по blueprint (goal + stages) → доменные ПОЛЯ настроек владельца.
    Возвращает {"fields":[{key,label,type,hint,options?,default?}]} или None. Чистый (без I/O сессии):
    вызывают и эндпоинт /x/gen_panel, и стройка (авто-панель у новых автоматизаций). Клиентский LLM — Qwen."""
    import re as _re
    import json as _json
    ag = qwen_agent()
    if not ag:
        return None
    st_txt = "\n".join("- " + str(st.get("title", "")) + ": вход " + str(st.get("inputs", ""))[:120]
                       for st in (stages or [])[:8])
    prompt = ("Ты проектируешь настройки бизнес-процесса для его владельца. По описанию процесса выдели "
              "3–6 НАСТРОЕК, которые владелец должен задать или сможет менять (пороги, лимиты, имена/роли, "
              "период, тон, валюта). НЕ технические параметры. Верни ТОЛЬКО JSON:\n"
              '{"fields":[{"key":"<латиница_snake>","label":"<по-русски>","type":"text|number|select",'
              '"hint":"<кратко>","options":["..."]?,"default":"<если есть>"}]}\n'
              "type=select только если у настройки явно перечислимые значения (тогда options обязательны).\n\n"
              "Процесс: " + str(goal or "")[:600] + "\nШаги:\n" + st_txt)
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
        m = _re.search(r"\{.*\}", text, _re.S)
        raw = _json.loads(m.group(0)) if m else {}
    except Exception:
        return None
    _rf = raw.get("fields")
    if not isinstance(_rf, list):            # Qwen мог вернуть не-список → не падаем
        return None
    clean, used = [], set()
    for f in _rf[:6]:
        if not isinstance(f, dict):
            continue
        key = _re.sub(r"[^a-z0-9_]", "_", str(f.get("key", "")).strip().lower())[:40].strip("_")
        label = str(f.get("label", "")).strip()[:80]
        typ = f.get("type") if f.get("type") in ("text", "number", "select") else "text"
        if not key or not label or key in used:
            continue
        used.add(key)
        fld = {"key": key, "label": label, "type": typ, "hint": str(f.get("hint", ""))[:120]}
        if typ == "select":
            _o = f.get("options")
            _o = _o if isinstance(_o, list) else []       # строка options НЕ режем посимвольно
            opts = [str(o).strip()[:40] for o in _o if str(o).strip()][:8]
            if len(opts) < 2:
                fld["type"] = "text"
            else:
                fld["options"] = opts
        if f.get("default"):
            fld["default"] = str(f.get("default"))[:120]
        clean.append(fld)
    return {"fields": clean} if clean else None


def _llm_backend_down(res):
    """Ошибка похожа на «бэкенд Qwen-агента недоступен» (флап ngrok-туннеля / пустой вывод LLM)?"""
    if not isinstance(res, dict) or res.get("status") != "error":
        return False
    m = str(res.get("message", "")).lower()
    return any(k in m for k in ("llm empty output", "endpoint", "ngrok", "offline", "err_ngrok", "platform llm"))


def llm_transient_error(res):
    """Only transport/backend failures are retryable; bad contracts and parse errors are final."""
    if not isinstance(res, dict):
        return True
    status = str(res.get("status") or "").lower()
    if status in ("timeout", "timed_out", "temporarily_unavailable"):
        return True
    if status not in ("error", "failed"):
        return False
    blob = json.dumps(res, ensure_ascii=False, default=str).lower()[:1200]
    code = int(res.get("http_code") or 0) if str(res.get("http_code") or "").isdigit() else 0
    return (_llm_backend_down(res) or code in (408, 429, 500, 502, 503, 504) or any(
        marker in blob for marker in (
            "timeout", "timed out", "read operation timed out", "connection reset",
            "connection aborted", "connection refused", "remote end closed", "temporary failure",
            "temporarily unavailable", "network is unreachable", "service unavailable")))


def run_llm_expert(expert_name, params, wait=660, agents=None, target=None):
    """LLM-эксперт с ретраем и ФОЛБЭКОМ по цепочке Qwen-агентов (config.llm_agents): основной моргнул →
    следующий. Делает keyless-путь устойчивым к падению бэкенда одного агента. Возвращает первый НЕ-LLM-ошибочный
    результат либо последнюю ошибку. agents — явная цепочка (напр. design-агент первым), иначе qwen_agents()."""
    _raw = (agents or []) + qwen_agents() if agents else qwen_agents()
    seen, agents = set(), []
    for a in (_raw or [""]):
        if a and a not in seen:
            seen.add(a)
            agents.append(a)
    agents = agents or [""]
    last = None
    for aid in agents:
        p = dict(params)
        p["agent_id"] = aid
        for attempt in range(2):   # флап обычно отпускает за секунды → короткий ретрай
            r = run_expert(expert_name, p, wait=wait, glob=True, target=target)
            if not llm_transient_error(r):
                return r
            last = r
            time.sleep(2)
    return last or {"status": "error", "message": "все Qwen-агенты недоступны (бэкенд не отвечает)"}


def _gen_identity(name, description, experts):
    """Личность агента для витрины: Qwen по имени/описанию/экспертам → emoji/accent/tagline/caps/category.
    Каждый агент индивидуален (разные цвет/эмодзи/слоган). Фолбэк {} — publish возьмёт эвристику."""
    import re as _re, json as _json
    ag = qwen_agent()
    if not ag:
        return {}
    prompt = ("Придумай ИНДИВИДУАЛЬНУЮ личность агента для витрины. Верни ТОЛЬКО JSON:\n"
              '{"emoji":"<один эмодзи по роли>","accent":"#RRGGBB","tagline":"<живой слоган 4-7 слов от лица пользы>",'
              '"capabilities":["<3-5 коротких умений>"],"category":"Документы|Продажи и клиенты|Контент|Автоматизация"}\n'
              "Эмодзи и цвет — РАЗНЫЕ у разных ролей, отражают суть. Только JSON.\n\n"
              "Агент: %s\nОписание: %s\nПод-эксперты: %s" % (name, description or name, ", ".join(experts) if experts else "—"))
    try:
        res = api("/api/agent/run", {"agent_id": ag, "input": prompt, "run_timeout": 120, "store": True}, timeout=140)
        text = ""
        for item in (res or {}).get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        text += c.get("text", "")
        m = _re.search(r"\{.*\}", text, _re.S)
        idv = _json.loads(m.group(0)) if m else {}
    except Exception:
        return {}
    out = {}
    if idv.get("emoji"):
        out["emoji"] = str(idv["emoji"])[:4]
    if idv.get("accent") and _re.match(r"^#[0-9a-fA-F]{6}$", str(idv["accent"])):
        out["accent"] = idv["accent"]
    if idv.get("tagline"):
        out["tagline"] = str(idv["tagline"])[:80]
    if idv.get("category"):
        out["category"] = str(idv["category"])[:40]
    if idv.get("capabilities"):
        def _trim(s, n=32):
            s = str(s).strip()
            if len(s) <= n:
                return s
            cut = s[:n]
            sp = cut.rfind(" ")
            return (cut[:sp] if sp > 12 else cut).rstrip(" ,.")
        out["capabilities"] = ",".join(_trim(c) for c in idv["capabilities"][:5])
    return out


def design_agent():
    """Модель для ПРОЕКТИРОВАНИЯ (blueprint/план стройки): большой JSON, нужен ЁМКИЙ вывод.
    Fine-tune (кодоген-мозг) ОБРЕЗАЕТ большой план (жёсткий потолок ~6K токенов), а БАЗОВЫЙ qwen3.7-max тянет.
    ЖЕЛЕЗНОЕ ПРАВИЛО: НИКОГДА Claude — только Qwen. Дефолт = свой Qwen-Визард клиента (config.agent_id);
    настраивается config.design_agent_id (тоже ТОЛЬКО Qwen)."""
    return CONFIG.get("design_agent_id") or CONFIG.get("agent_id", "")
