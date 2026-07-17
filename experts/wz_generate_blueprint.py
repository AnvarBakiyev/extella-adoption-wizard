$extens("include.py")
include("import requests", ["extella-pip install requests"])

def wz_generate_blueprint(
    session_path: str = "",
    session_id: str = "",
    base_dir: str = "",
    catalog_path: str = "",
    api_key: str = "",
    base_url: str = "https://api.openai.com/v1",
    model: str = "gpt-4o",
    language: str = "ru",
    output_path: str = "",
    api_token: str = "",
    agent_id: str = "",
    api_base: str = "https://api.extella.ai"
) -> dict:
    import json
    import requests
    from pathlib import Path
    from datetime import datetime, timezone

    def now():
        return datetime.now(timezone.utc).isoformat()

    # -- Resolve inputs ------------------------------------------------
    if not api_key and not api_token:
        return {"status": "error", "message": "нужен api_key (OpenAI) ИЛИ api_token (платформенная модель Qwen)"}
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
            return {"status": "error", "message": "session_path or session_id is required"}
        root = Path(base_dir) if base_dir else Path.home() / "extella_wizard" / "sessions"
        session_path = str(root / (session_id + ".json"))
    sp = Path(session_path)
    if not sp.exists():
        return {"status": "error", "message": "session file not found: " + str(sp)}
    if catalog_path:
        cp = Path(catalog_path)
    else:
        cat_dir = Path.home() / "extella_wizard" / "catalog"
        cp = cat_dir / "catalog.json"
        if not cp.exists():
            cp = cat_dir / "catalog_v1.json"
    if not cp.exists():
        return {"status": "error", "message": "catalog file not found: " + str(cp)}

    session = json.loads(sp.read_text(encoding="utf-8"))
    catalog = json.loads(cp.read_text(encoding="utf-8"))

    answers = session.get("answers", {})
    if not answers:
        return {"status": "error", "message": "session has no answers yet - run the interview first"}
    open_comments = [c for c in session.get("comments", []) if not c.get("resolved")]

    # -- Allowed vocabulary from catalog -------------------------------
    cap_ids = set()
    asset_names = set()
    for cap in catalog.get("capabilities", []):
        cap_ids.add(cap.get("id"))
        for a in cap.get("assets", []):
            asset_names.add(a)
    pack_ids = set(p.get("id") for p in catalog.get("packs", []))
    archetype_ids = set(a.get("id") for a in catalog.get("process_archetypes", []))
    # Базы знаний (кодексы/регламенты) для knowledge_grounding — передаём в промпт явно
    knowledge_packs = catalog.get("knowledge_packs", [])
    kpack_ids = set(p.get("id") for p in knowledge_packs)
    kpacks_payload = json.dumps(knowledge_packs, ensure_ascii=False)

    answers_payload = json.dumps(
        {qid: {"question": a.get("question", ""), "answer": a.get("answer", "")}
         for qid, a in answers.items()}, ensure_ascii=False)
    comments_payload = json.dumps(
        [{"block_ref": c.get("block_ref"), "author": c.get("author"), "text": c.get("text")}
         for c in open_comments], ensure_ascii=False) if open_comments else "[]"
    catalog_payload = json.dumps(catalog, ensure_ascii=False)
    if len(catalog_payload) > 30000:
        catalog_payload = catalog_payload[:30000]

    rubric = catalog.get("suitability_rubric", {})

    SYSTEM = f"""Ты — архитектор решений платформы Extella. По ответам бизнес-интервью составь Process Blueprint — бизнес-план внедрения ИИ-процесса.

ЖЁСТКИЕ ПРАВИЛА:
1. Используй ТОЛЬКО возможности из переданного КАТАЛОГА: в capability_ids — только id из catalog.capabilities, в asset_names — только имена из assets каталога, в pack_id — только id из catalog.packs или null, в archetype.id — только id из catalog.process_archetypes или null.
2. Всё, чего в каталоге нет, но что нужно клиенту, — оформляй как элемент gaps (честный разрыв с предложением, как закрыть), а НЕ как стадию с выдуманными компонентами. И наоборот: ПЕРЕД тем как записать потребность в gaps, проверь каталог — если возможность там есть (например, «еженедельно» = возможность scheduling, «уведомить» = integrations), это СТАДИЯ или её часть, а не разрыв.
2в. Если клиент назвал периодичность процесса (ежедневно/еженедельно/ежемесячно/к закрытию периода) — в blueprint ОБЯЗАНА быть финальная стадия «Регулярный запуск» с возможностью scheduling, где явно указана периодичность из ответов.
2б. Активно ищи СКРЫТЫЕ технические разрывы в ответах клиента: сканы/фотографии документов → нужно распознавание (OCR); звонки/аудио/видео → нужна расшифровка речи; данные только в закрытой системе → нужен доступ/выгрузка. Такие вещи честно клади в gaps, даже если клиент о них не спросил.
2г. Если разрыв закрывается известным расширением из catalog.delivery_extensions — укажи в элементе gap поле extension_id (точный id из каталога) и в proposal перескажи его effort_hint; если подходящего расширения нет — extension_id: null.
2а. Сначала подбери ближайший АРХЕТИП процесса из catalog.process_archetypes (типовую форму) и строй стадии по его capability_flow, адаптируя под ответы клиента; если ни один архетип не подходит — archetype.id = null и собирай стадии из возможностей напрямую. Процесс может относиться к ЛЮБОМУ департаменту (финансы, HR, юристы, закупки, операции, маркетинг) — не своди всё к контакт-центру.
2д. БАЗА ЗНАНИЙ: если процесс опирается на закон/кодекс/регламент/стандарт/политику (ответ knowledge_base не пуст ИЛИ по смыслу нужны правовые нормы — кадровые документы, договоры, налоги и т.п.) — ОБЯЗАТЕЛЬНО добавь стадию с capability_ids=["knowledge_grounding"] (asset_names=["kp_ask"]), которая находит релевантные статьи в базе и подставляет их в работу (агент опирается на актуальный документ, а не память модели). Выбери подходящую базу из ПЕРЕДАННОГО СПИСКА knowledge_packs по домену (HR→trud_rk, договоры→grazhd_rk, налоги→nalog_rk и т.д.) и запиши её id в поле knowledge_pack.pack_id. Если нужной базы в списке нет — knowledge_pack.pack_id=null и добавь элемент в gaps «нужна база знаний: …». НЕ вшивай статьи закона в другие стадии статикой — только через стадию knowledge_grounding.
3. Суитабилити считай по рубрике каталога: self_serve_allowed=true только для процессов класса {json.dumps(rubric.get("self_serve_allowed", []), ensure_ascii=False)}; процессы с записью во внешние системы или действиями от имени компании — self_serve_allowed=false и risk_level минимум medium.
4. Не выдумывай числа и факты о клиенте: опирайся только на ответы интервью. Если данных мало — пиши меньше стадий и больше open_questions.
5. Учитывай открытые комментарии команды (переданы отдельно) — это уточнения к ответам.
6. Тексты пиши деловым языком ({language}), без внутренних терминов платформы (CSPL, Listener и т.п.) в полях title/business_description/goal.
7. Верни ТОЛЬКО JSON без пояснений.

ФОРМАТ (строго):
{{
  "process_name": "короткое имя процесса",
  "goal": "1-2 предложения: что получит бизнес",
  "archetype": {{"id": "id архетипа из каталога или null", "adaptation": "чем процесс клиента отличается от типовой формы"}},
  "suitability": {{"score": 0-100, "risk_level": "low|medium|high", "self_serve_allowed": true/false, "rationale": "почему такой скор"}},
  "stages": [
    {{"id": "s1", "title": "...", "business_description": "что происходит, бизнес-языком",
      "capability_ids": ["id из каталога"], "asset_names": ["имена из каталога"],
      "inputs": "что на входе", "outputs": "что на выходе"}}
  ],
  "pack_recommendation": {{"pack_id": "id из каталога или null", "fit": "почему подходит / чего не хватает", "adaptation_needed": ["что настроить под клиента"]}},
  "knowledge_pack": {{"pack_id": "id из knowledge_packs или null", "why": "почему эта база нужна процессу"}},
  "gaps": [{{"title": "...", "description": "чего нет в каталоге", "proposal": "как закрыть (разработка/поставка/ручной шаг)", "extension_id": "id из catalog.delivery_extensions или null"}}],
  "sample_test_plan": {{"data_needed": "какие данные нужны для теста", "steps": ["шаг 1", "..."], "success_criteria": ["критерий 1", "..."]}},
  "open_questions": ["вопрос клиенту 1", "..."]
}}"""

    user_msg = f"""ОТВЕТЫ ИНТЕРВЬЮ (JSON):
{answers_payload}

ОТКРЫТЫЕ КОММЕНТАРИИ КОМАНДЫ (JSON):
{comments_payload}

КАТАЛОГ ВОЗМОЖНОСТЕЙ EXTELLA (JSON):
{catalog_payload}

ДОСТУПНЫЕ БАЗЫ ЗНАНИЙ (knowledge_packs — для стадии knowledge_grounding, выбери по домену):
{kpacks_payload}

Составь Process Blueprint по правилам."""

    # LLM: если есть api_key — OpenAI (dev); иначе — платформенная модель Qwen через агента (клиенту ключ НЕ нужен)
    content = ""
    if api_key:
        try:
            resp = requests.post(
                base_url.rstrip("/") + "/chat/completions",
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
        # платформенная модель (Qwen) через /api/agent/run с run_timeout — синхронно, без внешнего ключа
        try:
            rr = requests.post(
                api_base.rstrip("/") + "/api/agent/run",
                headers={"X-Auth-Token": api_token, "Content-Type": "application/json",
                         "X-Profile-Id": "default", "X-Agent-Id": agent_id},
                json={"agent_id": agent_id,
                      "input": SYSTEM + "\n\n" + user_msg +
                               "\n\nВерни СТРОГО валидный JSON-объект (Process Blueprint) без markdown и пояснений.",
                      "run_timeout": 240, "store": False},
                timeout=300).json()
        except Exception as e:
            return {"status": "error", "message": "platform LLM request failed: " + str(e)[:200]}
        content = "".join(c.get("text", "") for it in (rr.get("output") or [])
                          if it.get("type") == "message"
                          for c in (it.get("content") or []) if c.get("type") == "output_text")
        if not content:
            return {"status": "error", "message": "platform LLM empty output: " + str(rr)[:200]}

    try:
        import re as _re
        _m = _re.search(r"\{.*\}", content, _re.S)   # Qwen иногда добавляет текст — берём JSON-объект
        bp = json.loads(_m.group(0) if _m else content)
        _stages = bp.get("stages")
        assert isinstance(_stages, list), "stages не список"
        # #15: отбросить не-dict/безымянные стадии — иначе guardrail ниже падает на st.get() (AttributeError вне try)
        _stages = [st for st in _stages if isinstance(st, dict) and (st.get("title") or st.get("id"))]
        # #14: пустой набор стадий — не валидный процесс; честный отказ вместо «пустой» сборки
        assert _stages, "blueprint без валидных стадий"
        bp["stages"] = _stages
    except Exception as e:
        return {"status": "error", "message": "Не удалось разобрать план от модели: " + str(e)[:200]}

    # -- Catalog guardrail --------------------------------------------
    warnings = []
    gaps = bp.get("gaps") or []
    for st in bp["stages"]:
        bad_caps = [c for c in (st.get("capability_ids") or []) if c not in cap_ids]
        bad_assets = [a for a in (st.get("asset_names") or []) if a not in asset_names]
        if bad_caps or bad_assets:
            st["capability_ids"] = [c for c in (st.get("capability_ids") or []) if c in cap_ids]
            st["asset_names"] = [a for a in (st.get("asset_names") or []) if a in asset_names]
            removed = bad_caps + bad_assets
            warnings.append("stage '" + str(st.get("title", st.get("id", "?")))[:60] +
                            "': unknown components stripped: " + ", ".join(str(x) for x in removed))
            gaps.append({"title": "Компоненты вне каталога в стадии «" + str(st.get("title", "?"))[:60] + "»",
                         "description": "Модель предложила компоненты, которых нет в каталоге: " + ", ".join(str(x) for x in removed),
                         "proposal": "Проверить потребность и при необходимости запланировать разработку"})
    bp["gaps"] = gaps
    # #13: после срезки неизвестных компонентов — если НИ одна стадия не имеет исполнимых компонентов
    # (ни capability_ids, ни asset_names), процесс собрать не из чего → честный отказ вместо «пустой» сборки
    if not any((st.get("capability_ids") or st.get("asset_names")) for st in bp["stages"]):
        return {"status": "error", "gaps": gaps, "warnings": warnings,
                "message": "план без исполнимых компонентов: предложенные инструменты вне каталога. "
                           "Уточните задачу в интервью или запросите разработку недостающих компонентов."}

    pr = bp.get("pack_recommendation") or {}
    if pr.get("pack_id") and pr["pack_id"] not in pack_ids:
        warnings.append("pack_recommendation: unknown pack_id '" + str(pr["pack_id"]) + "' -> null")
        pr["pack_id"] = None
        bp["pack_recommendation"] = pr

    ar = bp.get("archetype") or {}
    if ar.get("id") and ar["id"] not in archetype_ids:
        warnings.append("archetype: unknown id '" + str(ar["id"]) + "' -> null")
        ar["id"] = None
        bp["archetype"] = ar

    # knowledge_pack: id должен быть из knowledge_packs, иначе null + gap
    kp = bp.get("knowledge_pack") or {}
    if kp.get("pack_id") and kp["pack_id"] not in kpack_ids:
        warnings.append("knowledge_pack: unknown pack_id '" + str(kp["pack_id"]) + "' -> null")
        bp.setdefault("gaps", []).append({
            "title": "База знаний вне каталога",
            "description": "Модель выбрала базу '" + str(kp["pack_id"]) + "', которой нет в knowledge_packs",
            "proposal": "Собрать/загрузить нужную базу знаний (kp_install_pack) или уточнить домен", "extension_id": None})
        kp["pack_id"] = None
        bp["knowledge_pack"] = kp

    ext_ids = set(e.get("id") for e in catalog.get("delivery_extensions", []))
    for g in bp.get("gaps") or []:
        if isinstance(g, dict) and g.get("extension_id") and g["extension_id"] not in ext_ids:
            warnings.append("gap '" + str(g.get("title", "?"))[:50] + "': unknown extension_id -> null")
            g["extension_id"] = None

    sut = bp.get("suitability") or {}
    try:
        sut["score"] = max(0, min(100, int(sut.get("score", 0))))
    except Exception:
        sut["score"] = 0
        warnings.append("suitability.score was not a number -> 0")
    if str(sut.get("risk_level", "")).lower() not in ("low", "medium", "high"):
        sut["risk_level"] = "medium"
    bp["suitability"] = sut

    # -- Write output + attach to session ------------------------------
    out = Path(output_path) if output_path else sp.parent / (session.get("session_id", sp.stem) + "_blueprint.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "generated_at": now(),
        "model_version": model,
        "catalog_version": catalog.get("catalog_version", ""),
        "session_id": session.get("session_id", ""),
        "blueprint": bp,
        "warnings": warnings
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    session["blueprint_path"] = str(out)
    session["stage"] = "blueprint"
    session.setdefault("log", []).append({"ts": now(), "event": "blueprint generated: " + str(out)})
    session["updated_at"] = now()
    sp.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"status": "success",
            "blueprint_path": str(out),
            "process_name": bp.get("process_name", ""),
            "archetype": (bp.get("archetype") or {}).get("id"),
            "suitability_score": sut.get("score"),
            "risk_level": sut.get("risk_level"),
            "self_serve_allowed": sut.get("self_serve_allowed"),
            "stages_count": len(bp.get("stages", [])),
            "knowledge_pack": (bp.get("knowledge_pack") or {}).get("pack_id"),
            "gaps_count": len(bp.get("gaps", [])),
            "open_questions_count": len(bp.get("open_questions", [])),
            "warnings": warnings[:5]}