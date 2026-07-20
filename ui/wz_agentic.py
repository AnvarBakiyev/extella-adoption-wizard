"""Агентная стройка Визарда: решить задачу целиком -> прогнать -> исправить -> доказать.

Этот слой намеренно НЕ дробит бизнес-задачу на экспертов заранее. Qwen получает полный Task Package
и профили всех образцов, создаёт одного исполняемого эксперта, а харнесс независимо запускает его и
проверяет факты результата. Декомпозиция остаётся оптимизацией после работающего решения.
"""
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from wz_platform import BASE, CONFIG, api, qwen_agent, run_expert
from wz_llm import design_agent


EXPERT_KWARGS = {
    "source_file": "", "output_dir": "", "api_token": "", "api_base": "https://api.extella.ai",
    "target": "", "source_key": "", "rules_json": "", "fields_json": "", "run_id": "",
    "placement_json": "", "adapter_json": "", "report_spec_json": "",
}
REPORT_KEYS = ("report_md", "report_xlsx", "report_pdf", "report_docx", "report_pptx")


def _clip(value, limit=400):
    s = str(value if value is not None else "").replace("\x00", "").strip()
    return s if len(s) <= limit else s[:limit] + "…"


def _compact(value, depth=0):
    """Ограниченный JSON-контекст: сохраняет структуру ТЗ, не отправляет модели бесконечные логи."""
    if depth > 6:
        return _clip(value, 300)
    if isinstance(value, dict):
        return {str(k)[:80]: _compact(v, depth + 1) for k, v in list(value.items())[:80]}
    if isinstance(value, list):
        return [_compact(v, depth + 1) for v in value[:60]]
    if isinstance(value, str):
        return _clip(value, 1800)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return _clip(value, 500)


def _sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _pdf_text(path):
    """Текст первых страниц; для скана best-effort OCR. Отсутствие движка — факт профиля, не успех."""
    text, source = "", ""
    for mod in ("pypdf", "PyPDF2"):
        try:
            pkg = __import__(mod)
            reader = pkg.PdfReader(str(path))
            text = "\n".join((p.extract_text() or "") for p in reader.pages[:5])
            if text.strip():
                source = mod
                break
        except Exception:
            continue
    if not text.strip() and shutil.which("pdftotext"):
        try:
            cp = subprocess.run(["pdftotext", "-f", "1", "-l", "5", str(path), "-"],
                                capture_output=True, text=True, timeout=35)
            text = cp.stdout or ""
            if text.strip():
                source = "pdftotext"
        except Exception:
            pass
    if not text.strip() and shutil.which("pdftoppm") and shutil.which("tesseract"):
        try:
            with tempfile.TemporaryDirectory(prefix="wz_pdf_") as td:
                pref = str(Path(td) / "page")
                subprocess.run(["pdftoppm", "-f", "1", "-l", "2", "-r", "150", "-png", str(path), pref],
                               capture_output=True, timeout=60)
                parts = []
                for image in sorted(Path(td).glob("page-*.png")):
                    cp = subprocess.run(["tesseract", str(image), "stdout", "-l", "rus+eng"],
                                        capture_output=True, text=True, timeout=60)
                    parts.append(cp.stdout or "")
                text = "\n".join(parts)
                if text.strip():
                    source = "tesseract"
        except Exception:
            pass
    return _clip(text, 9000), source or "unavailable_or_scan"


def profile_file(path):
    """Фактический, ограниченный профиль входа для рассуждения Qwen и независимой приёмки."""
    p = Path(path)
    out = {"name": p.name, "extension": p.suffix.lower(), "bytes": p.stat().st_size,
           "sha256": _sha256(p)}
    ext = p.suffix.lower()
    try:
        if ext in (".xlsx", ".xlsm"):
            import openpyxl
            wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
            sheets = []
            for ws in wb.worksheets[:4]:
                rows = []
                for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row or 1, 20), values_only=True):
                    rows.append([_clip(v, 180) for v in list(row)[:30]])
                sheets.append({"title": ws.title, "max_row": ws.max_row, "max_column": ws.max_column,
                               "sample_rows": rows})
            wb.close()
            out["workbook"] = sheets
        elif ext == ".csv":
            import csv
            with p.open("r", encoding="utf-8", errors="replace", newline="") as fh:
                rows = []
                for i, row in enumerate(csv.reader(fh)):
                    rows.append([_clip(v, 180) for v in row[:30]])
                    if i >= 19:
                        break
            out["sample_rows"] = rows
        elif ext == ".pdf":
            text, source = _pdf_text(p)
            out["text_sample"], out["text_source"] = text, source
        elif ext in (".txt", ".md", ".json", ".xml", ".html"):
            out["text_sample"] = _clip(p.read_text(encoding="utf-8", errors="replace"), 9000)
    except Exception as exc:
        out["profile_error"] = _clip(exc, 300)
    return out


def _read_json(path, fallback=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return fallback


def make_task_package(session_id, sample_files, sess_dir):
    """Единый источник контекста: интервью + blueprint + ТЗ + решения + правила + все образцы."""
    root = Path(sess_dir)
    session = _read_json(root / (session_id + ".json"), {}) or {}
    bpdoc = _read_json(root / (session_id + "_blueprint.json"), {}) or {}
    pdoc = _read_json(root / (session_id + "_build_plan.json"), {}) or {}
    chat = _read_json(root / (session_id + "_chat.json"), {}) or {}
    spec_path = root / (session_id + "_spec.md")
    spec = spec_path.read_text(encoding="utf-8", errors="replace")[:24000] if spec_path.exists() else ""
    profiles = [profile_file(p) for p in sample_files]
    package = {
        "contract_version": 1,
        "session_id": session_id,
        "client_name": session.get("client_name"),
        "language": session.get("language") or "ru",
        "original_request": session.get("questionnaire_task") or session.get("goal") or "",
        "interview_answers": session.get("answers") or {},
        "owner_comments": session.get("comments") or [],
        "decisions": session.get("decisions") or [],
        "rules": session.get("rules") or [],
        "fields": session.get("fields") or {},
        "permissions": session.get("permissions") or session.get("authority") or {},
        "source": session.get("source") or {},
        "schedule": session.get("schedule") or {},
        "recipients": session.get("recipients") or [],
        "data_check": session.get("data_check") or {},
        "blueprint": bpdoc.get("blueprint", bpdoc),
        "project_spec": spec,
        "build_plan": pdoc.get("plan", pdoc),
        "assistant_context": chat,
        "inputs": profiles,
        "runtime_input_contract": {
            "source_file": "путь к одному файлу ИЛИ папке-пакету; для нескольких входов обрабатывается вся папка",
            "output_dir": "отдельная папка для результатов; входные файлы нельзя изменять",
        },
        "acceptance_contract": {
            "must_use_every_sample": True,
            "must_run_on_real_samples": True,
            "must_return": ["status=success", "summary", "evidence.files_used", "evidence.acceptance_checks",
                            "report_md", "report_xlsx"],
            "no_external_writes": True,
        },
    }
    package = _compact(package)
    # Эти части уже ограничены профилировщиком/чтением файла. Общий _compact режет любую строку до
    # 1800 символов, но для Builder это как раз самая важная фактура: полный текст ТЗ и PDF-фрагмент.
    package["project_spec"] = _clip(spec, 12000)
    package["inputs"] = profiles
    raw = json.dumps(package, ensure_ascii=False, sort_keys=True, default=str)
    package["package_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return package


def _available_capabilities():
    """Проверенные плагины/блоки Композитора — Qwen должна уметь реюзить их внутри решения."""
    try:
        got = api("/api/kv/get", {"key": "composer:catalog", "global": True}, timeout=60)
        raw = got.get("value") if isinstance(got, dict) else None
        catalog = json.loads(raw) if raw else {}
    except Exception:
        return []
    out = []
    for block in (catalog.get("blocks") or catalog.get("items") or [])[:160]:
        if not isinstance(block, dict):
            continue
        name = str(block.get("id") or block.get("expert") or block.get("name") or "").strip()
        if not name:
            continue
        out.append({"expert": name, "kind": block.get("kind"),
                    "purpose": _clip(block.get("what") or block.get("description") or block.get("title"), 280),
                    "params": _compact(block.get("params") or block.get("defaults") or {})})
    return out


def _attach_capabilities(package):
    package = dict(package or {})
    package["available_plugins_and_experts"] = _available_capabilities()
    raw = json.dumps({k: v for k, v in package.items() if k != "package_sha256"},
                     ensure_ascii=False, sort_keys=True, default=str)
    package["package_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return package


def _agent_headers(agent_id):
    return {"X-Auth-Token": CONFIG.get("auth_token", ""), "Content-Type": "application/json",
            "X-Profile-Id": "default", "X-Agent-Id": agent_id or qwen_agent()}


def _post_agent(agent_id, payload, timeout=700):
    req = urllib.request.Request(BASE.rstrip("/") + "/api/agent/run",
                                 data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers=_agent_headers(agent_id), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"status": "error", "message": _clip(exc, 400)}


def _agent_text(response):
    text = ""
    for item in (response or {}).get("output", []):
        if isinstance(item, dict) and item.get("type") == "message":
            for content in item.get("content", []):
                if isinstance(content, dict) and content.get("type") == "output_text":
                    text += str(content.get("text") or "")
    return text or str((response or {}).get("output_text") or "")


def _build_prompt(expert_name, task_package, feedback, agent_id):
    package_json = json.dumps(task_package, ensure_ascii=False, indent=2, default=str)
    repair = ""
    if feedback:
        repair = ("\n\nПРЕДЫДУЩИЙ РЕАЛЬНЫЙ ПРОГОН НЕ ПРОШЁЛ. Не спорь с проверкой: найди причину, обнови "
                  "существующего эксперта и снова сохрани его. Фактура:\n" + _clip(feedback, 7000))
    return f"""Ты — Строитель Extella. Твоя задача — не описать решение, а СОЗДАТЬ ИЛИ ОБНОВИТЬ
одного реально исполняемого эксперта `{expert_name}` действием платформы.

Сначала рассмотри ТЗ и ВСЕ профили файлов как одну задачу. Сам выбери алгоритм, библиотеки и внутренние
этапы. Не дроби решение на внешние эксперты до того, как оно доказано целиком.
Если в available_plugins_and_experts есть проверенная способность, подходящая задаче, можешь вызвать её
через Extella API из эксперта вместо повторной реализации. Финальная точка запуска всё равно одна.

Обязательный контракт кода:
- CSPL=fython, ровно одна top-level функция; любые helpers только внутри неё;
- сигнатура РОВНО:
  def {expert_name}(source_file="", output_dir="", api_token="", api_base="https://api.extella.ai", target="", source_key="", rules_json="", fields_json="", run_id="", placement_json="", adapter_json="", report_spec_json="")
- source_file бывает файлом или папкой. Если это папка, найди и используй ВСЕ относящиеся к задаче файлы;
- искать входы можно ТОЛЬКО внутри source_file. Никаких fallback-поисков в cwd, home, /tmp, /data
  и других каталогах: если нужного файла нет в source_file, верни точную ошибку;
- входы не изменять; результаты писать только в output_dir;
- никаких абсолютных путей, секретов, shell-команд, отправки писем и записей во внешние системы;
- клиентские правила брать из rules_json/fields_json, а не зашивать значениями образца;
- ЗАПРЕЩЕНО помещать в код значения строк из образцов/контрольных кейсов (идентификаторы, суммы, даты,
  ожидаемые A/B/C). Образцы нужны только для проверки; алгоритм обязан работать на следующих файлах;
- для PDF сначала извлеки текст; если это скан — используй OCR (pypdf/pdf2image/pytesseract с include);
- если нужен смысловой шаг, разрешена платформенная Qwen через api_token; agent_id={agent_id};
- на этапе создания ТОЛЬКО сохрани/обнови эксперта. Не запускай его сам и не делай пробных вызовов:
  Визард отдельно прогонит сохранённый код с явными source_file и output_dir;
- обязательно создай человекочитаемые report.md и report.xlsx;
- верни dict:
  {{"status":"success","summary":{{...,"processed_files":N,"total_count":N}},
    "evidence":{{"files_used":["basename",...],"acceptance_checks":[
      {{"criterion":"...","passed":true,"evidence":"конкретный факт"}}]}},
    "report_md":"/absolute/generated/report.md","report_xlsx":"/absolute/generated/report.xlsx"}}
- status=success только если бизнес-цель выполнена на данных; при невозможности верни status=error и точную причину.

Исходный код помещай ТОЛЬКО в действие создания/обновления эксперта, не печатай код в ответе.
После действия ответь кратко, но источником истины будет сохранённый эксперт и его реальный прогон.

TASK PACKAGE:
{package_json}{repair}"""


def _sample_literals(task_package):
    """Отличительные значения СТРОК образца, которым запрещено просачиваться в исходник решения."""
    values = set()
    for profile in (task_package or {}).get("inputs") or []:
        for sheet in profile.get("workbook") or []:
            rows = sheet.get("sample_rows") or []
            for row in rows[1:]:  # первая строка — схема; имена колонок коду знать разрешено
                for cell in row:
                    s = str(cell or "").strip()
                    if 4 <= len(s) <= 100 and (re.search(r"\d", s) or len(s.split()) >= 2):
                        values.add(s)
        rows = profile.get("sample_rows") or []
        for row in rows[1:]:
            for cell in row:
                s = str(cell or "").strip()
                if 4 <= len(s) <= 100 and (re.search(r"\d", s) or len(s.split()) >= 2):
                    values.add(s)
        text = str(profile.get("text_sample") or "")
        for token in re.findall(r"(?<!\w)[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9_.\-/]{3,}(?!\w)", text):
            if re.search(r"\d", token):
                values.add(token)
    return sorted(values, key=lambda x: (-len(x), x))[:120]


def _validate_code(expert_name, code, task_package=None):
    issues = []
    tops = re.findall(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", code or "", flags=re.M)
    if tops != [expert_name]:
        issues.append("нужна одна top-level функция %s, найдено %s" % (expert_name, tops))
    sig = re.search(r"^def\s+" + re.escape(expert_name) + r"\s*\((.*?)\)\s*(?:->[^:]+)?\s*:",
                    code or "", flags=re.M | re.S)
    if not sig or "source_file" not in sig.group(1) or "output_dir" not in sig.group(1):
        issues.append("сигнатура не содержит source_file/output_dir")
    for marker in ("/Users/", "/home/", "os.system(", "shell=True",
                   "Path.cwd(", "Path.home(", "os.getcwd(", "Path('/tmp')", 'Path("/tmp")',
                   "Path('/data')", 'Path("/data")'):
        if marker in (code or ""):
            issues.append("запрещённый маркер в коде: " + marker)
    leaked = [v for v in _sample_literals(task_package or {}) if v.casefold() in (code or "").casefold()]
    if leaked:
        issues.append("в код зашиты значения образца: " + ", ".join(leaked[:8]))
    return issues


def _get_scoped_expert(expert_name, agent_id):
    req = urllib.request.Request(BASE.rstrip("/") + "/api/expert/get",
                                 data=json.dumps({"name": expert_name}).encode("utf-8"),
                                 headers=_agent_headers(agent_id), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=70) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return {}


def _create_or_update(expert_name, task_package, feedback, llm):
    """Нативная Qwen-стройка с последующим независимым get и усыновлением в global."""
    agent_id = llm.get("agent_id") or qwen_agent()
    prompt = _build_prompt(expert_name, task_package, feedback, agent_id)
    if llm.get("api_key"):
        try:
            body = {"model": llm.get("model"), "temperature": 0,
                    "response_format": {"type": "json_object"}, "max_tokens": 8000,
                    "messages": [{"role": "system", "content": "Верни JSON {code,description}."},
                                 {"role": "user", "content": prompt.replace(
                                     "Исходный код помещай ТОЛЬКО в действие создания/обновления эксперта, не печатай код в ответе.",
                                     "Верни исходный код в поле code JSON.")} ]}
            req = urllib.request.Request(llm.get("base_url", "").rstrip("/") + "/chat/completions",
                                         data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                         headers={"Authorization": "Bearer " + llm["api_key"],
                                                  "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=300) as response:
                raw = json.loads(response.read().decode("utf-8"))["choices"][0]["message"]["content"]
            spec = json.loads(raw)
            found = {"expert_code": spec.get("code", ""), "description": spec.get("description", "")}
        except Exception as exc:
            return {"ok": False, "why": "LLM не создала код: " + _clip(exc, 300)}
    else:
        run = _post_agent(agent_id, {"agent_id": agent_id, "input": prompt, "run_timeout": 650,
                                     "store": True, "max_output_tokens": 3000}, timeout=700)
        found = _get_scoped_expert(expert_name, agent_id)
        if not found or not (found.get("expert_code") or found.get("code")):
            found = api("/api/expert/get", {"name": expert_name, "global": True}, timeout=70)
        if not found or not (found.get("expert_code") or found.get("code")):
            return {"ok": False, "why": "Qwen не сохранила эксперта: " + _clip(run, 500)}
    code = str(found.get("expert_code") or found.get("code") or "")
    issues = _validate_code(expert_name, code, task_package)
    if issues:
        return {"ok": False, "why": "; ".join(issues)}
    description = str(found.get("description") or "Целостное решение бизнес-задачи, проверенное на образцах")[:900]
    saved = api("/api/expert/save", {"name": expert_name, "description": description, "code": code,
                                      "kwargs": dict(EXPERT_KWARGS), "cspl": "fython", "global": True}, timeout=180)
    ok = isinstance(saved, dict) and (saved.get("status") == "success" or saved.get("id"))
    return {"ok": bool(ok), "why": "" if ok else "global save: " + _clip(saved, 400),
            "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest()}


def _delete_draft(expert_name, agent_id=""):
    """Убрать временного эксперта из global и из скоупа Строителя (best effort)."""
    try:
        api("/api/expert/delete", {"name": expert_name, "global": True}, timeout=40)
    except Exception:
        pass
    if agent_id:
        try:
            req = urllib.request.Request(BASE.rstrip("/") + "/api/expert/delete",
                                         data=json.dumps({"name": expert_name}).encode("utf-8"),
                                         headers=_agent_headers(agent_id), method="POST")
            urllib.request.urlopen(req, timeout=40).read()
        except Exception:
            pass


def _promote_expert(draft_name, stable_name, task_package):
    """Атомарно опубликовать принятую draft-версию под стабильным именем процесса."""
    found = api("/api/expert/get", {"name": draft_name, "global": True}, timeout=70)
    code = str((found or {}).get("expert_code") or (found or {}).get("code") or "")
    if not code:
        return {"ok": False, "why": "принятый draft не найден перед публикацией"}
    code = re.sub(r"\b" + re.escape(draft_name) + r"\b", stable_name, code)
    issues = _validate_code(stable_name, code, task_package)
    if issues:
        return {"ok": False, "why": "; ".join(issues)}
    saved = api("/api/expert/save", {"name": stable_name,
                                      "description": str((found or {}).get("description") or
                                                         "Целостное решение бизнес-задачи, принятое Визардом")[:900],
                                      "code": code, "kwargs": dict(EXPERT_KWARGS),
                                      "cspl": "fython", "global": True}, timeout=180)
    ok = isinstance(saved, dict) and (saved.get("status") == "success" or saved.get("id"))
    return {"ok": bool(ok), "why": "" if ok else "stable save: " + _clip(saved, 400),
            "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest()}


def _artifact_paths(result):
    out = []
    if not isinstance(result, dict):
        return out
    for key in REPORT_KEYS:
        if isinstance(result.get(key), str):
            out.append((key, result[key]))
    for item in result.get("artifacts") or []:
        if isinstance(item, str):
            out.append(("artifact", item))
        elif isinstance(item, dict) and isinstance(item.get("path"), str):
            out.append((str(item.get("kind") or "artifact"), item["path"]))
    seen, clean = set(), []
    for key, path in out:
        if path not in seen:
            seen.add(path); clean.append((key, path))
    return clean


def validate_result(result, sample_files):
    """Детерминированная часть приёмки: модель не может позеленить отсутствующие входы/отчёты."""
    issues = []
    if not isinstance(result, dict):
        return {"ok": False, "issues": ["результат не является объектом"]}
    if result.get("status") != "success":
        issues.append("status не success: " + _clip(result.get("message") or result.get("status"), 300))
    summary = result.get("summary")
    if not isinstance(summary, dict) or len(summary) < 2:
        issues.append("нет содержательной summary")
    evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
    used = evidence.get("files_used") or result.get("files_used") or []
    used = {Path(str(x)).name.casefold() for x in used if str(x).strip()}
    expected = {Path(str(x)).name.casefold() for x in sample_files}
    missing = sorted(expected - used)
    if missing:
        issues.append("нет доказательства обработки файлов: " + ", ".join(missing))
    processed = (summary or {}).get("processed_files") if isinstance(summary, dict) else None
    try:
        if int(processed) < len(expected):
            issues.append("processed_files меньше числа входов")
    except Exception:
        issues.append("summary.processed_files не задан")
    checks = evidence.get("acceptance_checks") or result.get("acceptance_checks") or []
    if not isinstance(checks, list) or not checks:
        issues.append("нет evidence.acceptance_checks")
    elif any(not isinstance(c, dict) or c.get("passed") is not True or not str(c.get("evidence") or "").strip()
             for c in checks):
        issues.append("не все критерии приёмки подтверждены конкретными фактами")
    artifacts = _artifact_paths(result)
    live = []
    for key, path in artifacts:
        try:
            p = Path(path)
            if p.is_file() and p.stat().st_size >= 80:
                live.append((key, str(p), p.stat().st_size))
        except Exception:
            continue
    if not any(k == "report_md" for k, _, _ in live):
        issues.append("не создан открываемый report_md")
    if not any(k == "report_xlsx" for k, _, _ in live):
        issues.append("не создан открываемый report_xlsx")
    return {"ok": not issues, "issues": issues, "artifacts": live,
            "files_used": sorted(used), "summary": summary if isinstance(summary, dict) else {}}


def _result_preview(result, validation):
    preview = {"result": _compact(result), "artifact_previews": []}
    for key, path, size in (validation.get("artifacts") or [])[:5]:
        item = {"kind": key, "name": Path(path).name, "bytes": size}
        try:
            if Path(path).suffix.lower() in (".md", ".txt", ".html"):
                item["text"] = _clip(Path(path).read_text(encoding="utf-8", errors="replace"), 7000)
            elif Path(path).suffix.lower() in (".xlsx", ".xlsm"):
                item["profile"] = profile_file(path).get("workbook")
        except Exception as exc:
            item["preview_error"] = _clip(exc, 200)
        preview["artifact_previews"].append(item)
    return _compact(preview)


def judge_result(task_package, result, validation):
    """Независимый смысловой гейт: статус эксперта сам по себе не доказывает бизнес-результат."""
    agent_id = design_agent() or qwen_agent()
    prompt = ("Ты независимый приёмщик результата автоматизации. Не доверяй полю status и самооценке автора. "
              "Сопоставь ТЗ, фактические профили входов, summary, evidence и превью отчётов. "
              "PASS только если основная бизнес-цель выполнена на всех образцах и это доказано конкретными данными. "
              "Если в ТЗ не хватает критического бизнес-выбора, FAIL и сформулируй один точный вопрос владельцу. "
              "Верни ТОЛЬКО JSON: "
              '{"verdict":"pass|fail","confidence":0.0,"issues":["..."],"owner_question":""}.\n\n'
              "TASK PACKAGE:\n" + json.dumps(_compact(task_package), ensure_ascii=False, default=str) +
              "\n\nSTRUCTURAL CHECK:\n" + json.dumps(_compact(validation), ensure_ascii=False, default=str) +
              "\n\nACTUAL RESULT:\n" + json.dumps(_result_preview(result, validation), ensure_ascii=False, default=str))
    response = _post_agent(agent_id, {"agent_id": agent_id, "input": prompt, "run_timeout": 180,
                                      "store": False, "temperature": 0, "max_output_tokens": 2200}, timeout=210)
    text = _agent_text(response)
    try:
        match = re.search(r"\{.*\}", text, re.S)
        verdict = json.loads(match.group(0) if match else text)
    except Exception:
        return {"verdict": "fail", "confidence": 0, "issues": ["приёмщик не вернул валидный JSON"],
                "owner_question": ""}
    if verdict.get("verdict") not in ("pass", "fail"):
        verdict["verdict"] = "fail"
    try:
        verdict["confidence"] = max(0.0, min(1.0, float(verdict.get("confidence", 0))))
    except Exception:
        verdict["confidence"] = 0.0
    verdict["issues"] = [str(x)[:300] for x in (verdict.get("issues") or [])[:8]]
    verdict["owner_question"] = str(verdict.get("owner_question") or "")[:500]
    return verdict


def build_agentic_solution(session_id, build_id, namespace, sample_files, sess_dir, runs_dir, llm,
                            progress=None, max_attempts=4):
    """Главный цикл. Возвращает ok только после реального прогона + двух независимых гейтов."""
    progress = progress or (lambda *args, **kwargs: None)
    bdir = Path(runs_dir) / build_id
    bdir.mkdir(parents=True, exist_ok=True)
    progress("agentic_context", "Собираю полное ТЗ и изучаю все файлы", "running", "")
    package = _attach_capabilities(make_task_package(session_id, sample_files, sess_dir))
    (bdir / "task_package.json").write_text(json.dumps(package, ensure_ascii=False, indent=2, default=str),
                                             encoding="utf-8")
    progress("agentic_context", "Полное ТЗ и файлы собраны в один контекст", "success",
             "образцов: %d · контракт %s" % (len(sample_files), package.get("package_sha256", "")[:10]))
    expert_name = namespace + "_run_process"
    # Кандидаты не имеют права перезаписывать уже работающую версию процесса. Стабильное имя
    # обновляется только ПОСЛЕ реального прогона и бизнес-приёмки; красная стройка удаляет draft.
    draft_name = expert_name + "__draft_" + hashlib.sha256(str(build_id).encode("utf-8")).hexdigest()[:8]
    source_file = str(Path(sample_files[0]).parent if len(sample_files) > 1 else Path(sample_files[0]))
    attempts, feedback = [], ""
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        progress("agentic_reason", "Qwen решает задачу целиком · попытка %d/%d" % (attempt, max_attempts),
                 "running", "не дроблю процесс до рабочего результата")
        built = _create_or_update(draft_name, package, feedback, llm)
        if not built.get("ok"):
            rec = {"attempt": attempt, "phase": "build", "issues": [built.get("why") or "эксперт не создан"]}
            attempts.append(rec)
            feedback = json.dumps(rec, ensure_ascii=False)
            progress("agentic_reason", "Qwen не сохранила рабочее решение · исправляю", "warn", feedback)
            continue
        progress("agentic_reason", "Целостный эксперт создан", "success", draft_name)
        outdir = bdir / ("solution_attempt_%d" % attempt)
        outdir.mkdir(parents=True, exist_ok=True)
        progress("agentic_run", "Запускаю решение на всех реальных образцах", "running",
                 ", ".join(Path(p).name for p in sample_files))
        params = {"source_file": source_file, "output_dir": str(outdir),
                  "api_token": CONFIG.get("auth_token", ""), "api_base": BASE,
                  "rules_json": json.dumps(package.get("rules") or [], ensure_ascii=False),
                  "fields_json": json.dumps(package.get("fields") or {}, ensure_ascii=False)}
        result = run_expert(draft_name, params, wait=900, glob=True)
        validation = validate_result(result, sample_files)
        if validation["ok"]:
            progress("agentic_run", "Решение отработало на всех образцах", "success",
                     "файлов: %d · отчётов: %d" % (len(sample_files), len(validation.get("artifacts") or [])))
            progress("agentic_accept", "Проверяю бизнес-результат по ТЗ", "running", "")
            judge = judge_result(package, result, validation)
        else:
            judge = {"verdict": "fail", "confidence": 1.0, "issues": validation["issues"],
                     "owner_question": ""}
            progress("agentic_run", "Реальный прогон не прошёл · возвращаю ошибку Qwen", "warn",
                     "; ".join(validation["issues"])[:500])
        rec = {"attempt": attempt, "phase": "accept", "code_sha256": built.get("code_sha256"),
               "validation": validation, "judge": judge, "result": _compact(result)}
        attempts.append(rec)
        if validation["ok"] and judge.get("verdict") == "pass" and judge.get("confidence", 0) >= 0.7:
            promoted = _promote_expert(draft_name, expert_name, package)
            if not promoted.get("ok"):
                feedback = json.dumps({"publish_error": promoted.get("why")}, ensure_ascii=False)
                progress("agentic_accept", "Принятое решение не опубликовалось · повторяю", "warn",
                         promoted.get("why") or "ошибка публикации")
                continue
            _delete_draft(draft_name, llm.get("agent_id") or qwen_agent())
            progress("agentic_accept", "Бизнес-результат подтверждён", "success",
                     "уверенность приёмки: %d%%" % round(judge.get("confidence", 0) * 100))
            evidence = {"contract_version": 1, "built_at": datetime.now(timezone.utc).isoformat(),
                        "package_sha256": package.get("package_sha256"), "expert": expert_name,
                        "source_files": [{"name": Path(p).name, "sha256": _sha256(p)} for p in sample_files],
                        "attempts": attempts, "accepted_attempt": attempt}
            (bdir / "agentic_evidence.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            return {"ok": True, "expert": expert_name, "source_file": source_file,
                    "summary": validation.get("summary") or {}, "result": result,
                    "validation": validation, "judge": judge, "attempts": attempts,
                    "package_sha256": package.get("package_sha256"), "source_files": [Path(p).name for p in sample_files]}
        feedback = json.dumps({"actual_run": _compact(result), "structural_issues": validation["issues"],
                               "business_judge": judge}, ensure_ascii=False, default=str)
        progress("agentic_accept", "Приёмка нашла несоответствия · Qwen исправляет", "warn",
                 "; ".join(judge.get("issues") or validation["issues"])[:500])
        time.sleep(min(2 * attempt, 5))
    last = attempts[-1] if attempts else {}
    judge = last.get("judge") or {}
    issues = judge.get("issues") or last.get("issues") or ["решение не прошло приёмку"]
    _delete_draft(draft_name, llm.get("agent_id") or qwen_agent())
    return {"ok": False, "code": "agentic_acceptance_failed", "expert": expert_name,
            "detail": "; ".join(str(x) for x in issues)[:800],
            "owner_question": judge.get("owner_question") or "", "attempts": attempts,
            "package_sha256": package.get("package_sha256")}
