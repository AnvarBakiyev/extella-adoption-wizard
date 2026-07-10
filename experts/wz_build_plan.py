$extens("include.py")
include("import requests", ["extella-pip install requests"])

def wz_build_plan(
    session_id: str = "",
    session_path: str = "",
    blueprint_path: str = "",
    namespace: str = "",
    api_token: str = "",
    api_key: str = "",
    base_url: str = "https://api.openai.com/v1",
    model: str = "gpt-4o",
    language: str = "ru",
    output_path: str = "",
    agent_id: str = "",
    api_base: str = "https://api.extella.ai"
) -> dict:
    import json, re
    import requests
    from pathlib import Path
    from datetime import datetime, timezone

    def now():
        return datetime.now(timezone.utc).isoformat()

    # -- Validate & resolve -------------------------------------------
    if not namespace or not re.match(r"^[a-z][a-z0-9]{1,11}$", namespace):
        return {"status": "error", "message": "namespace is required: short snake prefix for process experts, e.g. 'dz' (lowercase, 2-12 chars)"}
    if not api_token:
        return {"status": "error", "message": "api_token is required"}
    # keyless: если мост не передал agent_id — читаем СВОЙ agent_id клиента из локального конфига устройства
    # (его собственный Qwen-Визард; чужой агент → 'Agent does not belong to this user'; Claude-дефолт запрещён)
    if not api_key and not agent_id:
        try:
            _cfg = json.loads((Path.home() / "extella_wizard" / "app" / "config.json").read_text(encoding="utf-8"))
            agent_id = _cfg.get("llm_agent_id") or _cfg.get("agent_id", "")
        except Exception:
            agent_id = ""
        if not agent_id:
            return {"status": "error", "message": "нет Qwen-агента для keyless: передайте agent_id или настройте config.agent_id (свой Qwen-Визард)"}
    if not session_path:
        if not session_id:
            return {"status": "error", "message": "session_id or session_path is required"}
        session_path = str(Path.home() / "extella_wizard" / "sessions" / (session_id + ".json"))
    sp = Path(session_path)
    if not sp.exists():
        return {"status": "error", "message": "session not found: " + str(sp)}
    session = json.loads(sp.read_text(encoding="utf-8"))
    if not blueprint_path:
        blueprint_path = session.get("blueprint_path", "")
    bpp = Path(blueprint_path)
    if not blueprint_path or not bpp.exists():
        return {"status": "error", "message": "blueprint not found - generate it first (wz_generate_blueprint)"}
    bdoc = json.loads(bpp.read_text(encoding="utf-8"))
    bp = bdoc.get("blueprint", bdoc)
    stages = bp.get("stages") or []
    if not stages:
        return {"status": "error", "message": "blueprint has no stages"}

    # -- Reuse candidates per stage via library semantic search --------
    headers = {"X-Auth-Token": api_token, "Content-Type": "application/json",
               "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}
    candidates = {}
    all_candidate_names = set()
    for st in stages:
        q = (str(st.get("title", "")) + ". " + str(st.get("business_description", "")))[:300]
        found = []
        try:
            r = requests.post(api_base.rstrip("/") + "/api/blocks/search",
                              headers=headers, json={"query": q, "limit": 5}, timeout=60)
            if r.status_code == 200:
                body = r.json()
                items = body.get("matches") or body.get("results") or []
                for item in items[:5]:
                    nm = item.get("name") or item.get("expert_name")
                    ds = str(item.get("description") or item.get("expert_description") or "")[:220]
                    if nm:
                        found.append({"name": nm, "description": ds})
                        all_candidate_names.add(nm)
        except Exception:
            pass
        candidates[st.get("id", "?")] = found

    # -- LLM: build plan ------------------------------------------------
    bp_payload = json.dumps({
        "process_name": bp.get("process_name"), "goal": bp.get("goal"),
        "archetype": bp.get("archetype"), "stages": stages,
        "gaps": bp.get("gaps"), "sample_test_plan": bp.get("sample_test_plan"),
        "suitability": bp.get("suitability")}, ensure_ascii=False)[:14000]
    cand_payload = json.dumps(candidates, ensure_ascii=False)[:12000]

    SYSTEM = f"""Ты — технический планировщик стройки процессов на платформе Extella. По Process Blueprint составь Build Plan: какие исполняемые эксперты создать или переиспользовать, в каком порядке, и как проверить каждый.

ЖЁСТКИЕ ПРАВИЛА:
1. Имена экспертов: snake_case, ОБЯЗАТЕЛЬНО начинаются с "{namespace}_".
2. Переиспользование: action="reuse" или "parameterize" допустимы ТОЛЬКО если reuse_of — точное имя из переданных КАНДИДАТОВ БИБЛИОТЕКИ; иначе action="build", reuse_of=null. Активно переиспользуй: библиотека проверена в бою.
3. cspl строго из: fython (обычная логика, по умолчанию) | nohup (оркестраторы, длинные фоновые) | parallel_task (параллельные воркеры для массовой/многоисточниковой обработки) | shell_runner (CLI-обёртки).
4. acceptance_test каждой задачи должен быть прогоняем БЕЗ доступа к системам клиента: на синтетике или файле-образце; example_params — конкретные значения. Если задача требует клиентского доступа (например, живая 1С) — тест на мок-данных + отметь это в risks.
5. Каждый эксперт: клиентская специфика (адреса баз, колонки, пороги) — параметрами, не в теле. Никакой записи во внешние системы, если blueprint явно не разрешил.
6. Порядок: depends_on по зависимостям данных; независимые задачи не связывай. Первым в human_gates всегда «план утверждён владельцем», последним — «деплой продового агента подтверждён».
7. Оркестратор — отдельная задача с cspl=nohup или fython (по образцу пайплайн-оркестраторов: стадии через REST, манифест, deferred→ожидание артефакта).
8. Тексты полей purpose/описания — {language}. Верни ТОЛЬКО JSON.

ФОРМАТ (строго):
{{
  "process_name": "...", "namespace": "{namespace}",
  "tasks": [
    {{"id": "t1", "stage_id": "s1", "expert_name": "{namespace}_...", "action": "build|reuse|parameterize",
      "reuse_of": null, "purpose": "что делает и зачем",
      "cspl": "fython", "params_spec": [{{"name": "...", "type": "str", "purpose": "..."}}],
      "acceptance_test": {{"description": "...", "example_params": {{}}, "expected": "что считается успехом"}},
      "depends_on": []}}
  ],
  "orchestrator": {{"expert_name": "{namespace}_run_pipeline", "task_order": ["t1", "..."]}},
  "production_agent": {{"name": "...", "role_summary": "чем занят агент 24/7", "schedule_hint": "какое расписание нужно"}},
  "human_gates": ["..."], "risks": ["..."]
}}"""

    user_msg = f"""PROCESS BLUEPRINT (JSON):
{bp_payload}

КАНДИДАТЫ БИБЛИОТЕКИ по стадиям (JSON, stage_id -> список):
{cand_payload}

Составь Build Plan по правилам."""

    # LLM: OpenAI если есть api_key, иначе платформенная Qwen через агента (клиенту ключ не нужен)
    content = ""
    if api_key:
        try:
            resp = requests.post(base_url.rstrip("/") + "/chat/completions",
                                 headers={"Authorization": "Bearer " + api_key,
                                          "Content-Type": "application/json"},
                                 json={"model": model,
                                       "messages": [{"role": "system", "content": SYSTEM},
                                                    {"role": "user", "content": user_msg}],
                                       "temperature": 0,
                                       "response_format": {"type": "json_object"},
                                       "max_tokens": 4000},
                                 timeout=180)
        except Exception as e:
            return {"status": "error", "message": "LLM request failed: " + str(e)[:200]}
        if resp.status_code != 200:
            return {"status": "error", "message": "LLM API error " + str(resp.status_code) + ": " + resp.text[:200]}
        content = resp.json()["choices"][0]["message"]["content"]
    else:
        try:
            rr = requests.post(api_base.rstrip("/") + "/api/agent/run",
                headers={"X-Auth-Token": api_token, "Content-Type": "application/json",
                         "X-Profile-Id": "default", "X-Agent-Id": agent_id},
                json={"agent_id": agent_id,
                      "input": SYSTEM + "\n\n" + user_msg + "\n\nВерни ТОЛЬКО валидный JSON-объект (Build Plan): "
                               "без markdown, без пояснений, без обучающих подсказок. Первый символ — '{', последний — '}'.",
                      # большой план обрезался на дефолтном лимите вывода → задаём щедрый потолок
                      "run_timeout": 600, "store": False, "max_output_tokens": 16000},
                timeout=660).json()
        except Exception as e:
            return {"status": "error", "message": "platform LLM request failed: " + str(e)[:200]}
        content = "".join(c.get("text", "") for it in (rr.get("output") or [])
                          if it.get("type") == "message"
                          for c in (it.get("content") or []) if c.get("type") == "output_text")
        if not content:
            return {"status": "error", "message": "platform LLM empty output: " + str(rr)[:200]}
    try:
        _m = re.search(r"\{.*\}", content, re.S)   # Qwen может добавить текст — берём JSON-объект
        plan = json.loads(_m.group(0) if _m else content)
        assert isinstance(plan.get("tasks"), list) and plan["tasks"]
    except Exception as e:
        return {"status": "error", "message": "Failed to parse LLM JSON: " + str(e)[:200]}

    # -- Guardrails ------------------------------------------------------
    warnings = []
    ALLOWED_CSPL = {"fython", "nohup", "parallel_task", "shell_runner"}
    name_re = re.compile("^" + namespace + r"_[a-z0-9_]+$")
    task_ids = set()
    for t in plan["tasks"]:
        nm = str(t.get("expert_name", ""))
        if not name_re.match(nm):
            fixed = namespace + "_" + re.sub(r"[^a-z0-9_]", "_", nm.lower()).strip("_")
            warnings.append("task " + str(t.get("id")) + ": expert_name '" + nm + "' -> '" + fixed + "'")
            t["expert_name"] = fixed
        if t.get("cspl") not in ALLOWED_CSPL:
            warnings.append("task " + str(t.get("id")) + ": cspl '" + str(t.get("cspl")) + "' -> fython")
            t["cspl"] = "fython"
        if t.get("action") in ("reuse", "parameterize"):
            if t.get("reuse_of") not in all_candidate_names:
                warnings.append("task " + str(t.get("id")) + ": reuse_of '" + str(t.get("reuse_of")) + "' not in candidates -> action=build")
                t["action"] = "build"
                t["reuse_of"] = None
        task_ids.add(t.get("id"))
    for t in plan["tasks"]:
        bad = [d for d in (t.get("depends_on") or []) if d not in task_ids]
        if bad:
            warnings.append("task " + str(t.get("id")) + ": unknown depends_on " + str(bad) + " removed")
            t["depends_on"] = [d for d in t["depends_on"] if d in task_ids]

    # -- Write + attach --------------------------------------------------
    out = Path(output_path) if output_path else sp.parent / (session.get("session_id", sp.stem) + "_build_plan.json")
    out.write_text(json.dumps({
        "generated_at": now(), "model_version": model,
        "session_id": session.get("session_id", ""),
        "blueprint_path": str(bpp),
        "plan": plan, "warnings": warnings,
        "reuse_candidates": candidates
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    session["build_plan_path"] = str(out)
    session.setdefault("log", []).append({"ts": now(), "event": "build plan generated: " + str(out)})
    session["updated_at"] = now()
    sp.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")

    acts = {}
    for t in plan["tasks"]:
        acts[t.get("action", "?")] = acts.get(t.get("action", "?"), 0) + 1
    return {"status": "success", "build_plan_path": str(out),
            "tasks_count": len(plan["tasks"]), "actions": acts,
            "orchestrator": (plan.get("orchestrator") or {}).get("expert_name"),
            "production_agent": (plan.get("production_agent") or {}).get("name"),
            "warnings": warnings[:5]}
