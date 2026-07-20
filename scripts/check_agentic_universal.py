#!/usr/bin/env python3
"""Универсальная матрица Task Contract → Source Model → draft/run/repair/memory.

Живой Qwen здесь намеренно заменён детерминированными mocks: тест доказывает инварианты харнесса,
а не стабильность внешнего провайдера или конкретные слова одной отрасли.
"""
import importlib.util
import json
import sys
import tempfile
import types
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "ui" / "wz_agentic.py"


def load_module():
    platform = types.ModuleType("wz_platform")
    platform.BASE = "https://example.invalid"
    platform.CONFIG = {"auth_token": "test"}
    platform.api = lambda *a, **k: {}
    platform.qwen_agent = lambda: "agent_qwen"
    platform.run_expert = lambda *a, **k: {}
    llm = types.ModuleType("wz_llm")
    llm.design_agent = lambda: "agent_judge"
    sys.modules["wz_platform"] = platform
    sys.modules["wz_llm"] = llm
    spec = importlib.util.spec_from_file_location("wz_agentic_universal", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def profile(name, sections, extension=".xlsx"):
    return {"name": name, "extension": extension, "bytes": 100, "sha256": name + "-sha",
            "workbook": [{"title": s[0], "max_row": 4, "max_column": len(s[1]), "header_row": 1,
                          "columns": s[1], "sample_rows": [s[1], s[2] if len(s) > 2 else ["x"]]}
                         for s in sections]}


def package(inputs, goal="Обработать входы и доказать результат", capabilities=None):
    contract = {"version": 1, "business_goal": goal, "original_request": goal,
                "required_result": {"success_criteria": ["результат доказан"]},
                "inputs": [{"name": x["name"], "profile": x} for x in inputs], "sha256": "contract"}
    return {"task_contract": contract, "original_request": goal, "inputs": inputs,
            "available_plugins_and_experts": capabilities or [], "working_memory": {"version": 1, "entries": []}}


def source_for(pkg, strategy="holistic_build", operation="обработать", normalizations=None,
               selected=None):
    sources = []
    for item in pkg["inputs"]:
        sections = [{"name": sh["title"], "role": "табличная сущность",
                     "entities": ["records"],
                     "identifier_fields": (sh.get("columns") or [])[:1],
                     "evidence": ["колонки: " + ", ".join(sh.get("columns") or [])]}
                    for sh in item.get("workbook") or []]
        if item.get("text_sample") is not None:
            sections.append({"name": "document", "role": "текстовый документ",
                             "entities": ["document"], "evidence": ["извлечён текст"]})
        elif item.get("columns") or item.get("sample_rows"):
            sections.append({"name": "data", "role": "табличная сущность", "entities": ["records"],
                             "identifier_fields": (item.get("columns") or [])[:1],
                             "evidence": ["фактические колонки"]})
        sources.append({"name": item["name"], "role": "обязательный вход",
                        "entities": ["business records"], "sections": sections,
                        "evidence": ["фактический профиль"]})
    return {"status": "ready", "strategy": strategy, "reason": "следует из контракта и профиля",
            "sources": sources,
            "operations": [{"name": operation, "inputs": [x["name"] for x in sources],
                            "normalizations": normalizations or [], "evidence": ["Task Contract"]}],
            "acceptance_criteria": ["все обязательные входы обработаны"],
            "selected_capabilities": selected or [], "missing_capability": "", "question": ""}


def assert_matrix(mod):
    # 1. Один Excel: фильтрация/агрегация/отчёт; название и колонки не отраслевые.
    one = package([profile("delta.xlsx", [("Ledger", ["category", "amount"], ["A", 12])])],
                  "Отфильтровать записи и агрегировать сумму")
    valid_one = source_for(one, strategy="build", operation="filter and aggregate")
    assert mod._normalize_source_model(valid_one, one)["ok"]
    # Невалидная первая модель не проходит молча: второй вызов получает точную ошибку валидатора.
    prompts = []
    invalid_one = json.loads(json.dumps(valid_one)); invalid_one["sources"][0]["role"] = ""
    replies = [invalid_one, valid_one]
    original_llm_json = mod._llm_json
    mod._llm_json = lambda llm, prompt, **kw: prompts.append(prompt) or replies.pop(0)
    retried = mod.build_source_model(one, {}, max_tries=2)
    mod._llm_json = original_llm_json
    assert retried["ok"] and len(prompts) == 2 and "не определена роль" in prompts[1]

    # 2. Несколько таблиц и различные ID: преобразование допустимо только с evidence.
    multi = package([profile("left.xlsx", [("Items", ["external_id", "qty"], ["0012", 2])]),
                     profile("right.xlsx", [("Export", ["reference", "value"], ["12", 2])])],
                    "Сопоставить связанные сущности")
    norm = [{"field": "external_id/reference", "method": "канонический числовой ключ",
             "evidence": "обе колонки состоят из цифровых строк, владелец разрешил", "requires_owner": False}]
    assert mod._normalize_source_model(source_for(multi, normalizations=norm), multi)["ok"]
    unsafe = source_for(multi, normalizations=[dict(norm[0], requires_owner=True)])
    assert mod._normalize_source_model(unsafe, multi)["ok"] is False

    # 3. Excel + PDF и 4. документный поток: роли следуют из схемы/текста, не из порядка.
    excel_pdf = package([profile("numbers.xlsx", [("Data", ["key", "metric"], ["K1", 8])]),
                         {"name": "scan.pdf", "extension": ".pdf", "bytes": 20,
                          "sha256": "pdf-sha", "text_sample": "Certificate key K1"}],
                        "Извлечь и сверить данные")
    assert mod._normalize_source_model(source_for(excel_pdf, operation="extract and reconcile"), excel_pdf)["ok"]
    docs = package([{"name": "memo.docx", "extension": ".docx", "bytes": 20,
                     "sha256": "doc-sha", "text_sample": "Request type: service"}],
                   "Классифицировать документ и вернуть структурированный результат")
    assert mod._normalize_source_model(source_for(docs, strategy="build", operation="semantic classification"), docs)["ok"]

    # 5. Смысловой LLM-шаг и 6. ветвление/объединение выбирают стратегию, а не жёсткую цепочку.
    semantic = source_for(docs, strategy="holistic_build", operation="Qwen semantic decision")
    assert mod._normalize_source_model(semantic, docs)["model"]["strategy"] == "holistic_build"
    branches = package([profile("a.xlsx", [("A", ["id", "a"], ["x", 1])]),
                        profile("b.xlsx", [("B", ["id", "b"], ["x", 2])]),
                        profile("c.xlsx", [("C", ["id", "c"], ["x", 3])])], "Объединить независимые ветки")
    assert mod._normalize_source_model(source_for(branches, operation="parallel branches then join"), branches)["ok"]

    # 7. Недостаток данных: ровно один вопрос; 8. capability: кандидат не становится installed/ready.
    need = {"status": "need_human", "strategy": "need_human", "reason": "не задана политика",
            "sources": [], "operations": [], "acceptance_criteria": [], "selected_capabilities": [],
            "missing_capability": "", "question": "Как трактовать записи без даты?"}
    assert mod._normalize_source_model(need, one)["model"]["status"] == "need_human"
    missing_question = dict(need, question="")
    assert mod._normalize_source_model(missing_question, one)["ok"] is False
    acquire = {"status": "acquire", "strategy": "acquire", "reason": "нужна OCR-модель",
               "sources": [], "operations": [], "acceptance_criteria": [], "selected_capabilities": [],
               "missing_capability": "OCR handwriting model",
               "question": "Подключить отдельно проверенную OCR-модель?"}
    acq = mod._normalize_source_model(acquire, one)
    assert acq["ok"] and acq["model"]["status"] == "acquire"

    # Reuse/compose разрешены только для capability из релевантного каталога.
    cap_pkg = package(one["inputs"], capabilities=[{"expert": "sheet_reader", "purpose": "read tables"}])
    reuse = source_for(cap_pkg, strategy="reuse", selected=["sheet_reader"])
    assert mod._normalize_source_model(reuse, cap_pkg)["ok"]
    assert mod._normalize_source_model(source_for(cap_pkg, strategy="reuse", selected=["invented"]), cap_pkg)["ok"] is False

    # 9. Смена схемы: ссылка на пропавший лист fail-closed; возможна честная остановка.
    changed = json.loads(json.dumps(source_for(one)))
    changed["sources"][0]["sections"][0]["name"] = "OldSheet"
    assert mod._normalize_source_model(changed, one)["ok"] is False
    assert mod._normalize_source_model(dict(need, question="Какое новое поле заменяет category?"), one)["ok"]


def acceptance_cases(mod, root):
    import openpyxl
    inp = root / "input.csv"
    inp.write_text("id,value\nA,1\n", encoding="utf-8")
    out = root / "out"
    out.mkdir(exist_ok=True)
    md = out / "report.md"
    md.write_text("# Result\n\nNo matching records; checked 1 input and 1 row with criterion X. "
                  "The empty result is expected and supported by the recorded acceptance evidence.\n",
                  encoding="utf-8")
    xlsx = out / "report.xlsx"
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["criterion", "result"]); ws.append(["X", 0]); wb.save(xlsx)
    # 10. Корректный пустой результат принимается с доказательством.
    empty = {"status": "success", "summary": {"processed_files": 1, "total_count": 0, "matched": 0},
             "evidence": {"files_used": ["input.csv"], "acceptance_checks": [
                 {"criterion": "проверены все строки", "passed": True, "evidence": "1 строка, 0 совпадений"}]},
             "report_md": str(md), "report_xlsx": str(xlsx)}
    assert mod.validate_result(empty, [inp])["ok"]
    # 11. Формальный success без фактов отклоняется.
    false_success = {"status": "success", "summary": {"processed_files": 1, "total_count": 0}}
    rejected = mod.validate_result(false_success, [inp])
    assert not rejected["ok"] and any("acceptance_checks" in x for x in rejected["issues"])
    return inp, empty, false_success


def repair_and_memory(mod, root, inp, good, bad):
    sid = "wz_universal"
    (root / (sid + ".json")).write_text(json.dumps({
        "session_id": sid, "client_name": "Universal", "questionnaire_task": "Обработать пакет",
        "answers": {"adaptive_91": {"question": "Какой результат?", "answer": "Проверенный отчёт"}},
        "rules": ["не выполнять внешние записи"]}, ensure_ascii=False), encoding="utf-8")
    (root / (sid + "_blueprint.json")).write_text(json.dumps({"blueprint": {
        "process_name": "Universal", "goal": "Обработать пакет", "sample_test_plan": {
            "success_criteria": ["отчёт доказан"], "steps": ["прочитать", "проверить"]}}},
        ensure_ascii=False), encoding="utf-8")
    (root / (sid + "_build_plan.json")).write_text('{"plan":{"tasks":[]}}', encoding="utf-8")

    def ready(pkg, llm, max_tries=2):
        raw = source_for(pkg, strategy="build", operation="read and report")
        return mod._normalize_source_model(raw, pkg)

    mod.build_source_model = ready
    create_calls, packages, promotions, deletions = [], [], [], []

    def create(name, pkg, feedback, llm):
        create_calls.append(feedback)
        packages.append(json.loads(json.dumps(pkg, default=str)))
        if len(create_calls) <= 3:
            return {"ok": False, "why": "generation unavailable %d" % len(create_calls)}
        return {"ok": True, "code_sha256": "changed-%d" % (len(create_calls) - 3)}

    runs = [bad, bad, good]
    mod._create_or_update = create
    mod.run_expert = lambda *a, **k: runs.pop(0)
    mod.judge_result = lambda *a, **k: {
        "verdict": "pass", "confidence": 0.96, "issues": [], "owner_question": "",
        "memory": {"concepts": [{"text": "Вход является реестром", "evidence": "полный прогон"}],
                   "rules": [{"text": "Проверять все строки", "evidence": "acceptance"}]},
        "rejected_hypotheses": []}
    mod._promote_expert = lambda draft, stable, pkg: (
        promotions.append((draft, stable)) or {"ok": True, "code_sha256": "stable"})
    mod._delete_draft = lambda draft, agent="": deletions.append(draft)
    mod.time.sleep = lambda *_: None
    result = mod.build_agentic_solution(
        sid, "budget", "uni", [inp], root, root, {"agent_id": "agent_qwen"},
        max_creation_attempts=4, max_run_repairs=2, max_acceptance_repairs=2, max_total_attempts=6)
    # 12. Три create-fail + первый runnable fail + две реально изменённые repair-версии.
    assert result["ok"] and len(create_calls) == 6, result
    runnable = [x for x in result["attempts"] if x.get("runnable_attempt")]
    assert len(runnable) == 3 and runnable[-1]["budgets"]["run_repair"] == 2
    assert promotions and promotions[0][1] == "uni_run_process"
    assert all("__draft_" in x[0] for x in promotions)
    # 14. Следующая попытка получила rejected-урок; success публикует только verified.
    assert any(x.get("status") == "rejected" for x in packages[4]["working_memory"]["entries"])
    assert result["verified_memory"] and all(x["status"] == "verified" for x in result["verified_memory"])
    assert all(x["status"] == "verified" for x in result["verified_memory"])

    # 13. Неизменившийся код — no_progress, не repair; итог честно stalled.
    sid_fail = "wz_universal_fail"
    base_session = json.loads((root / (sid + ".json")).read_text(encoding="utf-8"))
    base_session.pop("agentic_memory", None); base_session["session_id"] = sid_fail
    (root / (sid_fail + ".json")).write_text(json.dumps(base_session, ensure_ascii=False), encoding="utf-8")
    for suffix in ("_blueprint.json", "_build_plan.json"):
        (root / (sid_fail + suffix)).write_text((root / (sid + suffix)).read_text(encoding="utf-8"),
                                                encoding="utf-8")
    same_calls = []
    mod._create_or_update = lambda name, pkg, feedback, llm: (
        same_calls.append(feedback) or {"ok": True, "code_sha256": "same"})
    mod.run_expert = lambda *a, **k: bad
    promotions.clear()
    stalled = mod.build_agentic_solution(
        sid_fail, "stalled", "uni", [inp], root, root, {"agent_id": "agent_qwen"},
        max_creation_attempts=1, max_run_repairs=2, max_total_attempts=4)
    assert not stalled["ok"] and stalled["code"] == "agentic_stalled"
    assert sum(1 for x in stalled["attempts"] if x.get("phase") == "no_progress") == 3
    assert stalled["budgets"]["run_repair"]["used"] == 0 and not promotions
    # Failed memory survives this session but does not become verified process memory.
    rebuilt = mod.make_task_package(sid_fail, [inp], root, llm={"agent_id": "agent_qwen"})
    assert any(x.get("status") == "rejected" for x in rebuilt["working_memory"]["entries"])
    assert mod._verified_memory(stalled["working_memory"]) == []

    # need_human/acquire stop before any draft and cannot publish memory.
    called = []
    mod._create_or_update = lambda *a, **k: called.append(True) or {"ok": True, "code_sha256": "x"}
    mod.build_source_model = lambda *a, **k: {"ok": True, "model": {
        "status": "need_human", "strategy": "need_human", "reason": "ambiguous",
        "sources": [], "operations": [], "question": "Как трактовать пустое поле?", "sha256": "n"}}
    stopped = mod.build_agentic_solution(sid_fail, "human", "uni", [inp], root, root, {"agent_id": "agent_qwen"})
    assert stopped["code"] == "needs_owner_input" and not stopped["draft_created"] and not called
    mod.build_source_model = lambda *a, **k: {"ok": True, "model": {
        "status": "acquire", "strategy": "acquire", "reason": "missing OCR",
        "sources": [], "operations": [], "question": "Подключить OCR отдельно?",
        "missing_capability": "OCR", "sha256": "a"}}
    stopped = mod.build_agentic_solution(sid_fail, "acquire", "uni", [inp], root, root, {"agent_id": "agent_qwen"})
    assert stopped["code"] == "capability_missing" and stopped["verified_memory"] == [] and not called


def profile_variants(mod, root):
    # Варьируем порядок листов/названия/колонки и доказываем, что профилировщик не берёт только первые четыре.
    import openpyxl
    book = root / "renamed.xlsx"
    wb = openpyxl.Workbook(); wb.remove(wb.active)
    for i in range(6):
        ws = wb.create_sheet("Section-%d" % i)
        ws.append(["field_%d" % i, "measure_%d" % i]); ws.append(["id-%d" % i, i])
    wb.save(book)
    prof = mod.profile_file(book)
    assert len(prof["workbook"]) == 6 and prof["workbook"][5]["columns"] == ["field_5", "measure_5"]
    doc = root / "flow.docx"
    with zipfile.ZipFile(doc, "w") as zf:
        zf.writestr("word/document.xml", "<w:document><w:t>Document request alpha</w:t></w:document>")
    assert "Document request alpha" in mod.profile_file(doc).get("text_sample", "")


def main():
    mod = load_module()
    assert_matrix(mod)
    with tempfile.TemporaryDirectory(prefix="wz_universal_") as td:
        root = Path(td)
        inp, good, bad = acceptance_cases(mod, root)
        profile_variants(mod, root)
        repair_and_memory(mod, root, inp, good, bad)
    print("универсальная матрица: 14 классов + repair budget + память + fail-closed stops ✓")


if __name__ == "__main__":
    main()
