#!/usr/bin/env python3
"""Регрессия отчётов: артефакт переживает /tmp и восстанавливается из общего стора.

Чистые функции извлекаются из ui/server.py через AST: живой bridge, config и API не нужны.
"""
import ast
import base64
import json
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "ui" / "server.py"
WANTED = {"_persist_run_reports", "_materialize_from_store"}


def load_functions(report_dir, store, synced):
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in WANTED]
    if {n.name for n in body} != WANTED:
        raise AssertionError("не найдены функции долговечного отчёта в ui/server.py")

    def file_key(sid, name):
        return "file:%s:%s" % (sid, name)

    def sync_file(sid, path):
        p = Path(path)
        raw = p.read_bytes()
        base = file_key(sid, p.name)
        store[base + ":0"] = base64.b64encode(raw).decode()
        store[base + ":meta"] = json.dumps(
            {"name": p.name, "chunks": 1, "bytes": len(raw), "enc": False}
        )
        synced.append((sid, str(p)))
        return base

    def api(_path, payload):
        return {"value": store.get(payload.get("key"), "")}

    module = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    ns = {
        "Path": Path,
        "json": json,
        "REPORTS_DIR": Path(report_dir),
        "_REPORT_KEYS": ("report_xlsx", "report_pdf", "report_docx", "report_pptx", "report_md"),
        "_ns": lambda sid: "safe_" + str(sid),
        "_sync_file_to_store": sync_file,
        "_file_key": file_key,
        "api": api,
        "_vault_fernet": lambda **_kw: None,
    }
    exec(compile(module, str(SERVER), "exec"), ns)
    return ns["_persist_run_reports"], ns["_materialize_from_store"]


def main():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        source = root / "tmp" / "comparison.xlsx"
        source.parent.mkdir()
        expected = b"durable-report-regression"
        source.write_bytes(expected)
        store, synced = {}, []
        persist, materialize = load_functions(root / "reports", store, synced)

        result = {"status": "success", "report_xlsx": str(source)}
        persist("wz_report_test", result)
        durable = Path(result["report_xlsx"])
        assert durable != source and durable.is_file()
        assert durable.read_bytes() == expected
        assert synced == [("wz_report_test", str(durable))]

        durable.unlink()
        restored = materialize("wz_report_test", source.name, root / "restored")
        assert restored and Path(restored).read_bytes() == expected
        print("отчёт: /tmp → долговечная папка → общий стор → другое устройство ✓")


if __name__ == "__main__":
    main()
