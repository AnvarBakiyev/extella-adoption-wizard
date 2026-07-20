$extens("include.py")
include("import requests", ["extella-pip install requests"])
include("import openpyxl", ["extella-pip install openpyxl"])

def wz_data_reality_check(
    session_id: str = "",
    api_key: str = "",
    base_url: str = "https://api.openai.com/v1",
    model: str = "gpt-4o",
    api_token: str = "",
    agent_id: str = "",
    api_base: str = "https://api.extella.ai"
) -> dict:
    """Проверка реальности данных: сверяет ФАКТИЧЕСКИЕ колонки загруженного файла
    с процессом, который клиент описал в интервью, и честно говорит — тянет ли файл
    задачу. Пишет вердикт в сессию (data_check) и возвращает его.
    Параметры: session_id (сессия визарда), api_key (LLM), base_url, model."""
    import json
    import csv
    import requests
    from pathlib import Path
    from datetime import datetime, timezone

    if not session_id:
        return {"status": "error", "message": "session_id is required"}
    if not agent_id:   # keyless: свой agent_id клиента из локального конфига (не чужой агент, не Claude)
        try:
            _cfg = json.loads((Path.home() / "extella_wizard" / "app" / "config.json").read_text(encoding="utf-8"))
            agent_id = _cfg.get("llm_agent_id") or _cfg.get("agent_id", "")
        except Exception:
            agent_id = ""
    sp = Path.home() / "extella_wizard" / "sessions" / (session_id + ".json")
    if not sp.exists():
        return {"status": "error", "message": "session not found: " + session_id}
    session = json.loads(sp.read_text(encoding="utf-8"))
    answers = session.get("answers", {}) or {}
    # Адаптивное интервью намеренно НЕ обязано использовать старые id pain/process_today/success.
    # Источник истины — задача + все фактически показанные и заполненные пары вопрос/ответ.
    answer_rows = []
    for qid, raw in answers.items():
        if isinstance(raw, dict):
            question, answer = str(raw.get("question", "")).strip(), str(raw.get("answer", "")).strip()
        else:
            question, answer = str(qid).replace("_", " "), str(raw or "").strip()
        if answer:
            answer_rows.append({"id": str(qid), "question": question or str(qid), "answer": answer})
    task = str(session.get("questionnaire_task") or session.get("goal") or "").strip()
    blueprint = {}
    try:
        bp_path = sp.with_name(session_id + "_blueprint.json")
        if bp_path.exists():
            bp_doc = json.loads(bp_path.read_text(encoding="utf-8"))
            blueprint = bp_doc.get("blueprint", bp_doc) if isinstance(bp_doc, dict) else {}
    except Exception:
        blueprint = {}
    process_context = {
        "task_in_user_words": task,
        "interview_answers": answer_rows,
        "approved_blueprint": {
            "goal": blueprint.get("goal"),
            "data_source": blueprint.get("data_source"),
            "sample_test_plan": blueprint.get("sample_test_plan"),
        } if isinstance(blueprint, dict) else {},
    }
    process_desc = json.dumps(process_context, ensure_ascii=False)

    # ── инспекция реальных файлов ──
    fdir = Path.home() / "extella_wizard" / "sessions" / (session_id + "_files")
    files_info = []
    if fdir.is_dir():
        for f in sorted(fdir.iterdir()):
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            info = {"name": f.name, "format": ext}
            try:
                if ext in (".xlsx", ".xls"):
                    wb = openpyxl.load_workbook(str(f), read_only=True, data_only=True)
                    ws = wb[wb.sheetnames[0]]
                    rows = []
                    for r in ws.iter_rows(min_row=1, max_row=15):
                        rows.append([("" if c.value is None else str(c.value)) for c in r])
                    hdr, best = 0, -1
                    for i, r in enumerate(rows):
                        filled = sum(1 for v in r if v.strip())
                        strs = sum(1 for v in r if v.strip() and not v.replace(".", "").replace("-", "").isdigit())
                        if filled + strs > best:
                            best, hdr = filled + strs, i
                    info["sheet"] = str(ws.title)
                    info["columns"] = [v for v in rows[hdr] if v.strip()]
                    info["sample_row"] = rows[hdr + 1] if hdr + 1 < len(rows) else []
                elif ext == ".csv":
                    with open(str(f), "r", encoding="utf-8", errors="replace") as fh:
                        rd = list(csv.reader(fh))
                    info["columns"] = [v for v in (rd[0] if rd else []) if v.strip()]
                    info["sample_row"] = rd[1] if len(rd) > 1 else []
                else:
                    info["note"] = "формат не инспектируется (нужен коннектор)"
            except Exception as e:
                info["error"] = str(e)[:150]
            files_info.append(info)

    if not files_info:
        result = {"verdict": "no_files",
                  "summary": "Файл-образец не приложен — проверить соответствие данных процессу нельзя. Приложите образец на шаге интервью.",
                  "missing": [], "present": [], "context_version": 2}
        session["data_check"] = result
        session["updated_at"] = datetime.now(timezone.utc).isoformat()
        sp.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"status": "success", "data_check": result}

    if not api_key and not api_token:
        return {"status": "error", "message": "нужен api_key (OpenAI) или api_token (платформенная Qwen)"}

    if not task and not answer_rows and not (isinstance(blueprint, dict) and blueprint.get("goal")):
        result = {"verdict": "needs_context", "context_version": 2,
                  "client_message": "Сначала опишите задачу или ответьте хотя бы на один вопрос интервью — тогда я проверю, подходят ли данные.",
                  "missing": [], "present": [], "computable_metrics": [], "blocked_metrics": [],
                  "files_checked": [fi["name"] for fi in files_info]}
        session["data_check"] = result
        session["updated_at"] = datetime.now(timezone.utc).isoformat()
        sp.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"status": "success", "data_check": result}

    SYSTEM = (
        "Ты — аудитор данных внедрения. Тебе дают: (1) процесс, который клиент описал словами, "
        "и (2) ФАКТИЧЕСКИЕ колонки его загруженного файла. Задача — честно определить, "
        "СОДЕРЖИТ ЛИ файл поля, необходимые для описанного процесса и его метрик. "
        "Интервью может быть адаптивным: названия/id вопросов произвольные, поэтому прочитай ВСЕ пары "
        "вопрос/ответ и исходную задачу по смыслу. НЕ требуй старые пункты анкеты (боль, текущий процесс, "
        "критерий успеха) только потому, что нет полей с такими id. НЕ называй описания/ответы интервью "
        "полями файла. В missing разрешены ТОЛЬКО конкретные атрибуты исходных данных, без которых нельзя "
        "получить явно запрошенный результат. Проверяй минимально достаточные данные, а не идеальную схему: "
        "не запрашивай дополнительную детализацию, если заявленный результат уже считается из имеющихся колонок. "
        "Для таблицы допустимо считать одну строку одной записью и делать count/group by по указанной колонке, "
        "если контекст или пример строки этому не противоречат. Не требуй дату начала периода для показателя на "
        "текущую дату, если уже есть дата окончания. Не превращай полезное уточнение или возможность сделать "
        "метрику точнее в обязательное missing: такие оговорки можно кратко указать в client_message. "
        "Если процесс действительно требует, например, срок оплаты/дату продления, а в файле только транзакции — "
        "это разрыв. Верни СТРОГО JSON: "
        '{"verdict":"yes"|"partial"|"no", '
        '"present":[<колонки файла, релевантные процессу>], '
        '"missing":[{"need":"<что нужно процессу>","why":"<зачем>","in_file":false}], '
        '"computable_metrics":[<какие метрики РЕАЛЬНО можно посчитать из этого файла>], '
        '"blocked_metrics":["<метрика из явно заявленного результата — почему её нельзя посчитать>"], '
        '"client_message":"<1-3 предложения клиенту простым языком: тянет ли файл задачу и что делать при разрыве>"}'
    )
    user = ("КОНТЕКСТ ЗАДАЧИ И ВСЕ ОТВЕТЫ ИНТЕРВЬЮ (JSON):\n" + process_desc +
            "\n\nФАКТИЧЕСКИЕ ФАЙЛЫ (колонки и пример строки):\n" +
            json.dumps(files_info, ensure_ascii=False)[:3500])

    try:
        if api_key:
            r = requests.post(base_url.rstrip("/") + "/chat/completions",
                              headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                              json={"model": model, "temperature": 0,
                                    "response_format": {"type": "json_object"},
                                    "messages": [{"role": "system", "content": SYSTEM},
                                                 {"role": "user", "content": user}],
                                    "max_tokens": 1200},
                              timeout=120)
            if r.status_code != 200:
                return {"status": "error", "message": "LLM " + str(r.status_code) + ": " + r.text[:150]}
            content = r.json()["choices"][0]["message"]["content"]
        else:
            rr = requests.post(api_base.rstrip("/") + "/api/agent/run",
                headers={"X-Auth-Token": api_token, "Content-Type": "application/json",
                         "X-Profile-Id": "default", "X-Agent-Id": agent_id or "agent_extella_default"},
                json={"agent_id": agent_id,
                      "input": SYSTEM + "\n\n" + user + "\n\nВерни СТРОГО валидный JSON без markdown.",
                      "run_timeout": 180, "store": False}, timeout=240).json()
            content = "".join(c.get("text", "") for it in (rr.get("output") or [])
                              if it.get("type") == "message"
                              for c in (it.get("content") or []) if c.get("type") == "output_text")
        import re as _re
        _m = _re.search(r"\{.*\}", content, _re.S)
        result = json.loads(_m.group(0) if _m else content)
    except Exception as e:
        return {"status": "error", "message": str(e)[:200]}

    def _text_item(value):
        if isinstance(value, dict):
            title = value.get("metric") or value.get("name") or value.get("field") or value.get("need") or value.get("title")
            why = value.get("why") or value.get("reason") or value.get("evidence")
            if title and why:
                return str(title) + " — " + str(why)
            if title:
                return str(title)
            return json.dumps(value, ensure_ascii=False)
        return str(value or "")

    for key in ("present", "computable_metrics", "blocked_metrics"):
        raw = result.get(key) if isinstance(result.get(key), list) else []
        result[key] = [txt for item in raw if (txt := _text_item(item).strip())]
    clean_missing = []
    # Защита от старого контракта анкеты: описание задачи уже передано модели как контекст,
    # а не обязано существовать колонкой в исходном файле.
    context_only_markers = (
        "описание процесса", "описание бизнес", "бизнес-задач", "бизнес задача",
        "боль клиента", "главная боль", "текущее состояние", "текущий процесс",
        "критерий успеха", "критерии успеха",
    )
    for item in result.get("missing") if isinstance(result.get("missing"), list) else []:
        if isinstance(item, dict):
            need = str(item.get("need") or item.get("field") or item.get("name") or "").strip()
            if need and not any(marker in need.lower() for marker in context_only_markers):
                clean_missing.append({"need": need, "why": str(item.get("why") or item.get("reason") or "").strip(),
                                      "in_file": False})
        elif str(item or "").strip() and not any(marker in str(item).lower() for marker in context_only_markers):
            clean_missing.append({"need": str(item).strip(), "why": "", "in_file": False})
    result["missing"] = clean_missing
    if result.get("verdict") not in ("yes", "partial", "no", "needs_context"):
        result["verdict"] = "partial"
    result["client_message"] = _text_item(result.get("client_message")).strip()
    result["files_checked"] = [fi["name"] for fi in files_info]
    result["context_version"] = 2
    session["data_check"] = result
    session.setdefault("log", []).append({"ts": datetime.now(timezone.utc).isoformat(),
                                          "event": "data reality check: " + str(result.get("verdict"))})
    session["updated_at"] = datetime.now(timezone.utc).isoformat()
    sp.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "success", "data_check": result}
