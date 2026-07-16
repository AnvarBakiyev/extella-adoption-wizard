#!/usr/bin/env python3
"""CSPL Studio S1: регистрация handler'а в cspl:registry с ОБЯЗАТЕЛЬНЫМ fixtures-гейтом
(канон вета: код не регистрируется без прохождения контрольных прогонов).
Прогоняет позитивные/негативные fixtures + проверку детерминизма (sha256 md двух прогонов),
при зелени пишет запись в KV cspl:registry (default-скоуп) — оттуда язык виден Capability
Registry (/x/registry) и всем поверхностям.
Запуск: python3 scripts/cspl_register.py cspl_report_dsl
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync  # token()/api() — те же, что у sync-инструмента

RECORDS = [
    {"Номер документа": "СФ-001", "Контрагент": "ТОО Альфа", "Расхождение": "0"},
    {"Номер документа": "СФ-002", "Контрагент": "ИП Бета", "Расхождение": "500"},
    {"Номер документа": "СФ-003", "Контрагент": "ТОО Альфа", "Расхождение": "1800"},
    {"Номер документа": "СФ-004", "Контрагент": "АО Гамма", "Расхождение": "8000"},
    {"Номер документа": "СФ-005", "Контрагент": "ИП Бета", "Расхождение": "4500"},
]

FIXTURES = {
    "cspl_report_dsl": [
        {"name": "positive_basic", "expect": "success", "expect_rows": 5,
         "program": {"report": "Все расхождения", "columns": ["Номер документа", "Контрагент", "Расхождение"],
                     "totals": ["Расхождение"], "out": "both"}},
        {"name": "positive_filter_group", "expect": "success", "expect_rows": 3,
         "program": {"report": "Крупные расхождения", "columns": ["Номер документа", "Контрагент", "Расхождение"],
                     "filter": {"field": "Расхождение", "op": ">", "value": 1000},
                     "group_by": "Контрагент", "totals": ["Расхождение"], "out": "both"}},
        {"name": "negative_no_report", "expect": "invalid", "expect_error_field": "report",
         "program": {"columns": ["Контрагент"]}},
        {"name": "negative_bad_filter_op", "expect": "invalid", "expect_error_field": "filter.op",
         "program": {"report": "X", "columns": ["Контрагент"],
                     "filter": {"field": "Расхождение", "op": "~=", "value": 1}}},
    ]
}


def run_handler(name, params, tok):
    r = sync.api("/api/expert/run", {"name": name, "global": True, "params": params}, tok, timeout=180)
    out = r.get("result", r)
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except Exception:
            try:
                import ast
                out = ast.literal_eval(out)   # платформа отдаёт result питоновским repr, не JSON
            except Exception:
                pass
    return out if isinstance(out, dict) else {"status": "error", "raw": str(out)[:200]}


def bridge_compile(payload):
    import urllib.request
    req = urllib.request.Request("http://127.0.0.1:8765/x/cspl_compile",
                                 data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=300).read().decode("utf-8"))


def register_pipeline_dsl(tok):
    """S2: pipeline_dsl — компилятор в мосту (_make_orchestrator). Fixtures через /x/cspl_compile.
    Позитив компилирует ВРЕМЕННЫЙ эксперт cspltest_run_pipeline (детерминизм по code_sha256)."""
    print("fixtures cspl_pipeline_dsl (via bridge):")
    failed = 0
    prog_ok = {"pipeline": "cspltest", "stages": ["uc_parse_invoices_acts", "uc_anonymize_invoice_data"],
               "kp_stages": []}
    r1 = bridge_compile({"handler_id": "cspl_pipeline_dsl", "program": prog_ok})
    ok1 = r1.get("status") == "success" and r1.get("orchestrator") == "cspltest_run_pipeline" and r1.get("code_sha256")
    print(("  ✓ " if ok1 else "  ✗ ") + "positive_compile → " + str(r1.get("status")) + " · " + str(r1.get("orchestrator")))
    failed += 0 if ok1 else 1
    r2 = bridge_compile({"handler_id": "cspl_pipeline_dsl", "program": prog_ok})
    det = ok1 and r2.get("code_sha256") == r1.get("code_sha256")
    print(("  ✓ " if det else "  ✗ ") + "determinism (code_sha256 повтора совпал)")
    failed += 0 if det else 1
    r3 = bridge_compile({"handler_id": "cspl_pipeline_dsl", "action": "validate",
                         "program": {"pipeline": "cspltest", "stages": ["no_such_expert_xyz"]}})
    ok3 = r3.get("status") == "invalid" and any("не найден" in str(e.get("message")) for e in r3.get("errors", []))
    print(("  ✓ " if ok3 else "  ✗ ") + "negative_missing_stage → " + str(r3.get("status")))
    failed += 0 if ok3 else 1
    r4 = bridge_compile({"handler_id": "cspl_pipeline_dsl", "action": "validate",
                         "program": {"pipeline": "cspltest", "stages": ["uc_parse_invoices_acts"],
                                     "kp_stages": ["uc_validate_vat_compliance"]}})
    ok4 = r4.get("status") == "invalid" and any(e.get("field") == "kp_stages" for e in r4.get("errors", []))
    print(("  ✓ " if ok4 else "  ✗ ") + "negative_kp_not_subset → " + str(r4.get("status")))
    failed += 0 if ok4 else 1
    # уборка временного артефакта компиляции (best-effort)
    try:
        sync.api("/api/expert/delete", {"name": "cspltest_run_pipeline", "global": True}, tok)
        print("  · временный cspltest_run_pipeline удалён")
    except Exception as e:
        print("  · временный эксперт не удалился (не блокер):", str(e)[:60])
    if failed:
        print("fixtures FAILED: %d — регистрация ЗАБЛОКИРОВАНА" % failed)
        sys.exit(1)
    reg = {}
    try:
        g = sync.api("/api/kv/get", {"key": "cspl:registry"}, tok)
        reg = json.loads(g.get("value") or "{}")
    except Exception:
        pass
    handlers = reg.get("handlers") or {}
    handlers["cspl_pipeline_dsl"] = {
        "handler_id": "cspl_pipeline_dsl", "version": "1.0.0", "kind": "pipeline",
        "executor": "bridge:_make_orchestrator",
        "description": "pipeline_dsl: программа {pipeline, stages[], kp_stages[]} → исполняемый эксперт-оркестратор <ns>_run_pipeline (контракт F2: rules_json/fields_json); валидация включает существование стадий на платформе",
        "compiles_to": ["expert(orchestrator)"],
        "fixtures": [{"name": "positive_compile", "ok": ok1}, {"name": "determinism", "ok": det},
                     {"name": "negative_missing_stage", "ok": ok3}, {"name": "negative_kp_not_subset", "ok": ok4}],
        "determinism_code_sha256": r1.get("code_sha256"),
        "program_example": prog_ok,
    }
    sync.api("/api/kv/set", {"key": "cspl:registry",
                             "value": json.dumps({"v": 0, "handlers": handlers}, ensure_ascii=False),
                             "description": "CSPL Studio registry v0"}, tok)
    print("зарегистрирован: cspl_pipeline_dsl v1.0.0 · handlers в реестре:", len(handlers))


def main():
    handler = sys.argv[1] if len(sys.argv) > 1 else "cspl_report_dsl"
    tok = sync.token()
    if handler == "cspl_pipeline_dsl":
        register_pipeline_dsl(tok)
        return
    fixtures = FIXTURES.get(handler)
    if not fixtures:
        raise SystemExit("нет fixtures для " + handler)
    results = []
    failed = 0
    md_digests = []
    for fx in fixtures:
        params = {"action": "compile", "program_json": json.dumps(fx["program"], ensure_ascii=False),
                  "records_json": json.dumps(RECORDS, ensure_ascii=False),
                  "output_dir": "/tmp/cspl_fx_" + fx["name"]}
        out = run_handler(handler, params, tok)
        ok = out.get("status") == fx["expect"]
        if ok and fx["expect"] == "success" and out.get("rows") != fx["expect_rows"]:
            ok = False
        if ok and fx["expect"] == "invalid":
            flds = [e.get("field") for e in (out.get("errors") or [])]
            ok = fx["expect_error_field"] in flds
        if fx["name"] == "positive_filter_group" and out.get("status") == "success":
            md_digests.append((out.get("outputs") or {}).get("md_sha256"))
        results.append({"fixture": fx["name"], "ok": ok, "got": out.get("status"),
                        "rows": out.get("rows"), "errors": out.get("errors")})
        print(("  ✓ " if ok else "  ✗ ") + fx["name"] + " → " + str(out.get("status")) +
              (" rows=" + str(out.get("rows")) if out.get("rows") is not None else ""))
        if not ok:
            failed += 1

    # детерминизм: повторная компиляция того же — байт-в-байт одинаковый md
    fx2 = fixtures[1]
    out2 = run_handler(handler, {"action": "compile",
                                 "program_json": json.dumps(fx2["program"], ensure_ascii=False),
                                 "records_json": json.dumps(RECORDS, ensure_ascii=False),
                                 "output_dir": "/tmp/cspl_fx_repeat"}, tok)
    md_digests.append((out2.get("outputs") or {}).get("md_sha256"))
    det = len(md_digests) == 2 and md_digests[0] and md_digests[0] == md_digests[1]
    print(("  ✓ " if det else "  ✗ ") + "determinism (md sha256 повторного прогона совпал)")
    if not det:
        failed += 1

    if failed:
        print("fixtures FAILED: %d — регистрация ЗАБЛОКИРОВАНА (канон вета)" % failed)
        sys.exit(1)

    # регистрация в cspl:registry
    reg = {}
    try:
        g = sync.api("/api/kv/get", {"key": "cspl:registry"}, tok)
        reg = json.loads(g.get("value") or "{}")
    except Exception:
        reg = {}
    handlers = reg.get("handlers") or {}
    ver = run_handler(handler, {"action": "validate",
                                "program_json": json.dumps(fixtures[0]["program"], ensure_ascii=False)}, tok).get("version", "1.0.0")
    handlers[handler] = {
        "handler_id": handler, "version": ver, "kind": "report",
        "description": "report_dsl: программа-описание отчёта (JSON) → детерминированный .md/.xlsx; фильтр в семантике правил владельца (F2)",
        "compiles_to": ["md", "xlsx"],
        "fixtures": [{"name": r["fixture"], "ok": r["ok"]} for r in results] + [{"name": "determinism", "ok": det}],
        "determinism_md_sha256": md_digests[0],
        "program_example": fixtures[1]["program"],
    }
    sync.api("/api/kv/set", {"key": "cspl:registry",
                             "value": json.dumps({"v": 0, "handlers": handlers}, ensure_ascii=False),
                             "description": "CSPL Studio registry v0"}, tok)
    print("зарегистрирован:", handler, "v" + ver, "· handlers в реестре:", len(handlers))


if __name__ == "__main__":
    main()
