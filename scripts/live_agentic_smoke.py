#!/usr/bin/env python3
"""Живой (не CI) smoke агентной стройки на Qwen: Excel + PDF -> сверка -> два отчёта.

Создаёт/обновляет глобального QA-эксперта qaag_run_process. Запускать явно перед QA-релизом.
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP = Path.home() / "extella_wizard" / "app"
sys.path.insert(0, str(APP))  # живые config/wz_platform/wz_llm установленного моста

spec = importlib.util.spec_from_file_location("wz_agentic_live", ROOT / "ui" / "wz_agentic.py")
agentic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agentic)


def main():
    import openpyxl
    from reportlab.pdfgen import canvas

    with tempfile.TemporaryDirectory(prefix="extella_agentic_smoke_") as td:
        root = Path(td)
        sid = "wz_agentic_smoke"
        files = root / (sid + "_files")
        files.mkdir()

        xlsx = files / "registry.xlsx"
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Реестр 1С"
        ws.append(["Номер пломбы", "Количество", "Дата"])
        ws.append(["A-101", 2, "2026-07-20"])
        ws.append(["B-202", 1, "2026-07-20"])
        wb.save(xlsx)

        pdf = files / "certificate.pdf"
        c = canvas.Canvas(str(pdf)); c.drawString(72, 760, "Certificate of issued seals")
        c.drawString(72, 735, "Seal A-101 quantity 2")
        c.drawString(72, 710, "Seal C-303 quantity 1")
        c.save()

        session = {
            "session_id": sid, "client_name": "QA сверка реестра пломб",
            "answers": {
                "goal": {"question": "Что нужно сделать?",
                         "answer": "Сверить номера пломб и количество в выгрузке 1С Excel с сертификатом PDF"},
                "result": {"question": "Что считать результатом?",
                           "answer": "Таблица совпадений и расхождений; A-101 совпадает, B-202 только в Excel, C-303 только в PDF"},
            },
            "rules": ["Только чтение", "Не отправлять данные наружу"],
            "fields": {"comparison_key": "Номер пломбы", "quantity_field": "Количество"},
        }
        blueprint = {"process_name": "Сверка пломб", "goal": "Сверить Excel и PDF по номеру пломбы и количеству",
                     "archetype": "document_processing",
                     "stages": [
                         {"id": "excel", "title": "Прочитать реестр Excel"},
                         {"id": "pdf", "title": "Извлечь пломбы из PDF"},
                         {"id": "compare", "title": "Сопоставить номера и количество"},
                         {"id": "report", "title": "Сформировать отчёт"}],
                     "sample_test_plan": ["A-101 = match", "B-202 = only_excel", "C-303 = only_pdf"]}
        plan = {"tasks": [
            {"id": "excel", "depends_on": []}, {"id": "pdf", "depends_on": []},
            {"id": "compare", "depends_on": ["excel", "pdf"]},
            {"id": "report", "depends_on": ["compare"]}]}
        (root / (sid + ".json")).write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
        (root / (sid + "_blueprint.json")).write_text(
            json.dumps({"blueprint": blueprint}, ensure_ascii=False), encoding="utf-8")
        (root / (sid + "_build_plan.json")).write_text(
            json.dumps({"plan": plan}, ensure_ascii=False), encoding="utf-8")

        def progress(stage, title, status="running", detail=""):
            print("[%s] %s: %s%s" % (status, stage, title, (" · " + detail) if detail else ""), flush=True)

        result = agentic.build_agentic_solution(
            session_id=sid, build_id="build_agentic_smoke", namespace="qaag",
            sample_files=[xlsx, pdf], sess_dir=root, runs_dir=root,
            llm={"agent_id": agentic.qwen_agent(), "api_token": agentic.CONFIG.get("auth_token", ""),
                 "api_base": agentic.BASE, "api_key": ""},
            progress=progress, max_attempts=3)
        print(json.dumps({k: v for k, v in result.items() if k not in ("result", "attempts")},
                         ensure_ascii=False, indent=2, default=str))
        if not result.get("ok"):
            raise SystemExit(1)

        # HOLDOUT не видел Builder: другие идентификаторы и другой тип расхождения. Доказывает,
        # что эксперт выучил алгоритм, а не только перестал печатать A-101 в исходнике.
        hold = root / "holdout_files"; hold.mkdir()
        hx = hold / "new_registry.xlsx"
        wb = openpyxl.Workbook(); ws = wb.active
        ws.append(["Номер пломбы", "Количество"]); ws.append(["D-404", 5]); ws.append(["E-505", 2])
        wb.save(hx)
        hp = hold / "new_certificate.pdf"
        c = canvas.Canvas(str(hp)); c.drawString(72, 750, "Seal D-404 quantity 5")
        c.drawString(72, 725, "Seal E-505 quantity 3"); c.drawString(72, 700, "Seal F-606 quantity 1"); c.save()
        hout = root / "holdout_output"; hout.mkdir()
        hres = agentic.run_expert(result["expert"], {
            "source_file": str(hold), "output_dir": str(hout),
            "api_token": agentic.CONFIG.get("auth_token", ""), "api_base": agentic.BASE,
            "rules_json": "[]", "fields_json": json.dumps({"comparison_key": "Номер пломбы"}, ensure_ascii=False),
        }, wait=900, glob=True)
        hv = agentic.validate_result(hres, [hx, hp])
        if not hv.get("ok"):
            raise RuntimeError("holdout structural fail: " + str(hv))
        hs = hv.get("summary") or {}
        mismatch = hs.get("qty_mismatch", hs.get("qty_mismatches", hs.get("quantity_mismatch")))
        assert hs.get("processed_files") == 2 and hs.get("matches") == 1, hs
        assert hs.get("only_excel", 0) == 0 and hs.get("only_pdf") == 1 and mismatch == 1, hs
        print("HOLDOUT ✓ " + json.dumps(hs, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
