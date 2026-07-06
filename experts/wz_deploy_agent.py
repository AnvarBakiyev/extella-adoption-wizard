# expert: wz_deploy_agent
# description: Эксперт wz_deploy_agent (Adoption Wizard).
# params: session_id, blueprint_path, api_token, agent_id, llm_api_key, llm_base_url, agent_model, agent_name, smoke_test, api_base

$extens("include.py")
include("import requests", ["extella-pip install requests"])

def wz_deploy_agent(
    session_id: str = "",
    blueprint_path: str = "",
    api_token: str = "",
    agent_id: str = "",
    llm_api_key: str = "",
    llm_base_url: str = "https://api.openai.com/v1",
    agent_model: str = "gpt-4o",
    agent_name: str = "",
    smoke_test: str = "yes",
    api_base: str = "https://api.extella.ai"
) -> dict:
    import json
    import requests
    from pathlib import Path
    from datetime import datetime, timezone

    def now():
        return datetime.now(timezone.utc).isoformat()

    if not api_token:
        # доктрина «секреты не путешествуют»: bridge-конфиг устройства
        cfg = Path.home() / "extella_wizard" / "app" / "config.json"
        try:
            if cfg.exists():
                api_token = json.loads(cfg.read_text(encoding="utf-8")).get("auth_token", "")
        except Exception:
            api_token = ""
    if not api_token:
        return {"status": "error", "message": "api_token is required (или bridge-конфиг устройства)"}
    # Два режима:
    #   create (по умолчанию, agent_id пуст) — создаёт нового агента BYOK; нужен llm_api_key.
    #   update (agent_id задан) — перепрошивает СУЩЕСТВУЮЩЕГО агента (канон: UI-копия на
    #   платформенной модели Qwen; галочки MCP ставятся руками по матрице AGENT_SPEC).
    mode = "update" if agent_id else "create"
    if mode == "create" and not llm_api_key:
        return {"status": "error", "message": "llm_api_key is required in create mode (BYOK); "
                                              "для UI-копии на платформенной модели передай agent_id (режим update)"}

    sess_dir = Path.home() / "extella_wizard" / "sessions"
    session = None
    sp = None
    if session_id:
        sp = sess_dir / (session_id + ".json")
        if sp.exists():
            session = json.loads(sp.read_text(encoding="utf-8"))
    if not blueprint_path and session:
        blueprint_path = session.get("blueprint_path", "")
    bpp = Path(blueprint_path)
    if not blueprint_path or not bpp.exists():
        return {"status": "error", "message": "blueprint not found - provide blueprint_path or a session with a generated blueprint"}
    bdoc = json.loads(bpp.read_text(encoding="utf-8"))
    bp = bdoc.get("blueprint", bdoc)

    plan = {}
    if session and session.get("build_plan_path") and Path(session["build_plan_path"]).exists():
        pdoc = json.loads(Path(session["build_plan_path"]).read_text(encoding="utf-8"))
        plan = pdoc.get("plan", pdoc)

    prod = plan.get("production_agent") or {}
    orch = (plan.get("orchestrator") or {}).get("expert_name", "")
    process_name = bp.get("process_name", "Бизнес-процесс")
    if not agent_name:
        agent_name = prod.get("name") or (process_name + " | Агент процесса")

    # ── Deterministic instructions from blueprint (no extra LLM call) ──
    stages_txt = "\n".join(
        "%d. %s — %s (вход: %s; выход: %s)" % (
            i + 1, s.get("title", ""), s.get("business_description", ""),
            s.get("inputs", "-"), s.get("outputs", "-"))
        for i, s in enumerate(bp.get("stages") or []))
    gaps_txt = "\n".join("- " + str(g.get("title", "")) + ": " + str(g.get("proposal", ""))
                         for g in (bp.get("gaps") or [])) or "- нет"
    crit_txt = "\n".join("- " + str(c) for c in ((bp.get("sample_test_plan") or {}).get("success_criteria") or []))
    sut = bp.get("suitability") or {}

    instructions = """Ты — рабочий агент процесса «%s» на платформе Extella. Твоя работа: вести этот процесс изо дня в день и отвечать на вопросы о нём.

# Процесс
Цель: %s
Стадии:
%s

# Как запускать процесс
Оркестратор процесса: эксперт `%s` — запускай его инструментом run_expert ОБЯЗАТЕЛЬНО с параметром global:true. Параметры стадий вшиты дефолтами, ничего не выдумывай. Периодичность: %s. После прогона сообщай краткую сводку: что обработано, ключевые цифры, отклонения.

# Дисциплина инструментов
- За один ход — РОВНО ОДИН вызов инструмента. Никогда не вызывай несколько сразу.
- После вызова процитируй фактический результат (цифры, пути, статус) — дословно из ответа инструмента.
- Если инструмент вернул ошибку — покажи её текст как есть и остановись. Не изображай успех.
- Никогда не пиши текст, похожий на вызов инструмента, — либо реальный вызов, либо обычный ответ.

# Критерии качества (следи за ними в каждом прогоне)
%s

# Жёсткие границы
- Режим доступа к системам клиента: ТОЛЬКО ЧТЕНИЕ. Никогда не выполняй и не создавай операции записи/изменения/удаления во внешних системах.
- Письма/сообщения вовне — ТОЛЬКО черновики-файлы; отправляет человек. Автоотправка запрещена до отдельного согласования ИТ/ИБ.
- Ты ЗАМОРОЖЕН (уровень F2): не создавай и не изменяй экспертов, концепты и правила. Твоя работа — запускать готовый процесс и отчитываться.
- Изменения процесса (пороги, стадии, поля) — не твоя зона: отвечай «изменения процесса вносит Строитель процессов, назовите ему сессию %s» и ничего не меняй сам.
- Не выдумывай цифры: сообщай только фактические результаты запусков экспертов; если запуск не удался — скажи прямо и покажи ошибку.
- Известные ограничения процесса (не обещай их сверх согласованного):
%s
- Самообслуживание: %s. Действия за пределами согласованного объёма — только после подтверждения владельца процесса.

# Стиль
Деловой русский язык, кратко, с цифрами. Собеседник — сотрудники клиента; внутренние термины платформы не используй.""" % (
        process_name, bp.get("goal", ""), stages_txt,
        orch or "(указывается при внедрении)",
        prod.get("schedule_hint", "по согласованному расписанию"),
        crit_txt or "- согласованы при внедрении",
        session_id or "(см. карточку пилота)",
        gaps_txt,
        "разрешено в объёме анализа/отчётности" if sut.get("self_serve_allowed") else "требует согласования ИТ/ИБ")

    headers = {"X-Auth-Token": api_token, "Content-Type": "application/json",
               "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}

    def xapi(ep, payload, timeout=300):
        r = requests.post(api_base.rstrip("/") + ep, headers=headers, json=payload, timeout=timeout)
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:300]}
        if r.status_code not in (200, 201):
            return {"status": "error", "http": r.status_code, "message": str(body)[:300]}
        return body

    if mode == "create":
        created = xapi("/api/agent/create", {
            "provider": "custom", "model": agent_model,
            "baseURL": llm_base_url, "apiKey": llm_api_key,
            "name": agent_name,
            "description": "Продовый агент процесса «" + process_name + "» (задеплоен Adoption Wizard). Роль: " +
                           str(prod.get("role_summary", "ведение процесса и отчётность"))[:180],
            "instructions": instructions,
            "conversation_starters": [
                "Запусти процесс и покажи сводку",
                "Каков статус последнего прогона?",
                "Объясни, как устроен процесс"]})
        agent_id = created.get("id")
        if not agent_id:
            return {"status": "error", "message": "agent create failed: " + str(created)[:300]}
    else:
        updated = xapi("/api/agent/update", {"agent_id": agent_id, "instructions": instructions})
        if updated.get("status") == "error":
            return {"status": "error", "message": "agent update failed: " + str(updated)[:300]}
        agent_name = updated.get("name", agent_name)

    smoke = None
    if str(smoke_test).strip().lower() not in ("0", "false", "no", "off"):
        run = xapi("/api/agent/run", {"agent_id": agent_id,
                                      "input": "Кто ты и за какой процесс отвечаешь? Ответь в двух предложениях.",
                                      "run_timeout": 90, "store": False})
        text = ""
        for item in run.get("output", []) or []:
            if item.get("type") == "message":
                for c in item.get("content", []) or []:
                    if c.get("type") == "output_text":
                        text += c.get("text", "")
        smoke = text[:300] if text else ("error: " + str(run)[:200])

    if session is not None and sp is not None:
        session["production_agent"] = {"agent_id": agent_id, "name": agent_name,
                                       "mode": mode, "created_at": now()}
        session.setdefault("log", []).append({"ts": now(), "event": "production agent %s: %s" % ("deployed" if mode == "create" else "reflashed (update mode)", agent_id)})
        session["updated_at"] = now()
        sp.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"status": "success", "mode": mode, "agent_id": agent_id, "agent_name": agent_name,
            "smoke_reply": smoke,
            "api_howto": ("24/7 вызов: POST " + api_base + "/api/agent/run с заголовком X-Auth-Token "
                          "(токен выпускается token_generate) и телом {agent_id: '" + agent_id +
                          "', input: '<сообщение>'}. Ответ OpenAI-совместимый (output[].content[].text)."),
            "orchestrator": orch}
