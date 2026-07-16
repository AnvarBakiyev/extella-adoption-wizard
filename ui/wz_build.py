"""Кластер СТРОЙКИ моста Визарда (Фаза 1, шов #3).

Выделено из server.py: аудит собранных экспертов, кодоген стадии (_build_one), шаблоны оркестратора и
kp-стадии, сборка оркестратора, главный оркестратор стройки _run_build (план→кодоген→аудит→продовый агент).
Зависит от wz_platform (CONFIG/BASE/api/run_expert/qwen_agent) и wz_llm (run_llm_expert/design_agent).
Наружу торчит только _run_build (его зовёт HTTP-хендлер /x/build).
"""
import json
import re
import time
import base64
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from wz_platform import CONFIG, BASE, api, run_expert, qwen_agent
from wz_llm import run_llm_expert, design_agent, gen_panel_manifest

SESS_DIR = Path.home() / "extella_wizard" / "sessions"
RUNS_DIR = Path.home() / "extella_wizard" / "runs"
HOST_TARGET = "85800354-f7b7-449f-b526-9357cd91f780"  # managed-хостинг VPS (PS.kz)


def _audit_experts(names):
    """Детерминированный предзапусковый аудит кода построенных экспертов."""
    import re as _re
    issues = []
    for n in names:
        e = api("/api/expert/get", {"name": n, "global": True})
        code = e.get("expert_code", "") if isinstance(e, dict) else ""
        if not code:
            continue
        checks = {
            "секрет в коде": bool(_re.search(r"(sk-[A-Za-z0-9]{20}|api[_-]?key\s*=\s*['\"][A-Za-z0-9]{16})", code)),
            "отправка почты": ("smtplib" in code or "sendmail" in code),
            "внешняя запись": bool(_re.search(r"https?://(?!api\.extella\.ai|disnet\.extella\.ai)[a-z0-9.]+/", code)),
            "путь устройства": ("/Users/" in code or "/home/" in code),
        }
        for k, v in checks.items():
            if v:
                issues.append(n + ": " + k)
    verdict = "allow" if not issues else "allow-with-confirmation"
    return {"verdict": verdict, "issues": issues}


def _inspect_sample(session_id):
    """Реальные колонки загруженного файла-образца для грануднинга кодогена."""
    fdir = SESS_DIR / (session_id + "_files")
    if not fdir.is_dir():
        return "", None
    files = [p for p in sorted(fdir.iterdir()) if p.is_file()]
    if not files:
        return "", None
    f = files[0]
    ext = f.suffix.lower()
    try:
        if ext in (".xlsx", ".xls"):
            import openpyxl
            wb = openpyxl.load_workbook(str(f), read_only=True, data_only=True)
            ws = wb[wb.sheetnames[0]]
            rows = [[("" if c.value is None else str(c.value)) for c in r]
                    for r in ws.iter_rows(min_row=1, max_row=15)]
            hdr, best = 0, -1
            for i, r in enumerate(rows):
                sc = sum(1 for v in r if v.strip()) + sum(1 for v in r if v.strip() and not v.replace(".", "").replace("-", "").isdigit())
                if sc > best:
                    best, hdr = sc, i
            cols = [v for v in rows[hdr] if v.strip()]
            sample = rows[hdr + 1] if hdr + 1 < len(rows) else []
            hint = ("\n\nФАКТИЧЕСКАЯ СТРУКТУРА ФАЙЛА (СТРОЙ СТРОГО ПОД ЭТИ КОЛОНКИ, не выдумывай поля): "
                    + "лист '" + str(ws.title) + "', заголовки в строке #" + str(hdr + 1)
                    + ", колонки: " + json.dumps(cols, ensure_ascii=False)
                    + ", пример: " + json.dumps(sample, ensure_ascii=False)[:300])
            return hint, str(f)
        if ext == ".csv":
            import csv as _csv
            rd = list(_csv.reader(open(str(f), "r", encoding="utf-8", errors="replace")))
            cols = [v for v in (rd[0] if rd else []) if v.strip()]
            return ("\n\nФАКТИЧЕСКАЯ СТРУКТУРА ФАЙЛА (СТРОЙ ПОД ЭТИ КОЛОНКИ): csv, колонки: "
                    + json.dumps(cols, ensure_ascii=False)), str(f)
    except Exception:
        return "", str(f)
    return "", str(f)


_BUILD_SYS = """Ты — генератор кода СТАДИИ КОНВЕЙЕРА для платформы Extella. Верни ТОЛЬКО JSON:
{"code":"<полный код>", "description":"<англ.: что делает>"}

ЖЁСТКИЙ КОНТРАКТ СТАДИИ (соблюдать точно):
- Сигнатура РОВНО: def <ИМЯ>(input_path: str = "", output_path: str = "", rules_json: str = "", fields_json: str = "") -> dict. НИКАКИХ других параметров.
- ПРАВИЛА ВЛАДЕЛЬЦА (F2-контракт): rules_json — JSON-список правил словами, fields_json — JSON-словарь полей.
  Если непусты и НЕ начинаются с "{{" — распарси (json.loads в try) и ПРИМЕНИ релевантные правила к своей работе
  (фильтры порогов, пометки, доп-колонки, сортировка, что исключить); нерелевантные твоей стадии — игнорируй молча.
  Пустые/непарсящиеся — работай как обычно (обратная совместимость).
- ВХОД: читай из input_path. %(INPUT_DESC)s
- РАБОТА: %(PURPOSE)s
- ВЫХОД: запиши результат в output_path как JSON. %(OUTPUT_DESC)s Если это отчётная стадия — можешь дополнительно писать .md/.docx рядом (import docx через include), но JSON в output_path обязателен.
- ВЕРНИ компактный dict: {"status":"success","output_path":output_path, ...ключевые счётчики}. НЕ клади крупные данные в возврат.

Стандарт: первая строка $extens("include.py"); зависимости через include("import X",["extella-pip install X"]) (openpyxl/ docx — так; стдлиб json/csv/datetime — include("import json",[])); РОВНО ОДНА top-level функция (имя строго заданное), хелперы ВНУТРИ неё, не переопределяй include/load_module; валидация входов с ранним return {"status":"error"}; без хардкода путей/ключей; не обращаться к KV."""


def _build_one(expert_name, task, schema_hint, is_first, is_last, accept_input, llm):
    """Стройка СТАДИИ по контракту input_path->output_path. Keyless-путь: модель строит НАТИВНО
    (create-действием, исходник не в чат — уважает guard fine-tune), харнесс НЕЗАВИСИМО перечитывает
    созданное, усыновляет в global и ПРИНИМАЕТ прогоном на реальном входе. With-key путь — как было
    (модель отдаёт код текстом). Источник правды — get+прогон, не слово модели. Возвращает (ok, output_path, detail)."""
    import urllib.request as _u
    cspl = task.get("cspl", "fython")
    if is_first:
        input_desc = ("input_path — путь к ИСХОДНОМУ файлу данных клиента (xlsx/csv). Распарси его "
                      "(openpyxl для xlsx). ВАЖНО: строка заголовков даёт НАЗВАНИЯ колонок; ДАННЫЕ начинаются "
                      "со строки СРАЗУ ПОСЛЕ заголовков. Саму строку заголовков в записи НЕ включай. "
                      "Для КАЖДОЙ непустой строки данных собери словарь {название_колонки: ЗНАЧЕНИЕ ЯЧЕЙКИ (.value)}. "
                      "Значение — число/текст/дата, НЕ номер строки и НЕ индекс колонки. Пропускай пустые строки и "
                      "строки, повторяющие заголовки. Пиши json.dump(..., ensure_ascii=False, default=str)." + schema_hint)
        out_desc = "Запиши НОРМАЛИЗОВАННЫЙ список записей (list of dict со ЗНАЧЕНИЯМИ ячеек) как JSON."
    else:
        input_desc = ("input_path — путь к JSON-файлу от предыдущей стадии (список записей или "
                      "{\"records\":[...],\"summary\":{...}}). Прочитай json.load, работай с записями.")
        out_desc = ("ОБЯЗАТЕЛЬНО ВЫЧИСЛИ агрегаты из входных записей (НЕ копируй записи без обработки!): "
                    "определи числовую колонку ИТОГОВОЙ суммы — ПРЕДПОЧИТАЙ колонку, где в названии есть "
                    "'сумма'/'итог'/'стоимость'/'total'/'amount' (НЕ бери 'цена'/'price'/'цена за единицу', "
                    "если есть колонка итоговой суммы), и посчитай "
                    "total_count (число записей), total_sum (сумма по ней); построй разбивки — словари "
                    "{значение: сумма} по каждой НЕчисловой категориальной колонке (напр. Категория, Способ закупки). "
                    "Запиши JSON {\"summary\": {\"total_count\": N, \"total_sum\": X, \"by_<колонка>\": {...}, ...}, "
                    "\"records\": [...]}. "
                    + ("Это ФИНАЛЬНАЯ стадия — дополнительно собери человекочитаемый отчёт (.md рядом с output_path) "
                       "из summary." if is_last else ""))
    sysmsg = _BUILD_SYS % {"INPUT_DESC": input_desc, "PURPOSE": str(task.get("purpose", "обработай данные")),
                           "OUTPUT_DESC": out_desc}
    user = ("Имя эксперта (СТРОГО): " + expert_name + "\nCSPL: " + cspl +
            "\nНазначение: " + str(task.get("purpose", "")) +
            "\nСгенерируй код стадии строго по контракту (input_path, output_path).")
    out_path = "/tmp/stage_" + expert_name + ".json"
    last_err = None
    # директива нативной стройки: модель СОЗДАЁТ эксперта действием, исходник — только в create, не в чат
    build_directive = ("\n\nПОСТРОЙ этого эксперта НА ПЛАТФОРМЕ действием (создай ИЛИ обнови эксперта) под именем "
                       "СТРОГО '" + expert_name + "', cspl=" + cspl + ", сигнатура РОВНО "
                       "def " + expert_name + "(input_path=\"\", output_path=\"\"). Исходный код помещай ТОЛЬКО в "
                       "действие создания эксперта — НЕ печатай исходник в чат. РОВНО одна top-level функция, хелперы внутри.")
    for attempt in range(3):
        code = ""
        _descr = ""
        if llm.get("api_key"):
            # managed-build по внешнему ключу (без guard): модель отдаёт код текстом, харнесс сохраняет
            try:
                rq = _u.Request(llm["base_url"].rstrip("/") + "/chat/completions",
                                data=json.dumps({"model": llm["model"], "temperature": 0,
                                                 "response_format": {"type": "json_object"},
                                                 "messages": [{"role": "system", "content": sysmsg},
                                                              {"role": "user", "content": user}],
                                                 "max_tokens": 3500}).encode(),
                                headers={"Authorization": "Bearer " + llm["api_key"], "Content-Type": "application/json"},
                                method="POST")
                with _u.urlopen(rq, timeout=150) as r:
                    _content = json.loads(r.read().decode())["choices"][0]["message"]["content"]
                spec = json.loads(_content)
                code = spec.get("code", "")
                _descr = str(spec.get("description", ""))[:900]
            except Exception as e:
                last_err = "LLM: " + str(e)[:150]; time.sleep(2 + attempt * 4); continue
        else:
            # НАТИВНАЯ стройка на нашей модели: модель СОЗДАЁТ эксперта действием (исходник не в чат,
            # guard fine-tune уважается), store:true — иначе нативные действия не исполняются.
            # Затем харнесс НЕЗАВИСИМО перечитывает созданное В СКОУПЕ МОДЕЛИ (X-Agent-Id = строитель).
            _b = llm.get("agent_id") or qwen_agent()
            _tok = llm.get("api_token", CONFIG["auth_token"])
            _base = llm.get("api_base", "https://api.extella.ai").rstrip("/")
            _hdr = {"X-Auth-Token": _tok, "Content-Type": "application/json",
                    "X-Profile-Id": "default", "X-Agent-Id": _b or "agent_extella_default"}
            try:
                rq = _u.Request(_base + "/api/agent/run",
                                data=json.dumps({"agent_id": _b, "input": sysmsg + "\n\n" + user + build_directive,
                                                 "run_timeout": 300, "store": True}).encode(),
                                headers=_hdr, method="POST")
                with _u.urlopen(rq, timeout=330) as r:
                    r.read()
            except Exception as e:
                last_err = "build-run: " + str(e)[:150]; time.sleep(3 + attempt * 4); continue
            try:  # независимый get созданного (не слово модели — реальная запись)
                gq = _u.Request(_base + "/api/expert/get", data=json.dumps({"name": expert_name}).encode(),
                                headers=_hdr, method="POST")
                with _u.urlopen(gq, timeout=60) as r:
                    g = json.loads(r.read().decode())
            except Exception as e:
                g = {}; last_err = "get after build: " + str(e)[:150]
            code = (g.get("expert_code") or "") if isinstance(g, dict) else ""
            _descr = str((g.get("description") if isinstance(g, dict) else "") or "")[:900]
        # общая валидация: РОВНО одна top-level функция, имя строго expert_name
        code = re.sub(r"(?ms)^def\s+(?:load_module|include)\s*\(.*?(?=^\S|\Z)", "", code)
        tops = re.findall(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", code, flags=re.M)
        if len(tops) != 1:
            user += ("\n\nОШИБКА: нужна РОВНО ОДНА top-level функция def " + expert_name +
                     "(input_path,output_path), нашёл " + str(tops) + " (хелперы внутри); построй заново.")
            time.sleep(1); continue
        if tops[0] != expert_name:
            code = re.sub(r"^def\s+" + re.escape(tops[0]) + r"\s*\(", "def " + expert_name + "(", code, count=1, flags=re.M)
        # description ОБЯЗАН быть непустым: /api/expert/save валидирует min-длину (string_too_short),
        # а нативный create кладёт эксперта без description → get отдаёт пусто → save падал.
        if not _descr.strip():
            _descr = str(task.get("purpose", "")).strip()[:400]
        if len(_descr.strip()) < 8:
            _descr = "Стадия обработки данных: " + expert_name
        # усыновляем в GLOBAL: оркестратор ждёт global, а нативный create кладёт в скоуп модели
        sv = api("/api/expert/save", {"name": expert_name, "description": _descr,
                                      "code": code, "kwargs": {"input_path": "", "output_path": ""},
                                      "cspl": cspl, "global": True})
        if sv.get("status") not in ("success", None) and sv.get("id") is None and "error" in str(sv).lower():
            return False, None, "save: " + str(sv)[:150]
        # приёмка = реальный прогон стадии на фактическом входе (это же и звено среза)
        if Path(out_path).exists():
            try: Path(out_path).unlink()
            except Exception: pass
        run_out = run_expert(expert_name, {"input_path": accept_input, "output_path": out_path}, wait=300)
        # СТРОГАЯ приёмка: выход должен быть валидным JSON с непустыми данными (не просто «файл есть»)
        why = None
        if not Path(out_path).exists() or Path(out_path).stat().st_size == 0:
            why = "output_path не создан/пуст; run=" + str(run_out)[:180]
        else:
            try:
                data = json.loads(Path(out_path).read_text(encoding="utf-8"))
            except Exception as e:
                why = "выход не валидный JSON (" + str(e)[:80] + ") — пиши json.dump(ensure_ascii=False, default=str)"
            else:
                recs = data if isinstance(data, list) else (data.get("records") or data.get("rows") or data.get("items") or [])
                if is_first:
                    if not isinstance(recs, list) or len(recs) == 0 or not isinstance(recs[0], dict):
                        why = "первая стадия должна вернуть НЕПУСТОЙ список словарей-записей"
                    else:
                        def _is_headerish(rec):
                            vals = list(rec.values())
                            if vals and all(isinstance(v, int) for v in vals) and (max(vals) - min(vals)) <= len(vals) + 2:
                                return True  # значения-индексы колонок
                            return sum(1 for k, v in rec.items() if str(v).strip() == str(k).strip()) >= max(2, len(rec) // 2)
                        # прагматично: САМИ чистим строки-заголовки/индексы из вывода, не заваливаем сборку
                        cleaned = [r for r in recs if isinstance(r, dict) and not _is_headerish(r)]
                        if not cleaned:
                            why = "после отсева заголовков не осталось записей-данных — парсер не извлёк реальные строки"
                        elif len(cleaned) != len(recs):
                            Path(out_path).write_text(json.dumps(cleaned, ensure_ascii=False, default=str), encoding="utf-8")
                else:
                    summ = data.get("summary") if isinstance(data, dict) else None
                    has_num = isinstance(summ, dict) and any(
                        isinstance(v, (int, float)) or (isinstance(v, dict) and any(isinstance(x, (int, float)) for x in v.values()))
                        for v in summ.values())
                    if not has_num:
                        why = ("стадия обязана ВЫЧИСЛИТЬ summary с числами (total_count/total_sum/by_<колонка>), "
                               "а не копировать записи; сейчас summary пуст или без чисел")
        if why is None:
            return True, out_path, "built+accepted"
        user += "\n\nПРИЁМКА УПАЛА (вход " + str(accept_input) + "): " + why + ". Исправь под контракт."
    return False, None, "3 попытки: " + str(why or last_err or "не прошли приёмку")[:150]


_ORCH_TEMPLATE = '''$extens("include.py")
include("import requests", ["extella-pip install requests"])
include("import openpyxl", ["extella-pip install openpyxl"])
include("from cryptography.fernet import Fernet", ["extella-pip install cryptography"])

def %(NAME)s(source_file: str = "", work_dir: str = "%(WORKDIR)s", api_token: str = "", api_base: str = "https://api.extella.ai", target: str = "", source_key: str = "", rules_json: str = "", fields_json: str = "") -> dict:
    """Автосгенерированный оркестратор процесса. Гоняет контрактную цепочку стадий
    (input_path -> output_path) на исходном файле, чистит заголовки, возвращает сводку
    и рисует отчёт .md + .xlsx. Параметры: source_file, work_dir, api_token, target
    (пиннинг устройства для стадий), source_key (ключ файла в общем сторе для резолвера)."""
    import json
    import requests
    from pathlib import Path
    from datetime import datetime, timezone

    STAGES = %(STAGES)s
    KP_STAGES = %(KP_STAGES)s
    if not api_token:
        cfg = Path.home() / "extella_wizard" / "app" / "config.json"
        try:
            api_token = json.loads(cfg.read_text(encoding="utf-8")).get("auth_token", "") if cfg.exists() else ""
        except Exception:
            api_token = ""
    if not api_token:
        return {"status": "error", "message": "api_token не передан и нет bridge-конфига"}
    if not source_file:
        return {"status": "error", "message": "source_file обязателен"}
    wd = Path(work_dir); wd.mkdir(parents=True, exist_ok=True)
    headers = {"X-Auth-Token": api_token, "Content-Type": "application/json",
               "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}

    # резолвер источника: если локального пути нет (процесс исполняется на хостинге) —
    # материализуем файл из общего стора (KV) чанками base64 в рабочую папку
    _resolve_err = ""
    if source_file and not Path(source_file).exists():
        import base64 as _b64, hashlib as _hl
        _bn = Path(source_file).name
        _base = source_key or ("file:%(SID)s:" + _hl.md5(_bn.encode("utf-8")).hexdigest()[:12])
        try:
            _mr = requests.post(api_base.rstrip("/") + "/api/kv/get", headers=headers, json={"key": _base + ":meta"}, timeout=120).json()
            _mv = _mr.get("value")
            if _mv:
                _meta = json.loads(_mv); _buf = ""
                for _i in range(int(_meta.get("chunks", 0))):
                    _cr = requests.post(api_base.rstrip("/") + "/api/kv/get", headers=headers, json={"key": _base + ":" + str(_i)}, timeout=120).json()
                    _buf += _cr.get("value") or ""
                if _buf:
                    if _meta.get("enc"):
                        # файл зашифрован — расшифровываем ЛОКАЛЬНЫМ vault.key устройства (не из KV)
                        _kc = [Path("/opt/extella-listener/extella_wizard/vault.key"), Path.home() / "extella_wizard/app/vault.key", Path.cwd() / "extella_wizard/vault.key"]
                        _kp = next((c for c in _kc if c.exists()), None)
                        if not _kp:
                            _resolve_err = "зашифрованный источник: локальный vault.key не найден на устройстве-хостинге"
                            _rawf = None
                        else:
                            _rawf = Fernet(_kp.read_bytes()).decrypt(_buf.encode())
                    else:
                        _rawf = _b64.b64decode(_buf)
                    if _rawf:
                        _tmp = wd / _bn
                        _tmp.write_bytes(_rawf)
                        source_file = str(_tmp)
        except Exception as _e:
            _resolve_err = _resolve_err or ("не удалось восстановить источник из стора: " + str(_e)[:120])
    # честный fail вместо тихой пропажи файла (напр. enc без ключа / битые чанки)
    if source_file and not Path(source_file).exists():
        return {"status": "error", "message": _resolve_err or ("источник не найден на устройстве и не восстановлен из стора: " + str(source_file))}

    def is_headerish(rec):
        if not isinstance(rec, dict): return True
        vals = list(rec.values())
        if vals and all(isinstance(v, int) for v in vals) and (max(vals) - min(vals)) <= len(vals) + 2: return True
        return sum(1 for k, v in rec.items() if str(v).strip() == str(k).strip()) >= max(2, len(rec) // 2)

    def clean_file(path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return
        if isinstance(data, list):
            cl = [r for r in data if not is_headerish(r)]
            if cl and len(cl) != len(data):
                Path(path).write_text(json.dumps(cl, ensure_ascii=False, default=str), encoding="utf-8")

    prev, last_out = source_file, None
    for i, name in enumerate(STAGES):
        outp = str(wd / ("stage%%d.json" %% i))
        _params = {"input_path": prev, "output_path": outp}
        # F2 (контракт параметров): правила/поля владельца доступны КАЖДОЙ стадии этой сборки
        # (кодоген генерит стадии с этими kwargs; менять поведение обязана только та, кому релевантно)
        if not (rules_json or "").startswith("{{") and rules_json:
            _params["rules_json"] = rules_json
        if not (fields_json or "").startswith("{{") and fields_json:
            _params["fields_json"] = fields_json
        if name in KP_STAGES:   # knowledge-стадия реюзает kp_ask — нужен target (где лежит база) и токен
            _params["target"] = target
            _params["api_token"] = api_token
        body = {"expert_name": name, "params": _params, "global": True}
        if target:
            body["target"] = target
        r = requests.post(api_base.rstrip("/") + "/api/expert/run", headers=headers, json=body, timeout=600)
        try:
            res = r.json().get("result", r.json())
        except Exception:
            res = {}
        ok = (isinstance(res, dict) and res.get("status") == "success") or (Path(outp).exists() and Path(outp).stat().st_size > 0)
        if ok and i == 0:
            clean_file(outp)
        if not Path(outp).exists():
            return {"status": "error", "failed_stage": name, "detail": str(res)[:200]}
        prev, last_out = outp, outp

    summary = {}
    try:
        data = json.loads(Path(last_out).read_text(encoding="utf-8"))
        summary = data.get("summary", {}) if isinstance(data, dict) else {}
    except Exception:
        pass

    # F2: структурные правила владельца ({"field","op","value"} внутри rules_json) применяются
    # ЗДЕСЬ детерминированно — фильтр записей последней стадии + честный пересчёт сводки.
    # Текстовые правила из того же списка — дело кодогенных стадий; строки просто пропускаем.
    _structs = []
    try:
        if rules_json and not rules_json.startswith("{{"):
            _structs = [r for r in json.loads(rules_json)
                        if isinstance(r, dict) and r.get("field") and r.get("op")]
    except Exception:
        _structs = []
    if _structs:
        try:
            _fd = json.loads(Path(last_out).read_text(encoding="utf-8"))
            _recs = _fd if isinstance(_fd, list) else (_fd.get("records") if isinstance(_fd, dict) else None)
            if isinstance(_recs, list) and _recs and isinstance(_recs[0], dict):
                def _fkey(rec, fld):
                    fl = str(fld).casefold().strip()
                    for k in rec.keys():
                        if fl == str(k).casefold().strip() or fl in str(k).casefold():
                            return k
                    return None
                def _fnum(v):
                    try:
                        return float(str(v).replace(" ", "").replace(",", "."))
                    except Exception:
                        return None
                def _passes(rec):
                    for ru in _structs:
                        k = _fkey(rec, ru.get("field"))
                        if k is None:
                            continue   # поля нет в записи — правило её не душит (не наш разрез)
                        op, val = str(ru.get("op")), ru.get("value")
                        if op == "contains":
                            if str(val).casefold() not in str(rec.get(k, "")).casefold():
                                return False
                            continue
                        a, b = _fnum(rec.get(k)), _fnum(val)
                        if a is None or b is None:
                            continue
                        if (op == ">" and not a > b) or (op == ">=" and not a >= b) or \
                           (op == "<" and not a < b) or (op == "<=" and not a <= b) or \
                           (op in ("==", "=") and not a == b):
                            return False
                    return True
                _flt = [r for r in _recs if _passes(r)]
                if len(_flt) != len(_recs):
                    _new = {"total_count": len(_flt), "filtered_by_rules": len(_recs) - len(_flt)}
                    # колонка суммы: по имени + в ней реально есть числа (анонимизация могла замаскировать в ***)
                    _sumcol = next((c for c in _recs[0].keys()
                                    if ("сумм" in str(c).casefold() or "amount" in str(c).casefold() or "итог" in str(c).casefold())
                                    and any(_fnum(r.get(c)) is not None for r in _flt)), None)
                    if _sumcol:
                        _new["total_sum"] = sum((_fnum(r.get(_sumcol)) or 0) for r in _flt)
                    for k in list(summary.keys()):
                        if str(k).startswith("by_"):
                            _cnt = {}
                            for r in _flt:
                                ck = _fkey(r, str(k)[3:])
                                if ck is not None:
                                    v = str(r.get(ck))
                                    _cnt[v] = _cnt.get(v, 0) + 1
                            _new[k] = _cnt
                    summary = _new
        except Exception:
            pass   # применение правил не должно ронять прогон — сводка останется полной

    try:
        (wd / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception:
        pass

    # детерминированный рендер отчёта .md + .xlsx из summary
    md = wd / "report.md"; xlsx = wd / "report.xlsx"
    lines = ["# Сводка процесса", "", "_Сформировано: " + datetime.now(timezone.utc).isoformat()[:16].replace("T", " ") + " UTC_", ""]
    tc = summary.get("total_count"); ts = summary.get("total_sum")
    if tc is not None: lines.append("- Всего позиций: **%%s**" %% tc)
    if ts is not None: lines.append("- Общая сумма: **%%s**" %% format(ts, ",").replace(",", " "))
    for k, v in summary.items():
        if k.startswith("by_") and isinstance(v, dict) and v:
            lines += ["", "## " + k[3:], "", "| Значение | Сумма |", "|---|---|"]
            for kk, vv in list(v.items())[:20]:
                lines.append("| %%s | %%s |" %% (kk, vv))
    md.write_text("\\n".join(lines), encoding="utf-8")
    try:
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Сводка"
        ws.append(["Показатель", "Значение"])
        if tc is not None: ws.append(["Всего позиций", tc])
        if ts is not None: ws.append(["Общая сумма", ts])
        for k, v in summary.items():
            if k.startswith("by_") and isinstance(v, dict) and v:
                ws.append([]); ws.append([k[3:], ""])
                for kk, vv in list(v.items())[:50]:
                    ws.append([kk, vv])
        wb.save(str(xlsx))
    except Exception:
        pass

    result = {"status": "success", "summary": summary, "total_count": tc, "total_sum": ts,
              "report_md": str(md), "report_xlsx": str(xlsx), "host": __import__("socket").gethostname()}
    # межустройственный слепок последнего прогона в KV — планировщик читает его,
    # т.к. вложенный прогон возвращается отложенным без task_id.
    try:
        ns = Path(work_dir).name.replace("_run", "")
        rec = {"at": datetime.now(timezone.utc).isoformat(), "status": "success",
               "total_count": tc, "total_sum": ts, "report_xlsx": str(xlsx),
               "host": __import__("socket").gethostname()}
        requests.post(api_base.rstrip("/") + "/api/kv/set", headers=headers,
                      json={"key": "lastrun:" + ns, "value": json.dumps(rec, ensure_ascii=False, default=str),
                            "description": "lastrun " + ns}, timeout=60)
    except Exception:
        pass
    return result
'''


# Детерминированная обёртка knowledge-стадии: РЕЮЗ готового kp_ask (retrieval-движок не переписываем).
# Читает вход, формирует запрос, зовёт kp_ask(pack, question) на нужном устройстве (target), кладёт
# найденные нормы в legal_context для следующей стадии. Мягкая деградация: если базы/сети нет
# (build-срез без target) — проводит вход дальше с пустым контекстом, не роняя пайплайн.
_KP_STAGE_TEMPLATE = '''$extens("include.py")
include("import urllib.request", [])

def %(NAME)s(input_path="", output_path="", target="", api_token="", api_base="https://api.extella.ai", rules_json="", fields_json="") -> dict:   # rules/fields — контракт F2 (заглушки: kp-стадия ищет нормы)
    import json
    from pathlib import Path

    def _b(v):
        return (not v) or str(v).startswith("{{")

    if _b(api_token):
        try:
            api_token = json.loads((Path.home() / "extella_wizard" / "app" / "config.json").read_text(encoding="utf-8")).get("auth_token", "")
        except Exception:
            api_token = ""

    data = {}
    try:
        if input_path and Path(input_path).exists():
            data = json.loads(Path(input_path).read_text(encoding="utf-8"))
    except Exception:
        data = {}

    q = ""
    if isinstance(data, dict):
        q = str(data.get("request") or data.get("event_type") or data.get("question") or data.get("_query") or "")
    if not q:
        q = "Найди релевантные нормы и статьи по документу: " + json.dumps(data, ensure_ascii=False)[:400]

    ctx = ""
    if api_token:
        try:
            body = {"name": "kp_ask", "params": {"name": "%(PACK)s", "question": q}, "global": True}
            if not _b(target):
                body["target"] = target
            req = urllib.request.Request(api_base.rstrip("/") + "/api/expert/run",
                                         data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                         headers={"X-Auth-Token": api_token, "Content-Type": "application/json",
                                                  "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"},
                                         method="POST")
            rr = json.loads(urllib.request.urlopen(req, timeout=180).read().decode("utf-8"))
            ctx = rr.get("result") or rr.get("output") or ""
            if isinstance(ctx, (dict, list)):
                ctx = json.dumps(ctx, ensure_ascii=False)
        except Exception as e:
            ctx = "[knowledge lookup unavailable: %%s]" %% str(e)[:120]

    out = dict(data) if isinstance(data, dict) else {"input": data}
    out["legal_context"] = ctx
    out["knowledge_pack"] = "%(PACK)s"
    if output_path:
        Path(output_path).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "success", "legal_context_len": len(str(ctx)), "pack": "%(PACK)s"}
'''


def _build_kp_stage(name, pack_id):
    """РЕЮЗ kp_ask: сохраняет тонкую обёртку-стадию (не кодоген). Возвращает (name|None, sv)."""
    code = _KP_STAGE_TEMPLATE % {"NAME": name, "PACK": pack_id}
    sv = api("/api/expert/save", {"name": name,
                                  "description": "Knowledge grounding stage (reuses kp_ask on pack '" + pack_id + "'): "
                                                 "finds relevant articles and adds legal_context for the next stage.",
                                  "code": code,
                                  "kwargs": {"input_path": "", "output_path": "", "target": "",
                                             "api_token": "", "api_base": "https://api.extella.ai"},
                                  "cspl": "fython", "global": True})
    ok = sv.get("status") == "success" or sv.get("id") is not None
    return (name if ok else None), sv


def _kp_install_on(pack_id, target):
    """Авто-установка базы знаний на устройство прогона (best-effort, не роняет сборку)."""
    try:
        body = {"name": "kp_install_pack", "params": {"pack_id": pack_id}, "global": True}
        if target:
            body["target"] = target
        return api("/api/expert/run", body, timeout=600)
    except Exception as e:
        return {"status": "error", "message": str(e)[:150]}


def _make_orchestrator(ns, stage_names, work_dir, session_id="", kp_stages=None):
    """Создаёт (external save → persist) вызываемый оркестратор процесса с вшитыми стадиями.
    session_id вшивается (%(SID)s) для резолвера файла из общего стора на хостинге.
    kp_stages — имена knowledge-стадий, которым оркестратор пробрасывает target+api_token (реюз kp_ask)."""
    name = ns + "_run_pipeline"
    code = _ORCH_TEMPLATE % {"NAME": name, "WORKDIR": work_dir,
                             "STAGES": json.dumps(stage_names, ensure_ascii=False),
                             "KP_STAGES": json.dumps(kp_stages or [], ensure_ascii=False),
                             "SID": session_id}
    sv = api("/api/expert/save", {"name": name,
                                  "description": "Auto-generated process orchestrator: runs the contract pipeline ("
                                                 + " -> ".join(stage_names) + ") on a source file, cleans headers, "
                                                 "returns summary and renders .md/.xlsx report. Params: source_file, work_dir, api_token, target, source_key.",
                                  "code": code, "kwargs": {"source_file": "", "work_dir": work_dir, "rules_json": "", "fields_json": "",
                                                           "api_token": "", "api_base": "https://api.extella.ai",
                                                           "target": "", "source_key": ""},
                                  "cspl": "fython", "global": True})
    ok = sv.get("status") == "success" or sv.get("id") is not None
    return (name if ok else None), sv


def _run_build(session_id, build_id):
    """Фоновая стройка процесса: план -> сборка задач -> аудит. Прогресс в build_progress.json."""
    bdir = RUNS_DIR / build_id
    bdir.mkdir(parents=True, exist_ok=True)
    prog = {"build_id": build_id, "session_id": session_id, "status": "running", "stages": []}

    def now():
        return datetime.now(timezone.utc).isoformat()

    def save():
        prog["updated_at"] = now()
        (bdir / "build_progress.json").write_text(json.dumps(prog, ensure_ascii=False, indent=2), encoding="utf-8")

    def stage(sid, title, status="running", **extra):
        for s in prog["stages"]:
            if s["id"] == sid:
                s["status"] = status
                s.update(extra)
                save()
                return
        prog["stages"].append({"id": sid, "title": title, "status": status, **extra})
        save()

    llm = {"api_key": CONFIG.get("llm_api_key", ""), "model": CONFIG.get("llm_model", ""),
           "base_url": CONFIG.get("llm_base_url", ""),
           "api_token": CONFIG.get("auth_token", ""), "api_base": BASE,
           # keyless-кодоген идёт на канонический Qwen (см. qwen_agent) — НЕ Claude-дефолт agent_extella_default (жёг бы баланс).
           "agent_id": qwen_agent()}
    tok = {"api_token": CONFIG["auth_token"]}

    # namespace: короткий snake-префикс для экспертов процесса (из имени клиента)
    try:
        _s = json.loads((SESS_DIR / (session_id + ".json")).read_text(encoding="utf-8"))
    except Exception:
        _s = {}
    _words = re.findall(r"[A-Za-z]+", _s.get("client_name", "") or "")
    if _words:
        ns = ("".join(w[0] for w in _words[:3]) if len("".join(w[0] for w in _words[:3])) >= 2 else _words[0][:3]).lower()
    else:
        ns = "p" + session_id.split("_")[-1][:3]
    ns = re.sub(r"[^a-z0-9]", "", ns)[:5] or "proc"

    try:
        # 1. План стройки
        stage("plan", "Составляю план стройки", "running")
        # ПЛАН строит ёмкая design-модель (fine-tune обрезает большой план); КОДОГЕН ниже — на fine-tune (llm).
        # план строит design-агент первым; при флапе — фолбэк по цепочке Qwen (run_llm_expert)
        r = run_llm_expert("wz_build_plan", dict(session_id=session_id, namespace=ns, **llm), wait=900,
                           agents=[design_agent()])
        if not isinstance(r, dict) or r.get("status") == "error":
            stage("plan", "Составляю план стройки", "error", error=str(r)[:300])
            prog["status"] = "error"; save(); return
        plan_path = SESS_DIR / (session_id + "_build_plan.json")
        if not plan_path.exists():
            stage("plan", "Составляю план стройки", "error", error="план не сохранился")
            prog["status"] = "error"; save(); return
        pdoc = json.loads(plan_path.read_text(encoding="utf-8"))
        plan = pdoc.get("plan", pdoc)
        tasks = plan.get("tasks", [])
        built_names = []
        stage("plan", "Составляю план стройки", "success", tasks_count=len(tasks))

        schema_hint, sample_file = _inspect_sample(session_id)

        # KNOWLEDGE-СТАДИЯ: из blueprint берём базу знаний (knowledge_pack) и какие стадии на неё опираются.
        # Такие стадии НЕ кодогеним — реюзаем готовый kp_ask (тонкая обёртка). Базу авто-ставим на target прогона.
        kp_pack = ""
        kp_stage_ids = set()
        try:
            _bp = json.loads((SESS_DIR / (session_id + "_blueprint.json")).read_text(encoding="utf-8")).get("blueprint", {})
            kp_pack = ((_bp.get("knowledge_pack") or {}).get("pack_id")) or ""
            for _st in _bp.get("stages", []):
                if "knowledge_grounding" in (_st.get("capability_ids") or []):
                    kp_stage_ids.add(_st.get("id"))
        except Exception:
            pass
        _KP_KW = ("норм", "кодекс", "статьи", "законодат", "knowledge", "kp_ask", "grounding", "правов", "trud", "grazhd", "nalog")

        def is_kp_task(t):
            if not kp_pack:
                return False
            if t.get("stage_id") in kp_stage_ids:
                return True
            blob = (str(t.get("expert_name", "")) + " " + str(t.get("purpose", "")) + " " + str(t.get("title", ""))).lower()
            return any(k in blob for k in _KP_KW)

        kp_stage_names = []
        if kp_pack:
            stage("kp_install", "Ставлю базу знаний: " + kp_pack, "running")
            _ki = _kp_install_on(kp_pack, HOST_TARGET)
            _kis = _ki.get("status", "?") if isinstance(_ki, dict) else "?"
            stage("kp_install", "База знаний " + kp_pack + " (" + str(_kis) + ")", "success", pack=kp_pack)  # best-effort

        # ДАТА-СТАДИИ конвейера (парсинг/анализ/отчёт) — строим ВСЕ заново под единый контракт
        # (реюз старых экспертов не по контракту рвёт цепочку). Не-дата задачи (расписание) — вне среза.
        def is_data_stage(t):
            nm = (t.get("expert_name") or "").lower()
            return not any(x in nm for x in ("schedule", "orchestr", "pipeline", "notif", "send", "email", "cron"))
        data_tasks = [t for t in tasks if is_data_stage(t)]
        other_tasks = [t for t in tasks if not is_data_stage(t)]

        for t in other_tasks:
            tid = t.get("id", "x")
            stage("task_" + tid, "Вне конвейера данных: " + (t.get("title") or t.get("expert_name") or tid),
                  "success", skipped=True)

        # 2. Сборка МОСТОМ по единому контракту + вертикальный срез на реальном файле:
        #    каждая дата-стадия принимает выход предыдущей (первая — исходный файл клиента).
        current_input = sample_file
        slice_ok = bool(sample_file)
        for idx, t in enumerate(data_tasks):
            tid = t.get("id", "t%d" % (idx + 1))
            title = t.get("title") or t.get("expert_name") or tid
            nm = t.get("expert_name") or (ns + "_" + tid)
            stage("task_" + tid, "Собираю и проверяю: " + title, "running")
            if not current_input:
                stage("task_" + tid, "Ошибка: " + title, "error", expert=nm,
                      detail="нет входа для стадии (не приложен файл-образец?)")
                slice_ok = False
                break
            if is_kp_task(t):
                # РЕЮЗ kp_ask: сохраняем тонкую обёртку и прогоняем на входе. На build-срезе без target
                # обёртка мягко деградирует (пустой legal_context) — на run-time оркестратор даёт target.
                kp_stage_names.append(nm)
                nm2, sv = _build_kp_stage(nm, kp_pack)
                outp = str(bdir / (nm + "_out.json"))
                if not nm2:
                    ok, detail = False, "kp-обёртка не сохранилась: " + str(sv)[:120]
                else:
                    try:
                        api("/api/expert/run", {"name": nm, "params": {"input_path": current_input, "output_path": outp},
                                                "global": True}, timeout=200)
                    except Exception:
                        pass
                    if not Path(outp).exists():
                        try:
                            import shutil as _sh
                            _sh.copy(current_input, outp)  # passthrough: срез продолжается, retrieval — на run-time
                        except Exception:
                            outp = current_input
                    ok, detail = True, "реюз kp_ask(" + kp_pack + ")"
            else:
                ok, outp, detail = _build_one(nm, t, schema_hint, is_first=(idx == 0),
                                              is_last=(idx == len(data_tasks) - 1),
                                              accept_input=current_input, llm=llm)
            if ok:
                built_names.append(nm)
                current_input = outp  # выход стадии = вход следующей (это и есть срез)
            else:
                slice_ok = False
            stage("task_" + tid, ("Собрано+прогнано: " if ok else "Ошибка: ") + title,
                  "success" if ok else "error", expert=nm, detail=str(detail)[:200])
            if not ok:
                break

        # итог среза: последний output = сводка
        slice_summary = None
        if slice_ok and current_input and current_input != sample_file and Path(current_input).exists():
            try:
                sdata = json.loads(Path(current_input).read_text(encoding="utf-8"))
                slice_summary = sdata.get("summary") if isinstance(sdata, dict) else {"records": len(sdata)}
                prog["slice_output"] = current_input
                prog["slice_summary"] = slice_summary
            except Exception:
                pass

        built_ok = [n for n in built_names if n]
        had_build_tasks = any(str(t.get("action", "build")).lower() != "reuse" for t in tasks)
        if had_build_tasks and not built_ok:
            prog["status"] = "error"
            prog["error"] = "ни один компонент не собрался (сборщик не смог пройти приёмку — вероятно, нужен файл-образец)"
            save(); return

        # 3. Автосоздание вызываемого оркестратора процесса (стадии — построенные дата-эксперты)
        orchestrator = None
        stage_experts = [t.get("expert_name") or (ns + "_" + t.get("id", "")) for t in data_tasks]
        stage_experts = [n for n in stage_experts if n in built_ok]
        if stage_experts:
            stage("orchestrator", "Собираю оркестратор процесса", "running")
            orchestrator, _sv = _make_orchestrator(ns, stage_experts, "/tmp/" + ns + "_run", session_id,
                                                   kp_stages=[n for n in kp_stage_names if n in built_ok])
            stage("orchestrator", "Оркестратор процесса: " + (orchestrator or "ошибка"),
                  "success" if orchestrator else "error", expert=orchestrator)
            if orchestrator:
                prog["orchestrator"] = orchestrator

        # 4. Аудит перед запуском
        stage("audit", "Проверяю процесс перед запуском", "running")
        aud = _audit_experts([n for n in built_names if n])
        prog["audit"] = aud
        prog["built_experts"] = [n for n in built_names if n]
        stage("audit", "Проверяю процесс перед запуском", "success",
              verdict=aud["verdict"], issues=aud["issues"])

        prog["status"] = "built"
        save()
        # отметка в сессии
        try:
            sp = SESS_DIR / (session_id + ".json")
            s = json.loads(sp.read_text(encoding="utf-8"))
            s["stage"] = "built"
            s.setdefault("builds", []).append({"build_id": build_id, "at": now(),
                                               "experts": prog["built_experts"], "audit": aud,
                                               "params_contract": 1,   # F2: оркестратор+стадии принимают rules_json/fields_json
                                               "orchestrator": orchestrator,
                                               "slice_summary": prog.get("slice_summary"),
                                               "source_file": sample_file})
            # §7bis ступень 3: автопанель — новая автоматизация выходит из Строителя с готовой формой
            # настроек (best-effort; если Qwen моргнул — владелец соберёт кнопкой в кабинете).
            if not s.get("panel_manifest"):
                try:
                    _bpf = SESS_DIR / (session_id + "_blueprint.json")
                    _bpm = json.loads(_bpf.read_text(encoding="utf-8")).get("blueprint", {}) if _bpf.exists() else {}
                    _mani = gen_panel_manifest(_bpm.get("goal") or _bpm.get("summary") or "", _bpm.get("stages") or [])
                    if _mani:
                        s["panel_manifest"] = dict(_mani, generated_at=now())
                except Exception:
                    pass
            s["updated_at"] = now()
            sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    except Exception as e:
        prog["status"] = "error"; prog["error"] = str(e)[:300]; save()
