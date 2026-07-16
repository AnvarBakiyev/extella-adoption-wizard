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


def main():
    handler = sys.argv[1] if len(sys.argv) > 1 else "cspl_report_dsl"
    fixtures = FIXTURES.get(handler)
    if not fixtures:
        raise SystemExit("нет fixtures для " + handler)
    tok = sync.token()
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
