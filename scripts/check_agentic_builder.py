#!/usr/bin/env python3
"""Регрессия агентной стройки: весь контекст/файлы -> фактическая приёмка -> один эксперт."""
import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "ui" / "wz_agentic.py"
BUILD = ROOT / "ui" / "wz_build.py"
WIZARD = ROOT / "ui" / "wizard.html"
SERVER = ROOT / "ui" / "server.py"


def load_module():
    platform = types.ModuleType("wz_platform")
    platform.BASE = "https://example.invalid"
    platform.CONFIG = {"auth_token": "test"}
    platform.api = lambda *a, **k: {}
    platform.qwen_agent = lambda: "agent_test"
    platform.run_expert = lambda *a, **k: {}
    llm = types.ModuleType("wz_llm")
    llm.design_agent = lambda: "agent_judge"
    sys.modules["wz_platform"] = platform
    sys.modules["wz_llm"] = llm
    spec = importlib.util.spec_from_file_location("wz_agentic_test", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    mod = load_module()
    with tempfile.TemporaryDirectory(prefix="wz_agentic_test_") as td:
        root = Path(td)
        sid = "wz_test"
        files = root / (sid + "_files")
        files.mkdir()
        xlsx = files / "register.xlsx"
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Номер пломбы", "Количество"])
        ws.append(["A-101", 2])
        wb.save(xlsx)
        pdf = files / "certificate.pdf"
        try:
            from pypdf import PdfWriter
        except Exception:
            from PyPDF2 import PdfWriter
        writer = PdfWriter(); writer.add_blank_page(width=300, height=300)
        with pdf.open("wb") as fh:
            writer.write(fh)
        (root / (sid + ".json")).write_text(json.dumps({
            "session_id": sid, "client_name": "Сверка пломб",
            "answers": {"goal": {"question": "Что сделать?", "answer": "Сверить реестр с сертификатом"}},
            "rules": ["ничего не отправлять наружу"], "fields": {"key": "Номер пломбы"}},
            ensure_ascii=False), encoding="utf-8")
        (root / (sid + "_blueprint.json")).write_text(json.dumps({"blueprint": {
            "process_name": "Сверка", "goal": "Найти расхождения Excel и PDF",
            "stages": [{"id": "excel"}, {"id": "pdf"}, {"id": "compare"}]}},
            ensure_ascii=False), encoding="utf-8")
        (root / (sid + "_build_plan.json")).write_text(json.dumps({"plan": {
            "tasks": [{"id": "excel", "depends_on": []}, {"id": "pdf", "depends_on": []},
                      {"id": "compare", "depends_on": ["excel", "pdf"]}]}},
            ensure_ascii=False), encoding="utf-8")
        package = mod.make_task_package(sid, [xlsx, pdf], root)
        assert package["blueprint"]["goal"] == "Найти расхождения Excel и PDF"
        assert {x["name"] for x in package["inputs"]} == {"register.xlsx", "certificate.pdf"}
        assert package["task_contract"]["interview"][0]["answer"] == "Сверить реестр с сертификатом"
        assert all(x.get("profile") for x in package["task_contract"]["inputs"])
        assert package["package_sha256"]
        package["build_plan"] = {"legacy_instruction": "LEGACY_PLAN_MUST_NOT_REACH_QWEN"}
        package["assistant_context"] = {"messages": ["CHAT_TRANSCRIPT_MUST_NOT_REACH_QWEN"]}
        catalog = [
            {"expert": "pdf_ocr", "purpose": "OCR and PDF document extraction"},
            {"expert": "excel_reader", "purpose": "Read Excel xlsx spreadsheets"},
            {"expert": "weather", "purpose": "Weather forecast"},
            {"expert": "github", "purpose": "GitHub pull requests"},
            {"expert": "reddit", "purpose": "Search Reddit"},
            {"expert": "stocks", "purpose": "Stock market prices"},
            {"expert": "cowsay", "purpose": "Print an ASCII cow"},
        ]
        selected = mod._select_capabilities(package, catalog)
        assert {x["expert"] for x in selected} == {"pdf_ocr", "excel_reader"}, selected
        package["available_plugins_and_experts"] = selected
        prompt = mod._build_prompt("pc_run_process", package, "", "agent_test")
        assert "Не запускай его сам" in prompt and "явными source_file и output_dir" in prompt
        assert "LEGACY_PLAN_MUST_NOT_REACH_QWEN" not in prompt
        assert "CHAT_TRANSCRIPT_MUST_NOT_REACH_QWEN" not in prompt
        assert "authority_order" in prompt and "relevant_capabilities" in prompt
        assert "weather" not in prompt and len(prompt) < 18000, len(prompt)
        repair = mod._repair_context({"issue": "hardcoded A-101", "file": str(pdf)}, package)
        assert "A-101" not in repair and "<sample_value>" in repair and "certificate.pdf" in repair

        out = root / "out"
        out.mkdir()
        md = out / "report.md"
        md.write_text("# Сверка\n\nОбработаны register.xlsx и certificate.pdf. Расхождений: 1.\n", encoding="utf-8")
        report_xlsx = out / "report.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Номер", "Статус"]); ws.append(["A-101", "расхождение"])
        wb.save(report_xlsx)
        result = {"status": "success",
                  "summary": {"processed_files": 2, "total_count": 1, "discrepancies": 1},
                  "evidence": {"files_used": ["register.xlsx", "certificate.pdf"],
                               "acceptance_checks": [{"criterion": "сверка", "passed": True,
                                                      "evidence": "найдено 1 расхождение"}]},
                  "report_md": str(md), "report_xlsx": str(report_xlsx)}
        accepted = mod.validate_result(result, [xlsx, pdf])
        assert accepted["ok"] is True, accepted
        broken = json.loads(json.dumps(result))
        broken["evidence"]["files_used"] = ["register.xlsx"]
        rejected = mod.validate_result(broken, [xlsx, pdf])
        assert rejected["ok"] is False
        assert any("certificate.pdf" in issue for issue in rejected["issues"])

        code = ('def pc_run_process(source_file="", output_dir="", api_token="", api_base="", target="", '
                'source_key="", rules_json="", fields_json="", run_id="", placement_json="", '
                'adapter_json="", report_spec_json=""):\n    return {}\n')
        assert mod._validate_code("pc_run_process", code, package) == []
        leaked = code.replace("return {}", "return {'seal': 'A-101'}")
        assert any("значения образца" in x for x in mod._validate_code("pc_run_process", leaked, package))
        escaped = code.replace("return {}", "return list(Path.home().rglob('*.xlsx'))")
        assert any("Path.home(" in x for x in mod._validate_code("pc_run_process", escaped, package))

        # Быстрый путь — один code-artifact ответ, validation и контролируемый API-save. Responses API
        # reasoning не должен победить финальный message. Нативный tool-action остаётся только fallback.
        original_post = mod._post_agent
        original_get = mod._get_scoped_expert
        original_api = mod.api
        fallback_calls, saved_experts = [], []

        def artifact_first(_agent, payload, **_kwargs):
            fallback_calls.append(payload["input"])
            if payload["input"].startswith("РЕЖИМ CODE-ARTIFACT"):
                artifact = json.dumps({"code": code, "description": "Проверенный тестовый эксперт"},
                                      ensure_ascii=False)
                return {"status": "completed", "output": [
                    {"type": "reasoning", "content": [
                        {"type": "reasoning_text", "text": "внутренний анализ не является ответом"}]},
                    {"type": "message", "content": [
                        {"type": "output_text", "text": artifact}]},
                ]}
            raise AssertionError("native action must not run when code-artifact is valid")

        def fallback_api(path, body, **_kwargs):
            if path == "/api/expert/get":
                return {}
            if path == "/api/expert/save":
                saved_experts.append(body)
                return {"status": "success", "id": "saved"}
            return {}

        mod._post_agent = artifact_first
        mod._get_scoped_expert = lambda *_args, **_kwargs: {}
        mod.api = fallback_api
        recovered = mod._create_or_update("pc_run_process", package, "", {"agent_id": "agent_test"})
        assert recovered["ok"] is True, recovered
        assert len(fallback_calls) == 1 and fallback_calls[0].startswith("РЕЖИМ CODE-ARTIFACT")
        assert recovered["generation_path"] == "code_artifact"
        assert saved_experts and saved_experts[0]["name"] == "pc_run_process"
        assert saved_experts[0]["code"] == code

        # Если провайдер не соблюдает JSON-контракт, один нативный action-fallback всё ещё разрешён.
        native = {"called": False}

        def invalid_artifact_then_native(_agent, payload, **_kwargs):
            if payload["input"].startswith("РЕЖИМ CODE-ARTIFACT"):
                return {"status": "completed", "output_text": "не JSON"}
            native["called"] = True
            return {"status": "completed", "output_text": "эксперт сохранён"}

        mod._post_agent = invalid_artifact_then_native
        mod._get_scoped_expert = lambda *_args, **_kwargs: (
            {"expert_code": code, "description": "Нативный fallback"} if native["called"] else {})
        recovered_native = mod._create_or_update(
            "pc_run_process", package, "", {"agent_id": "agent_test"})
        assert recovered_native["ok"] is True and native["called"], recovered_native
        assert recovered_native["generation_path"] == "native_action_fallback"
        mod._post_agent = original_post
        mod._get_scoped_expert = original_get
        mod.api = original_api

        def ready_source(pkg, llm, max_tries=2):
            sources = []
            for p in pkg["inputs"]:
                sections = []
                for sh in p.get("workbook") or []:
                    sections.append({"name": sh["title"], "role": "данные", "evidence": sh.get("columns") or ["sample"]})
                if p.get("text_sample") is not None:
                    sections.append({"name": "document", "role": "документ", "evidence": ["text profile"]})
                sources.append({"name": p["name"], "role": "обязательный вход", "entities": ["записи"], "sections": sections,
                                "evidence": ["фактический профиль"]})
            raw = {"status": "ready", "strategy": "holistic_build", "reason": "несколько входов",
                   "sources": sources, "operations": [{"name": "обработать пакет", "inputs": [x["name"] for x in sources],
                                                        "evidence": ["Task Contract"]}],
                   "acceptance_criteria": ["все файлы обработаны"], "selected_capabilities": [],
                   "missing_capability": "", "question": ""}
            return mod._normalize_source_model(raw, pkg)

        checked_source = ready_source(package, {}, 2)
        assert checked_source["ok"] and checked_source["model"]["strategy"] == "holistic_build"
        ambiguous = json.loads(json.dumps(checked_source["model"]))
        ambiguous.pop("sha256", None)
        ambiguous["operations"][0]["normalizations"] = [{"field": "id", "method": "remove prefix",
                                                          "evidence": "guess", "requires_owner": True}]
        assert mod._normalize_source_model(ambiguous, package)["ok"] is False
        mod.build_source_model = ready_source

        # Solve-run-repair: первый фактический прогон не доказал PDF, второй исправлен и принят.
        bad = json.loads(json.dumps(result))
        bad["evidence"]["files_used"] = ["register.xlsx"]
        runs = [bad, result]
        feedbacks, events, promotions, deletions = [], [], [], []
        mod._create_or_update = lambda name, pkg, feedback, llm: (
            feedbacks.append(feedback) or {"ok": True, "code_sha256": "code-%d" % len(feedbacks)})
        mod._promote_expert = lambda draft, stable, pkg: (
            promotions.append((draft, stable)) or {"ok": True, "code_sha256": "accepted"})
        mod._delete_draft = lambda draft, agent="": deletions.append((draft, agent))
        mod.run_expert = lambda *a, **k: runs.pop(0)
        mod.judge_result = lambda *a, **k: {"verdict": "pass", "confidence": 0.94,
                                                    "issues": [], "owner_question": ""}
        mod.time.sleep = lambda *_: None
        built = mod.build_agentic_solution(
            sid, "build_test", "pc", [xlsx, pdf], root, root, {"agent_id": "agent_test"},
            progress=lambda *args: events.append(args), max_creation_attempts=2, max_total_attempts=4)
        assert built["ok"] is True
        assert len(built["attempts"]) == 2
        assert feedbacks[0] == "" and "certificate.pdf" in json.dumps(feedbacks[1])
        assert built["source_file"] == str(files)
        assert promotions and promotions[0][1] == "pc_run_process" and "__draft_" in promotions[0][0]
        assert deletions and deletions[0][0] == promotions[0][0]
        assert any(event[0] == "agentic_accept" and event[2] == "success" for event in events)
        assert any("code-artifact draft-эксперта" in event[1] for event in events)
        assert any("run_expert" in event[1] for event in events)
        assert any("Stable-эксперт опубликован" in event[1] for event in events)
        assert built["verified_memory"] and all(x["status"] == "verified" for x in built["verified_memory"])

        # Красная стройка удаляет только уникальный draft и ни разу не публикует stable-эксперта.
        runs[:] = [bad, bad, bad]
        promotions.clear(); deletions.clear()
        failed = mod.build_agentic_solution(
            sid, "build_fail", "pc", [xlsx, pdf], root, root, {"agent_id": "agent_test"},
            progress=lambda *args: None, max_creation_attempts=1, max_total_attempts=3)
        assert failed["ok"] is False and promotions == []
        assert deletions and "__draft_" in deletions[-1][0]
        assert failed["expert_ran"] is True and failed["verified_memory"] == []
        assert all(x.get("status") != "verified" or x.get("source") == "owner"
                   for x in failed["working_memory"]["entries"])

        # Внутренний Строитель и внешний UPC обязаны применять один minimum_confidence. Раньше
        # Строитель публиковал stable при 0.85, после чего UPC с порогом 0.90 отвергал тот же шаг.
        runs[:] = [result, result, result]
        promotions.clear(); deletions.clear()
        mod.judge_result = lambda *a, **k: {"verdict": "pass", "confidence": 0.85,
                                            "issues": [], "owner_question": ""}
        strict = mod.build_agentic_solution(
            sid, "build_strict_semantic", "pc", [xlsx, pdf], root, root,
            {"agent_id": "agent_test"}, progress=lambda *args: None,
            max_creation_attempts=1, max_acceptance_repairs=2, max_total_attempts=3,
            step_contract={"id": "quality", "title": "Смысловая проверка", "version": 1,
                           "acceptance": {"required_artifacts": ["report_md", "report_xlsx"],
                                          "semantic_criteria": ["результат соответствует смыслу"],
                                          "minimum_confidence": 0.90}})
        assert strict["ok"] is False and promotions == [], strict
        assert all((row.get("judge") or {}).get("confidence") == 0.85
                   for row in strict["attempts"] if row.get("judge")), strict["attempts"]

        # Полностью детерминированный контракт не оплачивает и не ждёт второго Qwen-судью.
        runs[:] = [result]
        promotions.clear(); deletions.clear()
        mod.judge_result = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("deterministic step must not call semantic judge"))
        deterministic = mod.build_agentic_solution(
            sid, "build_deterministic_fast", "pc", [xlsx, pdf], root, root,
            {"agent_id": "agent_test"}, progress=lambda *args: None,
            max_creation_attempts=1, max_total_attempts=3,
            step_contract={"id": "files", "title": "Проверка файлов", "version": 1,
                           "acceptance": {"required_artifacts": ["report_md", "report_xlsx"],
                                          "deterministic_checks": ["оба файла обработаны"],
                                          "semantic_criteria": [], "minimum_confidence": 0.99}})
        assert deterministic["ok"] is True and promotions, deterministic
        assert deterministic["judge"]["source"] == "deterministic_contract"

    source = BUILD.read_text(encoding="utf-8")
    assert "prepared = prepare_task_context(" in source
    assert 'source_model.get("strategy") == "holistic_build"' in source
    assert 'llm["task_context"] = _builder_brief(task_package)' in source
    assert "build_agentic_solution(" in source
    assert "max_creation_attempts=4" in source and "max_acceptance_repairs=2" in source
    assert source.index("build_agentic_solution(") < source.index("# KNOWLEDGE-СТАДИЯ")
    assert '"agentic_events": []' in source and 'event = {"at": now()' in source
    assert '"updated_at": stamp' in source
    html = WIZARD.read_text(encoding="utf-8")
    assert "Агентная стройка:" in html
    assert "Последняя активность:" in html and "История действий" in html
    assert "Прямые вызовы Qwen не отображаются в Listener" in html
    server = SERVER.read_text(encoding="utf-8")
    assert 'lb.get("agentic_contract"' in server
    assert '_fp2["output_dir"] = _agentic_output_dir' in server
    assert '_fp["output_dir"] = _agentic_output_dir' in server
    print("агентная стройка: полное ТЗ + все файлы + фактическая приёмка + упаковка ✓")


if __name__ == "__main__":
    main()
