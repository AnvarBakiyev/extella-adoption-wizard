#!/usr/bin/env python3
"""Живой E2E через HTTP моста: session -> 2 uploads -> build -> cabinet run -> archive."""
import base64
import json
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path


BASE = "http://127.0.0.1:8765"


def post(path, body, timeout=900):
    req = urllib.request.Request(BASE + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"status": "error", "http_code": exc.code, "message": str(exc)}


def get(path, timeout=60):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main():
    import openpyxl
    from reportlab.pdfgen import canvas

    created = post("/x/expert", {"expert_name": "wz_session", "params": {
        "action": "create", "client_name": "QA Agentic E2E"}})
    session = ((created.get("result") or {}).get("session") if isinstance(created.get("result"), dict) else None)
    if not session:
        session = created.get("session") or {}
    sid = session.get("session_id")
    if not sid:
        raise RuntimeError("session create failed: " + str(created)[:500])
    print("session", sid, flush=True)
    archived = False
    try:
        answers = {
            "goal": {"question": "Что сделать?",
                     "answer": "Сверить номера пломб и количество из Excel с сертификатом PDF"},
            "input": {"question": "Какие входы?", "answer": "Один Excel и один PDF в общей папке"},
            "result": {"question": "Как проверить?",
                       "answer": "A-101 совпадает; B-202 только Excel; C-303 только PDF; нужен XLSX и Markdown отчёт"},
            "authority": {"question": "Что разрешено?", "answer": "Только чтение локальных файлов"},
            "schedule": {"question": "Как запускать?", "answer": "Пока вручную"},
        }
        post("/x/expert", {"expert_name": "wz_session", "params": {"action": "save_answers",
             "session_id": sid, "payload_json": json.dumps(answers, ensure_ascii=False)}})
        with tempfile.TemporaryDirectory(prefix="wz_http_e2e_") as td:
            root = Path(td)
            xlsx = root / "registry.xlsx"
            wb = openpyxl.Workbook(); ws = wb.active
            ws.append(["Номер пломбы", "Количество"]); ws.append(["A-101", 2]); ws.append(["B-202", 1])
            wb.save(xlsx)
            pdf = root / "certificate.pdf"
            c = canvas.Canvas(str(pdf)); c.drawString(72, 750, "Seal A-101 quantity 2")
            c.drawString(72, 725, "Seal C-303 quantity 1"); c.save()
            for path in (xlsx, pdf):
                uploaded = post("/x/upload", {"session_id": sid, "filename": path.name,
                                "content_base64": base64.b64encode(path.read_bytes()).decode("ascii")})
                if uploaded.get("status") != "success":
                    raise RuntimeError("upload failed: " + str(uploaded))

        sess_root = Path.home() / "extella_wizard" / "sessions"
        blueprint_path = sess_root / (sid + "_blueprint.json")
        blueprint = {"process_name": "QA сверка пломб", "goal": "Сверить Excel и PDF по пломбам и количеству",
                     "archetype": "document_processing", "suitability": {"score": 90},
                     "stages": [
                         {"id": "s1", "title": "Прочитать Excel", "business_description": "Реестр 1С"},
                         {"id": "s2", "title": "Прочитать PDF", "business_description": "Сертификат пломб"},
                         {"id": "s3", "title": "Сверить", "business_description": "Номер и количество"},
                         {"id": "s4", "title": "Отчёт", "business_description": "Расхождения XLSX и Markdown"}],
                     "sample_test_plan": ["A-101 совпадает", "B-202 only_excel", "C-303 only_pdf"]}
        blueprint_path.write_text(json.dumps({"session_id": sid, "blueprint": blueprint}, ensure_ascii=False),
                                  encoding="utf-8")
        post("/x/expert", {"expert_name": "wz_session", "params": {"action": "set_blueprint",
             "session_id": sid, "payload_json": str(blueprint_path)}})

        started = post("/x/build", {"session_id": sid})
        bid = started.get("build_id")
        if not bid:
            raise RuntimeError("build start failed: " + str(started))
        print("build", bid, flush=True)
        deadline = time.time() + 1800
        last_title = ""
        progress = {}
        while time.time() < deadline:
            progress = (get("/x/build_progress?build_id=" + bid).get("progress") or {})
            stages = progress.get("stages") or []
            title = stages[-1].get("title", "") if stages else progress.get("status", "")
            if title != last_title:
                print(progress.get("status"), "·", title, flush=True); last_title = title
            if progress.get("status") in ("built", "error", "orphaned"):
                break
            time.sleep(4)
        if progress.get("status") != "built":
            raise RuntimeError("build failed: " + json.dumps(progress.get("error_struct") or progress,
                                                               ensure_ascii=False, default=str)[:1500])
        assert progress.get("build_mode") == "agentic", progress
        assert progress.get("source_files") == ["certificate.pdf", "registry.xlsx"] or \
               set(progress.get("source_files") or []) == {"certificate.pdf", "registry.xlsx"}
        print("built ·", progress.get("orchestrator"), "·", progress.get("slice_summary"), flush=True)

        deployed = post("/x/deploy", {"session_id": sid, "confirmed": True})
        if deployed.get("status") != "success":
            raise RuntimeError("deploy approval failed: " + str(deployed))
        run = post("/x/run_process", {"session_id": sid}, timeout=1000)
        if run.get("status") != "success":
            raise RuntimeError("cabinet run failed: " + json.dumps(run, ensure_ascii=False, default=str)[:1200])
        summary = (run.get("run") or {}).get("summary") or {}
        assert summary.get("processed_files") == 2, summary
        matches = summary.get("matches", summary.get("match"))
        assert matches == 1 and summary.get("only_excel") == 1 and summary.get("only_pdf") == 1, summary
        assert (run.get("run") or {}).get("report_md") and (run.get("run") or {}).get("report_xlsx")
        print("cabinet run ✓", json.dumps(summary, ensure_ascii=False), flush=True)

        removed = post("/x/automation_delete", {"session_id": sid})
        archived = removed.get("status") == "success"
        print("archive", archived, flush=True)
    finally:
        if not archived:
            print("QA session kept for diagnostics:", sid, flush=True)


if __name__ == "__main__":
    main()
