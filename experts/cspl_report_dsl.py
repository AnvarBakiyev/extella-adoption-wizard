$extens("include.py")
include("import openpyxl", ["extella-pip install openpyxl"])

def cspl_report_dsl(action: str = "compile", program_json: str = "", records_json: str = "",
                    input_path: str = "", output_dir: str = "/tmp/cspl_report") -> dict:
    """CSPL Studio S1 (ТЗ v2 §10): системный handler предметного языка report_dsl.
    ПРОГРАММА (JSON, пишется владельцем/Qwen'ом на этапе создания — здесь LLM НЕТ, только код):
      {"report": "<название>", "columns": ["<колонка>", ...],
       "filter": {"field","op":">|>=|<|<=|==|contains","value"},   # опц., семантика F2
       "group_by": "<колонка>",                                    # опц., сводка по группам
       "totals": ["<числовая колонка>", ...],                      # опц., итоговые суммы
       "out": "md|xlsx|both"}                                      # дефолт both
    КОНТРАКТ CSPL (критерии приёмки ТЗ §10.5):
    - action=validate: некорректная программа отклоняется ДО исполнения, ошибки со ссылкой на поле;
    - action=compile: детерминированная компиляция — одинаковые (программа, данные, версия handler)
      дают байт-в-байт одинаковый .md (никаких timestamp в выходе); .xlsx проверяется по содержимому
      ячеек (zip-контейнер несёт даты — известное ограничение формата);
    - структурные ошибки, не маскируются под success.
    Вход данных: records_json (inline список записей) ИЛИ input_path (JSON: список или {"records":[...]}).
    """
    import json
    import hashlib
    from pathlib import Path

    VERSION = "1.0.0"
    OPS = (">", ">=", "<", "<=", "==", "contains")

    def _blank(v):
        return (not v) or str(v).startswith("{{")

    # ── 1. парсинг и ВАЛИДАЦИЯ программы (до любых данных и исполнения) ──
    errors = []
    program = {}
    if _blank(program_json):
        errors.append({"field": "program_json", "message": "программа не передана"})
    else:
        try:
            program = json.loads(program_json)
            if not isinstance(program, dict):
                errors.append({"field": "program_json", "message": "программа должна быть JSON-объектом"})
                program = {}
        except Exception as e:
            errors.append({"field": "program_json", "message": "не парсится как JSON: " + str(e)[:100]})

    if program:
        if not str(program.get("report", "")).strip():
            errors.append({"field": "report", "message": "обязательное поле: название отчёта"})
        cols = program.get("columns")
        if not (isinstance(cols, list) and cols and all(isinstance(c, str) and c.strip() for c in cols)):
            errors.append({"field": "columns", "message": "обязателен непустой список строк-колонок"})
        flt = program.get("filter")
        if flt is not None:
            if not isinstance(flt, dict):
                errors.append({"field": "filter", "message": "filter должен быть объектом {field, op, value}"})
            else:
                if not str(flt.get("field", "")).strip():
                    errors.append({"field": "filter.field", "message": "обязательное поле фильтра"})
                if str(flt.get("op", "")) not in OPS:
                    errors.append({"field": "filter.op", "message": "op должен быть одним из: " + ", ".join(OPS)})
                if "value" not in flt:
                    errors.append({"field": "filter.value", "message": "обязательное значение фильтра"})
        gb = program.get("group_by")
        if gb is not None and not (isinstance(gb, str) and gb.strip()):
            errors.append({"field": "group_by", "message": "group_by должен быть именем колонки"})
        tots = program.get("totals")
        if tots is not None:
            if not (isinstance(tots, list) and all(isinstance(t, str) for t in tots)):
                errors.append({"field": "totals", "message": "totals должен быть списком имён колонок"})
            elif isinstance(cols, list) and any(t not in cols for t in tots):
                errors.append({"field": "totals", "message": "каждая колонка totals обязана входить в columns"})
        if str(program.get("out", "both")) not in ("md", "xlsx", "both"):
            errors.append({"field": "out", "message": "out должен быть md | xlsx | both"})

    if errors:
        return {"status": "invalid", "handler": "cspl_report_dsl", "version": VERSION, "errors": errors}
    if action == "validate":
        return {"status": "valid", "handler": "cspl_report_dsl", "version": VERSION}

    # ── 2. данные ──
    records = None
    if not _blank(records_json):
        try:
            records = json.loads(records_json)
        except Exception as e:
            return {"status": "error", "errors": [{"field": "records_json", "message": "не парсится: " + str(e)[:100]}]}
    elif not _blank(input_path) and Path(input_path).exists():
        try:
            records = json.loads(Path(input_path).read_text(encoding="utf-8"))
        except Exception as e:
            return {"status": "error", "errors": [{"field": "input_path", "message": "не читается: " + str(e)[:100]}]}
    if isinstance(records, dict):
        records = records.get("records")
    if not (isinstance(records, list) and all(isinstance(r, dict) for r in records)):
        return {"status": "error", "errors": [{"field": "records", "message": "нужен список записей-объектов (records_json или input_path)"}]}

    # колонки программы обязаны существовать в данных (мягкий матч как в F2: casefold+подстрока)
    def _fkey(rec, fld):
        fl = str(fld).casefold().strip()
        for k in rec.keys():
            if fl == str(k).casefold().strip() or fl in str(k).casefold():
                return k
        return None

    if records:
        sample = records[0]
        missing = [c for c in program["columns"] if _fkey(sample, c) is None]
        if missing:
            return {"status": "error", "errors": [{"field": "columns",
                    "message": "колонок нет в данных: " + ", ".join(missing)}]}

    # ── 3. фильтр (семантика F2) ──
    def _num(v):
        try:
            return float(str(v).replace(" ", "").replace(",", "."))
        except Exception:
            return None

    flt = program.get("filter")
    if flt:
        def _passes(rec):
            k = _fkey(rec, flt["field"])
            if k is None:
                return True
            op, val = flt["op"], flt["value"]
            if op == "contains":
                return str(val).casefold() in str(rec.get(k, "")).casefold()
            a, b = _num(rec.get(k)), _num(val)
            if a is None or b is None:
                return True
            return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b, "==": a == b}[op]
        records = [r for r in records if _passes(r)]

    # ── 4. детерминированная компиляция ──
    cols = program["columns"]
    rows = [[("" if r.get(_fkey(r, c)) is None else str(r.get(_fkey(r, c)))) for c in cols] for r in records]
    totals = {}
    for t in (program.get("totals") or []):
        s = sum((_num(r.get(_fkey(r, t))) or 0) for r in records)
        totals[t] = int(s) if s == int(s) else round(s, 2)
    groups = {}
    gb = program.get("group_by")
    if gb:
        for r in records:
            gk = str(r.get(_fkey(r, gb), ""))
            g = groups.setdefault(gk, {"count": 0})
            g["count"] += 1
            for t in (program.get("totals") or []):
                g[t] = round(g.get(t, 0) + (_num(r.get(_fkey(r, t))) or 0), 2)

    out_dir = Path(output_dir if not _blank(output_dir) else "/tmp/cspl_report")
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = {}
    want = str(program.get("out", "both"))

    if want in ("md", "both"):
        lines = ["# " + program["report"], "",
                 "_report_dsl v" + VERSION + " · строк: " + str(len(rows)) + "_", "",
                 "| " + " | ".join(cols) + " |",
                 "|" + "---|" * len(cols)]
        lines += ["| " + " | ".join(v.replace("|", "/") for v in row) + " |" for row in rows]
        if totals:
            lines += ["", "**Итого:** " + " · ".join(k + " = " + str(v) for k, v in sorted(totals.items()))]
        if groups:
            lines += ["", "## По группам (" + gb + ")", ""]
            for gk in sorted(groups):
                g = groups[gk]
                lines.append("- **" + gk + "**: строк " + str(g["count"]) +
                             "".join(" · " + t + " = " + str(g[t]) for t in sorted(g) if t != "count"))
        md_text = "\n".join(lines) + "\n"
        mp = out_dir / "report.md"
        mp.write_text(md_text, encoding="utf-8")
        outputs["md"] = str(mp)
        outputs["md_sha256"] = hashlib.sha256(md_text.encode("utf-8")).hexdigest()

    if want in ("xlsx", "both"):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Отчёт"
        ws.append(cols)
        for row in rows:
            ws.append(row)
        if totals:
            ws.append([])
            ws.append(["Итого"] + ["" for _ in cols[1:]])
            for k in sorted(totals):
                ws.append([k, totals[k]])
        if groups:
            ws2 = wb.create_sheet("Группы")
            ws2.append([gb, "строк"] + sorted(program.get("totals") or []))
            for gk in sorted(groups):
                g = groups[gk]
                ws2.append([gk, g["count"]] + [g.get(t, 0) for t in sorted(program.get("totals") or [])])
        xp = out_dir / "report.xlsx"
        wb.save(str(xp))
        outputs["xlsx"] = str(xp)

    return {"status": "success", "handler": "cspl_report_dsl", "version": VERSION,
            "rows": len(rows), "totals": totals or None,
            "groups": len(groups) or None, "outputs": outputs}
