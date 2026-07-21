#!/usr/bin/env python3
"""Регрессия: формальный success не считается сохранённым планом и не открывает стройку."""
import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SERVER = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
HTML = (ROOT / "ui" / "wizard.html").read_text(encoding="utf-8")
PLATFORM = (ROOT / "ui" / "wz_platform.py").read_text(encoding="utf-8")


def extracted_validator():
    tree = ast.parse(SERVER)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == "_blueprint_doc_usable")
    ns = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
                 "server.py", "exec"), ns)
    return ns["_blueprint_doc_usable"]


def main():
    usable = extracted_validator()
    valid = {"blueprint": {"process_name": "Проверка заявок",
                           "stages": [{"id": "read", "title": "Чтение"}],
                           "suitability": {"score": 80}}}
    assert usable(valid)
    assert not usable(None)
    assert not usable({"blueprint": {}})
    assert not usable({"blueprint": {"process_name": "X", "stages": [],
                                      "suitability": {"score": 80}}})
    assert not usable({"blueprint": {"process_name": "X", "stages": [{"id": "s"}],
                                      "suitability": {}}})
    assert 'code": "blueprint_not_saved"' in SERVER
    assert "if not _blueprint_doc_usable(_build_bp_doc):" in SERVER
    assert "function usableBlueprintDoc(doc)" in HTML
    assert "if(!usableBlueprintDoc(saved))" in HTML
    assert "${r.suitability_score}/100" not in HTML
    ptree = ast.parse(PLATFORM)
    pfn = next(n for n in ptree.body if isinstance(n, ast.FunctionDef)
               and n.name == "parse_expert_result")
    pns = {"json": json, "ast": ast, "_scrub": lambda s: s}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[pfn], type_ignores=[])),
                 "wz_platform.py", "exec"), pns)
    parsed = pns["parse_expert_result"]({"status": "completed",
                                          "result": "[Execution Error] name 'layer' is not defined"})
    assert parsed["status"] == "error" and parsed["code"] == "expert_execution_error"
    print("план: success только после сохранения и структурной проверки ✓")


if __name__ == "__main__":
    main()
