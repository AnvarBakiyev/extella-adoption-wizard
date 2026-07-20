#!/usr/bin/env python3
"""Регрессия QA-дельты: фильтр install.py выбирает только явно изменённые артефакты."""
import ast
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.py"


def main():
    tree = ast.parse(INSTALL.read_text(encoding="utf-8"))
    wanted = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "selected"]
    if len(wanted) != 1:
        raise AssertionError("в install.py нет delta-фильтра selected")
    ns = {"os": os, "HERE": str(ROOT),
          "DELTA_FILES": {"experts/wz_build_plan.py", "concepts/changed.md"}}
    exec(compile(ast.fix_missing_locations(ast.Module(body=wanted, type_ignores=[])), str(INSTALL), "exec"), ns)
    selected = ns["selected"]
    assert selected(ROOT / "experts" / "wz_build_plan.py")
    assert selected(ROOT / "concepts" / "changed.md")
    assert not selected(ROOT / "experts" / "wz_session.py")
    assert not selected(ROOT / "concepts" / "unchanged.md")
    ns["DELTA_FILES"].clear()
    assert selected(ROOT / "experts" / "wz_session.py")  # полный установщик обратно совместим
    print("QA-дельта: изменённые артефакты выбраны, полный режим сохранён ✓")


if __name__ == "__main__":
    main()
