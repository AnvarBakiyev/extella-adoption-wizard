#!/usr/bin/env python3
"""Регрессия: формальный success не считается сохранённым планом и не открывает стройку."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SERVER = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
HTML = (ROOT / "ui" / "wizard.html").read_text(encoding="utf-8")


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
    print("план: success только после сохранения и структурной проверки ✓")


if __name__ == "__main__":
    main()
