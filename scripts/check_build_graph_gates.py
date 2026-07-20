#!/usr/bin/env python3
"""Регрессия: линейный Строитель не маскирует DAG как последовательную цепочку."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "ui" / "wz_build.py"
WIZARD = ROOT / "ui" / "wizard.html"


def load_topology_check():
    tree = ast.parse(BUILD.read_text(encoding="utf-8"))
    body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_pipeline_topology"]
    if len(body) != 1:
        raise AssertionError("не найден гейт топологии в ui/wz_build.py")
    ns = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(BUILD), "exec"), ns)
    return ns["_pipeline_topology"]


def main():
    check = load_topology_check()
    linear = [
        {"id": "read", "depends_on": []},
        {"id": "check", "depends_on": ["read"]},
        {"id": "report", "depends_on": ["check"]},
    ]
    branched = [
        {"id": "pair", "depends_on": []},
        {"id": "excel", "depends_on": ["pair"]},
        {"id": "pdf", "depends_on": ["pair"]},
        {"id": "compare", "depends_on": ["excel", "pdf"]},
    ]
    assert check(linear)["supported"] is True
    result = check(branched)
    assert result["supported"] is False
    assert result["branches"] == ["pair"]
    assert result["joins"] == ["compare"]

    source = BUILD.read_text(encoding="utf-8")
    failure_gate = source.index("if failed_stage:")
    orchestrator = source.index("_make_orchestrator", failure_gate)
    assert failure_gate < orchestrator
    assert '"verdict": "not_run"' in source
    html = WIZARD.read_text(encoding="utf-8")
    assert 's.status==="error"&&s.detail' in html
    assert 's.status==="blocked"&&s.detail' in html
    assert "needs_agentic = len(sample_files) > 1 or not topology" in source
    print("стройка: DAG распознан; сложный идёт целиком в Qwen, линейный сбой не маскируется ✓")


if __name__ == "__main__":
    main()
