# expert: wz_data_reality_check
# description: Проверка реальности данных: сверяет ФАКТИЧЕСКИЕ колонки загруженного файла с процессом, который клиент описал в интервью, и честно говорит — тянет ли 
# params: session_id, api_key, base_url, model

$extens("include.py")
include("import requests", ["extella-pip install requests"])
include("import openpyxl", ["extella-pip install openpyxl"])

def wz_data_reality_check(
    session_id: str = "",
    api_key: str = "",
    base_url: str = "https://api.openai.com/v1",
    model: str = "gpt-4o"
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
    sp = Path.home() / "extella_wizard" / "sessions" / (session_id + ".json")
    if not sp.exists():
        return {"status": "error", "message": "session not found: " + session_id}
    session = json.loads(sp.read_text(encoding="utf-8"))
    answers = session.get("answers", {}) or {}

    def a(key):
        v = answers.get(key)
        if isinstance(v, dict):
            return str(v.get("answer", ""))
        return str(v or "")

    process_desc = (
        "Боль: " + a("pain") +
        "\nКак сейчас: " + a("process_today") +
        "\nИсточник данных (со слов клиента): " + a("data_sources") +
        "\nКритерий успеха / нужные метрики: " + a("success") +
        "\nПериодичность: " + a("frequency")
    )

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
                  "missing": [], "present": []}
        session["data_check"] = result
        session["updated_at"] = datetime.now(timezone.utc).isoformat()
        sp.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"status": "success", "data_check": result}

    if not api_key:
        return {"status": "error", "message": "api_key is required for the reality check LLM"}

    SYSTEM = (
        "Ты — аудитор данных внедрения. Тебе дают: (1) процесс, который клиент описал словами, "
        "и (2) ФАКТИЧЕСКИЕ колонки его загруженного файла. Задача — честно определить, "
        "СОДЕРЖИТ ЛИ файл поля, необходимые для описанного процесса и его метрик. "
        "НЕ выдумывай поля, которых нет. Если процесс требует, например, срок оплаты/дату продления, "
        "а в файле только транзакции — это разрыв. Верни СТРОГО JSON: "
        '{"verdict":"yes"|"partial"|"no", '
        '"present":[<колонки файла, релевантные процессу>], '
        '"missing":[{"need":"<что нужно процессу>","why":"<зачем>","in_file":false}], '
        '"computable_metrics":[<какие метрики РЕАЛЬНО можно посчитать из этого файла>], '
        '"blocked_metrics":[<метрики из критерия успеха, которые посчитать НЕЛЬЗЯ, и почему>], '
        '"client_message":"<1-3 предложения клиенту простым языком: тянет ли файл задачу и что делать при разрыве>"}'
    )
    user = ("ПРОЦЕСС (со слов клиента):\n" + process_desc +
            "\n\nФАКТИЧЕСКИЕ ФАЙЛЫ (колонки и пример строки):\n" +
            json.dumps(files_info, ensure_ascii=False)[:3500])

    try:
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
        result = json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception as e:
        return {"status": "error", "message": str(e)[:200]}

    result["files_checked"] = [fi["name"] for fi in files_info]
    session["data_check"] = result
    session.setdefault("log", []).append({"ts": datetime.now(timezone.utc).isoformat(),
                                          "event": "data reality check: " + str(result.get("verdict"))})
    session["updated_at"] = datetime.now(timezone.utc).isoformat()
    sp.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "success", "data_check": result}
