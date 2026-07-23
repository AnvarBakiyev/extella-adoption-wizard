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

    SYSTEM = f"""Ты — технический планировщик Universal Process Runtime платформы Extella. По Process Blueprint составь исполняемый версионируемый граф: какие шаги выполнить, в каком порядке, каким режимом, с какими полномочиями и как доказать каждый результат.

ЖЁСТКИЕ ПРАВИЛА:
1. Имена экспертов: snake_case, ОБЯЗАТЕЛЬНО начинаются с "{namespace}_".
2. Библиотека — инвентарь, не граница. implementation_mode="reuse" и action="reuse|parameterize"
допустимы ТОЛЬКО если reuse_of — точное имя из КАНДИДАТОВ. Если совпадения нет, шаг остаётся в плане:
generate — создать CSPL/fython-эксперта; llm_worker — Qwen сама исполняет смысловой шаг; acquire — нужна
внешняя модель/репозиторий/инструмент; human — нужен доступ/решение/опасное действие человека; delegate —
ограниченный дочерний граф. Неизвестная задача никогда не превращается в пустой план.
3. cspl строго из: fython (обычная логика, по умолчанию) | nohup (оркестраторы, длинные фоновые) | parallel_task (параллельные воркеры для массовой/многоисточниковой обработки) | shell_runner (CLI-обёртки).
4. acceptance_test каждой задачи должен быть прогоняем БЕЗ доступа к системам клиента: на синтетике или файле-образце; example_params — конкретные значения. Если задача требует клиентского доступа (например, живая 1С) — тест на мок-данных + отметь это в risks.
5. Каждый эксперт: клиентская специфика (адреса баз, колонки, пороги) — параметрами, не в теле. Никакой записи во внешние системы, если blueprint явно не разрешил.
6. Порядок: depends_on по зависимостям данных; независимые задачи не связывай. Первым в human_gates всегда «план утверждён владельцем», последним — «деплой продового агента подтверждён».
7. Оркестратор укажи ТОЛЬКО в отдельном объекте orchestrator после tasks. НЕ добавляй его как задачу:
Universal Process Runtime сам скомпилирует оркестратор из графа бизнес-шагов, иначе один процесс исполнится дважды.
8. Тексты полей purpose/описания — {language}. Верни ТОЛЬКО JSON.
9. В tasks включай только бизнес-стадии обработки данных. НЕ создавай оркестратор, watcher папки, daemon,
   cron, launchd/systemd, autostart, установщики и отдельные задачи расписания/доставки: источник, триггер,
   расписание и режим 24/7 Extella настраивает после сборки через кабинет процесса.
10. Максимум 40 статических tasks. Большие участки оформляй одним delegate-шагом с subgraph_goal.
11. Для КАЖДОГО шага явно заполни permissions: read/create/move/modify/delete/install/send/external_write.
Опасные действия не скрывай внутри generate: они будут остановлены human approval gate.
12. Acceptance раздели на deterministic_checks (файлы, хэши, schema, counts, side-effect journal) и
semantic_criteria (только смысл, который проверит независимая Qwen). Transport completed не является успехом.
13. Синтетика из acceptance_test — только изолированный тест реализации. Никогда не переноси придуманные
идентификаторы, строки, количества или ожидаемые результаты синтетики в acceptance/semantic_criteria реального
прогона. Там допустимы только общие инварианты либо конкретные факты, уже явно присутствующие в Blueprint или
ответах владельца. Отсутствующий в исходной фактуре пример не является обязательной записью пользовательских данных.

ФОРМАТ (строго):
{{
  "process_name": "...", "namespace": "{namespace}",
  "tasks": [
    {{"id": "t1", "stage_id": "s1", "expert_name": "{namespace}_...", "action": "build|reuse|parameterize",
      "implementation_mode": "reuse|generate|llm_worker|acquire|human|delegate",
      "reuse_of": null, "capability_ref": null, "subgraph_goal": null, "purpose": "что делает и зачем",
      "cspl": "fython", "params_spec": [{{"name": "...", "type": "str", "purpose": "..."}}],
      "acceptance_test": {{"description": "...", "example_params": {{}}, "expected": "что считается успехом"}},
      "input_contract": {{"artifacts":[],"data_schema":{{}},"required":true}},
      "output_contract": {{"artifacts":[],"data_schema":{{}},"postconditions":[]}},
      "permissions": {{"read":[],"create":[],"move":[],"modify":[],"delete":[],"install":[],"send":[],"external_write":[]}},
      "acceptance": {{"deterministic_checks":[],"semantic_criteria":[],"required_artifacts":[],"minimum_confidence":0.7}},
      "retry_policy": {{"max_attempts":4,"repair_on":["expert_error","contract_violation","acceptance_failed"],"human_on":["permission_required","ambiguous_owner_decision"]}},
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
    IMPLEMENTATION_MODES = {"reuse", "generate", "llm_worker", "acquire", "human", "delegate"}
    PERMISSIONS = ("read", "create", "move", "modify", "delete", "install", "send", "external_write")
    name_re = re.compile("^" + namespace + r"_[a-z0-9_]+$")
    # Record-level examples are useful inside acceptance_test, but they must never silently become
    # facts required from the user's real files.  Qwen sometimes writes a synthetic D-404 next to
    # owner-provided A-101/B-202/C-303 and then the independent judge correctly rejects the real
    # output for not containing the invented row.  Keep only record identifiers grounded in the
    # authoritative interview/Blueprint; replace the whole mixed claim with a source-grounded
    # invariant so the synthetic fixture stays an implementation test, not production truth.
    record_re = re.compile(
        r"(?<![\w])(?:[A-Za-zА-Яа-яЁё]{1,16}[-_/]\d[A-Za-zА-Яа-яЁё0-9._/-]{0,48})(?![\w])")
    authoritative_text = json.dumps({
        "answers": session.get("answers") or {},
        "questionnaire_task": session.get("questionnaire_task") or "",
        "blueprint": bp,
    }, ensure_ascii=False, default=str).casefold()
    authoritative_records = {x.casefold() for x in record_re.findall(authoritative_text)}
    grounded_semantic = (
        "Результат соответствует фактическим данным текущих входов и заявленной бизнес-логике шага"
        if str(language or "ru").lower().startswith("ru") else
        "The result matches the actual current inputs and the stated business logic of the step")

    def sanitize_semantic(criteria, task_id):
        safe = []
        replaced = False
        for raw in criteria or []:
            text = str(raw).strip()
            if not text:
                continue
            found = {x.casefold() for x in record_re.findall(text)}
            unsupported = sorted(found - authoritative_records)
            if unsupported:
                warnings.append("task " + str(task_id or "?") +
                                ": synthetic record claims removed from semantic acceptance: " +
                                ", ".join(unsupported[:5]))
                replaced = True
                continue
            safe.append(text)
        if replaced and grounded_semantic not in safe:
            safe.insert(0, grounded_semantic)
        return safe

    task_ids = set()
    plan["tasks"] = [t for t in plan["tasks"][:40] if isinstance(t, dict)]
    orchestrator_name = str((plan.get("orchestrator") or {}).get("expert_name") or "")
    business_tasks = []
    for task in plan["tasks"]:
        blob = " ".join(str(task.get(k) or "") for k in ("title", "purpose", "id", "expert_name")).casefold()
        runtime_only = (bool(orchestrator_name) and str(task.get("expert_name") or "") == orchestrator_name)
        runtime_only = runtime_only or (len(plan["tasks"]) > 1 and any(marker in blob for marker in (
            "оркестратор процесса", "process orchestrator", "последовательно запускает стадии")))
        if runtime_only:
            warnings.append("task " + str(task.get("id") or "?") +
                            ": runtime orchestrator removed; UPC compiles it from the graph")
        else:
            business_tasks.append(task)
    plan["tasks"] = business_tasks
    if not plan["tasks"]:
        return {"status": "error", "message": "Build Plan has no valid tasks"}
    for index, t in enumerate(plan["tasks"], 1):
        if not t.get("id"):
            t["id"] = "t" + str(index)
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
        mode = str(t.get("implementation_mode") or "").lower()
        if mode not in IMPLEMENTATION_MODES:
            mode = "reuse" if t.get("action") in ("reuse", "parameterize") and t.get("reuse_of") else "generate"
        if mode == "reuse" and t.get("reuse_of") not in all_candidate_names:
            warnings.append("task " + str(t.get("id")) + ": implementation reuse has no exact candidate -> generate")
            mode = "generate"
            t["action"] = "build"
            t["reuse_of"] = None
        t["implementation_mode"] = mode
        t["capability_ref"] = t.get("reuse_of") if mode == "reuse" else None
        perms = t.get("permissions") if isinstance(t.get("permissions"), dict) else {}
        t["permissions"] = {kind: ([str(x) for x in perms.get(kind) if str(x)]
                                   if isinstance(perms.get(kind), list) else []) for kind in PERMISSIONS}
        acceptance = t.get("acceptance") if isinstance(t.get("acceptance"), dict) else {}
        t["acceptance"] = {
            "deterministic_checks": [str(x) for x in (acceptance.get("deterministic_checks") or []) if str(x)],
            "semantic_criteria": sanitize_semantic(
                acceptance.get("semantic_criteria") or [], t.get("id")),
            "required_artifacts": [str(x) for x in (acceptance.get("required_artifacts") or []) if str(x)],
            "minimum_confidence": acceptance.get("minimum_confidence", 0.7),
        }
        retry = t.get("retry_policy") if isinstance(t.get("retry_policy"), dict) else {}
        try:
            max_attempts = max(1, min(int(retry.get("max_attempts") or 4), 10))
        except Exception:
            max_attempts = 4
        t["retry_policy"] = {
            "max_attempts": max_attempts,
            "repair_on": retry.get("repair_on") or ["expert_error", "contract_violation", "acceptance_failed"],
            "human_on": retry.get("human_on") or ["permission_required", "ambiguous_owner_decision"],
        }
        task_ids.add(t.get("id"))
    for t in plan["tasks"]:
        bad = [d for d in (t.get("depends_on") or []) if d not in task_ids]
        if bad:
            warnings.append("task " + str(t.get("id")) + ": unknown depends_on " + str(bad) + " removed")
            t["depends_on"] = [d for d in t["depends_on"] if d in task_ids]
    if isinstance(plan.get("orchestrator"), dict):
        plan["orchestrator"]["task_order"] = [
            str(x) for x in (plan["orchestrator"].get("task_order") or []) if str(x) in task_ids]

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
