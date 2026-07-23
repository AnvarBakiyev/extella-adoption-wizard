#!/usr/bin/env python3
"""Регрессия UX карты: наружу должны выходить человеческие названия шагов, не только expert_name.

Тест извлекает чистые функции из ui/server.py через AST, не импортируя живой bridge и его config.json.
"""
import ast
import json
import re
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "ui" / "server.py"
WANTED = {"_placement_stages", "_placement_stage_labels"}


def load_functions(sess_dir):
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in WANTED]
    if {n.name for n in body} != WANTED:
        raise AssertionError("не найдены функции ярлыков карты в ui/server.py")
    module = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    ns = {"json": json, "Path": Path, "SAFE_ID": re.compile(r"^[A-Za-z0-9_-]+$"),
          "SESS_DIR": Path(sess_dir)}
    exec(compile(module, str(SERVER), "exec"), ns)
    return ns["_placement_stage_labels"]


def main():
    sid = "wz_20260720_labeltest"
    session = {"session_id": sid, "builds": [{"experts": [
        "eur_fetch_inbox_attachment", "eur_generate_comparison_report", "eur_legacy_step"]}]}
    plan = {"plan": {"tasks": [
        {"expert_name": "eur_fetch_inbox_attachment", "purpose": "Получение вложений из почты."},
        {"expert_name": "eur_generate_comparison_report", "title": "Сравнительный отчёт"},
    ]}}
    with tempfile.TemporaryDirectory() as td:
        Path(td, sid + "_build_plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        labels = load_functions(td)(session)
    assert labels["eur_fetch_inbox_attachment"] == "Получение вложений из почты"
    assert labels["eur_generate_comparison_report"] == "Сравнительный отчёт"
    assert labels["eur_legacy_step"] == "Legacy step"
    assert all(labels.get(stage) and labels[stage] != stage for stage in session["builds"][-1]["experts"])
    print("ярлыки карты: человеческие названия + фолбэк ✓")


if __name__ == "__main__":
    main()
