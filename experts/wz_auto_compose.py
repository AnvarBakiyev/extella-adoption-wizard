# expert: wz_auto_compose
# description: Композитор: задача словами -> Qwen подбирает блоки из каталога способностей (composer:catalog), собирает декларативный план, ставит недостающие локальные модели, пишет flow:<id> в KV и карточку в _mkt_automations. Только вет-проверенные блоки (whitelist) — чего нет, честно возвращает в missing. Возвращает {flow_id, plan, card, missing}.

def wz_auto_compose(task="", agent_id="", api_token="", api_base="https://api.extella.ai", reuse_flow_id="") -> dict:
    import json, re, urllib.request
    from pathlib import Path
    from datetime import datetime, timezone

    def _b(v):
        return (not v) or str(v).startswith("{{")

    if _b(task):
        return json.dumps({"status": "error", "message": "опиши задачу словами (task)"}, ensure_ascii=False)
    if _b(agent_id):
        # дефолт — свой Qwen клиента из конфига Визарда (прежний хардкод agent_iVWW… удалён с платформы → 404)
        _cfgp = Path.home() / "extella_wizard" / "app" / "config.json"
        try:
            _c = json.loads(_cfgp.read_text(encoding="utf-8")) if _cfgp.exists() else {}
        except Exception:
            _c = {}
        _ch = _c.get("llm_agents") or []
        agent_id = ((_ch[0] if isinstance(_ch, list) and _ch else "") or _c.get("llm_agent_id") or _c.get("agent_id") or "")
    if _b(agent_id):
        return json.dumps({"status": "error", "message": "нет agent_id (Qwen): передай параметром или заполни config.json Визарда"}, ensure_ascii=False)
    if _b(api_base):
        api_base = "https://api.extella.ai"
    if _b(api_token):
        cfg = Path.home() / "extella_wizard" / "app" / "config.json"
        try:
            api_token = json.loads(cfg.read_text(encoding="utf-8")).get("auth_token", "") if cfg.exists() else ""
        except Exception:
            api_token = ""
    if not api_token:
        return json.dumps({"status": "error", "message": "нет api_token"}, ensure_ascii=False)

    # КАНОН: KV/expert-run идут в СЛУЖЕБНЫЙ скоуп agent_extella_default (иначе KV не виден серверу/тулбару);
    # Qwen (agent_id) — ТОЛЬКО на /api/agent/run (LLM). agent_extella_default тут = скоуп, не платный вызов.
    SVC = "agent_extella_default"
    headers = {"X-Auth-Token": api_token, "Content-Type": "application/json",
               "X-Profile-Id": "default", "X-Agent-Id": SVC}

    def _post(path, body, t=240, agent=None):
        h = dict(headers)
        if agent:
            h["X-Agent-Id"] = agent
        req = urllib.request.Request(api_base.rstrip("/") + path,
                                     data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=t) as r:
            return json.loads(r.read().decode("utf-8"))

    # --- каталог блоков (вет-проверенный whitelist) ---
    try:
        catalog_val = _post("/api/kv/get", {"key": "composer:catalog", "global": True}, t=60).get("value")
    except Exception as ex:
        return json.dumps({"status": "error", "message": "не прочитал каталог блоков (сеть/KV): " + str(ex)[:120]}, ensure_ascii=False)
    try:
        catalog = json.loads(catalog_val).get("blocks", []) if catalog_val else []
    except Exception as ex:
        return json.dumps({"status": "error", "message": "каталог блоков повреждён (не JSON): " + str(ex)[:120]}, ensure_ascii=False)
    if not catalog:
        return json.dumps({"status": "error", "message": "composer:catalog пуст — засидируйте каталог блоков"}, ensure_ascii=False)
    allowed = {b["id"] for b in catalog}

    # --- какие локальные модели реально стоят на устройстве (Ollama) — чтобы Qwen подобрал под задачу ---
    local_models = []
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=8) as _r:
            local_models = [m.get("name", "") for m in json.loads(_r.read().decode("utf-8")).get("models", []) if m.get("name")]
    except Exception:
        local_models = []

    # --- Qwen: задача + каталог -> строгий JSON-план ---
    cat_txt = "\n".join("- %s [%s]: %s | params: %s%s" % (
        b["id"], b.get("kind", ""), b.get("what", ""), json.dumps(b.get("params", {}), ensure_ascii=False),
        (" | requires_model: " + b["requires_model"]) if b.get("requires_model") else "")
        for b in catalog)
    lm_txt = ", ".join(local_models) or "none installed"
    prompt = (
        "You are the Extella Composer. Compose an automation ONLY from the block catalog below. Do NOT invent block ids.\n"
        "USER TASK:\n" + str(task) + "\n\nBLOCK CATALOG:\n" + cat_txt + "\n\n"
        "INSTALLED LOCAL MODELS on the user's device (use for any block that takes a `model` param): " + lm_txt + "\n\n"
        "Return EXACTLY one JSON object, nothing else (no prose, no fences):\n"
        '{"name": <short automation name in the SAME language as the USER TASK>, "description": <one sentence in the task\'s language>, "emoji": <one emoji>, '
        '"steps": [{"expert": <block id from catalog>, "params": {<param>: <value>}, "save_as": <short_key>}], '
        '"synthesis_prompt": <instructions in English for the analyst agent that will turn step results into a markdown brief with sections and a table; be specific to the task; MUST end with: "Write the brief in the same language as the user task.">, '
        '"missing": [<capability the task needs but the catalog lacks, empty if none>]}\n'
        "Rules: prefer the fewest steps that solve the task; use realistic params (folders under ~/extella_wizard/, counts 5-15); "
        "for any block with a `model` param pick the MOST task-appropriate model from INSTALLED LOCAL MODELS "
        "(finance-tuned for money/bank/subscription tasks, medical for health, law for legal, else a general one); "
        "to feed an earlier step's output into a later step's text param, embed {{save_as}} inside the text (e.g. \"...data: {{scan}}. Analyze...\"); "
        "if the task cannot be solved with the catalog, put what's lacking into missing and still compose the best partial plan."
    )
    try:
        resp = _post("/api/agent/run", {"agent_id": agent_id, "input": prompt, "store": False,
                                        "temperature": 0, "tool_choice": "none", "max_output_tokens": 1600}, agent=agent_id)
        parts = []
        for it in (resp.get("output") or []):
            if isinstance(it, dict) and it.get("type") == "message":
                for c in it.get("content", []):
                    if isinstance(c, dict) and c.get("text"):
                        parts.append(c["text"])
        txt = ("\n".join(parts) or resp.get("output_text") or "").strip()
        s, e = txt.find("{"), txt.rfind("}")
        plan = json.loads(txt[s:e + 1])
    except Exception as ex:
        return json.dumps({"status": "error", "message": "композиция не разобралась: " + str(ex)[:140]}, ensure_ascii=False)

    # --- валидация: только блоки из whitelist; defaults каталога побеждают несуществующие пути ---
    import os
    by_id = {b["id"]: b for b in catalog}

    def _san(k):   # одинаковая нормализация для save_as и для {{ссылок}}
        return re.sub(r"[^a-z0-9_]", "_", str(k).lower())[:24] or "step"

    steps = []
    missing = [str(m) for m in (plan.get("missing") or [])]
    orig_to_san = {}   # исходный ключ Qwen -> санитизированный save_as (чтобы {{ссылки}} не рассинхронились)
    used_san = set()
    for st in (plan.get("steps") or []):
        if not isinstance(st, dict) or st.get("expert") not in allowed:
            missing.append("unknown block: " + str((st or {}).get("expert")))
            continue
        blk = by_id[st["expert"]]
        raw_p = st.get("params")
        params = dict(raw_p) if isinstance(raw_p, dict) else {}   # [15] Qwen мог прислать params не объектом
        if raw_p and not isinstance(raw_p, dict):
            missing.append("шаг %s: params не объект — взяты дефолты блока" % st["expert"])
        for k, dv in (blk.get("defaults") or {}).items():
            v = params.get(k)
            # модель могла выдумать путь, которого нет на устройстве — берём проверенный default
            if not v:
                params[k] = dv
            elif isinstance(v, str) and ("/" in v or v.startswith("~")) and not os.path.exists(os.path.expanduser(v)):
                params[k] = dv
        # блок гоняет локальную модель -> выбранная модель ДОЛЖНА быть реально установлена;
        # для доменной задачи доменная модель (finance-llama для денег/банка и т.п.) имеет приоритет
        if local_models and (blk.get("requires_model") or "model" in (blk.get("params") or {})):
            tl = str(task).lower()
            dom_pick = None
            for tag, kws in (("finan", ["financ", "money", "bank", "subscription", "spend", "budget", "invoice", "payment", "tax", "expense", "финанс", "деньг", "банк", "подписк", "расход"]),
                             ("medic", ["medical", "health", "clinic", "patient", "diagnos", "symptom", "медиц", "здоров", "пациент"]),
                             ("law",   ["law", "legal", "contract", "court", "compliance", "юрид", "закон", "договор", "суд"])):
                if any(k in tl for k in kws):
                    dom_pick = next((m for m in local_models if tag in m.lower()), None)
                    if dom_pick:
                        break
            mv = params.get("model")
            if dom_pick:
                params["model"] = dom_pick
            elif not mv or mv not in local_models:
                rq = blk.get("requires_model")
                params["model"] = rq if rq in local_models else next((m for m in local_models if "qwen" in m.lower()), local_models[0])
        # санитизируем save_as и разводим дубли (иначе результаты шагов затирают друг друга)
        orig = str(st.get("save_as") or st["expert"])
        san = _san(orig); base = san; n = 2
        while san in used_san:
            san = (base[:22] + "_" + str(n)); n += 1
        used_san.add(san); orig_to_san[orig] = san
        steps.append({"expert": st["expert"], "params": params, "save_as": san})
    if not steps:
        return json.dumps({"status": "error", "message": "план без валидных шагов", "missing": missing, "plan_raw": plan}, ensure_ascii=False)

    # [4/6] синхронизируем {{ссылки}} с санитизированными save_as и валидируем разрешимость (только на ПРЕДЫДУЩИЕ шаги)
    ref_rx = re.compile(r"\{\{(\w+)((?:\.\w+)?)\}\}")
    def _rewrite(v):
        if isinstance(v, str):
            return ref_rx.sub(lambda mo: "{{" + orig_to_san.get(mo.group(1), mo.group(1)) + mo.group(2) + "}}", v)
        if isinstance(v, dict):
            return {k: _rewrite(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_rewrite(x) for x in v]
        return v
    seen = set()
    for sp in steps:
        sp["params"] = _rewrite(sp["params"])
        for mo in ref_rx.finditer(json.dumps(sp["params"], ensure_ascii=False)):
            if mo.group(1) not in seen:
                missing.append("шаг %s ссылается на {{%s}} — нет среди предыдущих шагов" % (sp["expert"], mo.group(1)))
        seen.add(sp["save_as"])

    # --- автоустановка моделей, которые реально выбраны в шагах (штатно: ollama -> видно в Models/Мои) ---
    installed = []
    need_models = set()
    for s in steps:
        mv = (s.get("params") or {}).get("model")
        if mv and not str(mv).startswith("{{"):
            need_models.add(mv)
    _OK = ("success", "already", "ok", "done", "installed", "present")
    for mdl in sorted(need_models):
        try:
            r = _post("/api/expert/run", {"expert_name": "cap_localmodel_install",
                                          "params": {"model": mdl}, "global": True})
            out = r.get("result", r)
            if isinstance(out, str):
                try:
                    out = json.loads(out)
                except Exception:
                    out = {}
            installed.append({"model": mdl, "status": (out or {}).get("status", "?")})
        except Exception as ex:
            installed.append({"model": mdl, "status": "error", "err": str(ex)[:80]})
    # честный флаг готовности: все нужные модели встали (или уже были)
    install_ok = all(str(i.get("status", "")).lower() in _OK for i in installed) if installed else True
    for i in installed:
        if str(i.get("status", "")).lower() not in _OK:
            missing.append("локальная модель не установилась: %s (%s)" % (i.get("model"), i.get("status")))

    # --- flow в KV + карточка в витрине ---
    import hashlib
    slug = re.sub(r"[^a-z0-9]+", "-", str(plan.get("name") or "").lower()).strip("-")[:28]
    # C2 (кабинет композиции): reuse_flow_id = СТАБИЛЬНАЯ перезапись сохранённой композиции.
    # Иначе каждая чат-доводка рождала бы новый flow:<id> + новую карточку (мусор в витрине)
    # и рассинхрон с sched:<sid>.flow_id / builds[] сессии.
    if not _b(reuse_flow_id):
        flow_id = re.sub(r"[^A-Za-z0-9_-]", "", str(reuse_flow_id))[:64]
    else:
        flow_id = (slug + "-" if slug else "") + hashlib.md5(str(task).encode("utf-8")).hexdigest()[:8]
    flow = {"name": plan.get("name") or slug, "task": str(task),
            "steps": steps, "synthesis_prompt": plan.get("synthesis_prompt") or "Summarize the step results as a markdown brief with a table.",
            "deliver": "", "deliver_client": "default",
            "installed": installed, "missing": missing,   # C2: персистенция для вкладки «Состав» (раньше жили только в эфемерном ответе)
            "composed_at": datetime.now(timezone.utc).isoformat()}
    try:
        _post("/api/kv/set", {"key": "flow:" + flow_id, "value": json.dumps(flow, ensure_ascii=False),
                              "description": "composer flow"}, t=60)
    except Exception as ex:
        return json.dumps({"status": "error", "message": "не записал план: " + str(ex)[:120]}, ensure_ascii=False)

    card = {"id": "flow-" + flow_id, "name": (plan.get("emoji") or "⚙️") + " " + (plan.get("name") or slug),
            "type": "process", "description": plan.get("description") or str(task),
            "orchestrator": "wz_flow_run", "runParams": {"flow_id": flow_id},
            "composed": True, "installed": bool(install_ok),
            "components": [s["expert"] for s in steps]}
    try:
        try:
            cur = _post("/api/kv/get", {"key": "_mkt_automations", "global": True}, t=60).get("value")
        except Exception:
            cur = None  # каталога ещё нет (первая автоматизация на аккаунте) — создадим ниже
        cat2 = json.loads(cur) if cur else {"items": []}
        cat2["items"] = [it for it in cat2.get("items", []) if it.get("id") != card["id"]]
        cat2["items"].insert(0, card)
        _post("/api/kv/set", {"key": "_mkt_automations", "value": json.dumps(cat2, ensure_ascii=False),
                              "description": "automations catalog", "global": True}, t=60)
    except Exception as ex:
        return json.dumps({"status": "error", "message": "карточка не записана: " + str(ex)[:120], "flow_id": flow_id}, ensure_ascii=False)

    # json-строка, а не dict: витрина (JS) парсит JSON.parse — Python-repr ей не по зубам
    return json.dumps({"status": "success", "flow_id": flow_id, "card": card, "steps": steps,
                       "installed": installed, "installed_ok": bool(install_ok), "missing": missing,
                       "synthesis_prompt": flow["synthesis_prompt"][:300]}, ensure_ascii=False)
