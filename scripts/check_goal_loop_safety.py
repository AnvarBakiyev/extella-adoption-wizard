#!/usr/bin/env python3
"""Регрессия границы: экспериментальный Контур не подменяет production Builder."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "ui" / "server.py"
WIZARD = ROOT / "ui" / "wizard.html"
AGENTIC = ROOT / "ui" / "wz_agentic.py"


def extract_function(tree, name):
    return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)


def run_gate(node, qwen, verdict):
    ns = {"qwen_agent": lambda: qwen, "_agent_json": lambda *a, **k: verdict}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 str(SERVER), "exec"), ns)
    return ns["_step_gate"]("goal", "step", "result")


def main():
    source = SERVER.read_text(encoding="utf-8")
    html = WIZARD.read_text(encoding="utf-8")
    agentic = AGENTIC.read_text(encoding="utf-8")
    tree = ast.parse(source)
    gate = extract_function(tree, "_step_gate")

    # Пустой Qwen, пустой JSON и явный отказ всегда закрывают гейт.
    assert run_gate(gate, "", {})["ok"] is False
    assert run_gate(gate, "agent", {})["ok"] is False
    assert run_gate(gate, "agent", {"ok": False, "why": "bad"})["ok"] is False
    accepted = run_gate(gate, "agent", {"ok": True, "why": "proved",
                                         "concepts": [{"k": "x", "v": "y"}], "rules": ["r"]})
    assert accepted["ok"] is True and accepted["concepts"] == [{"k": "x", "v": "y"}]

    # Мозг обновляется только после доказанного done, не при need_human/max_iters/failed.
    assert 'if ok and out.get("status") == "done" and isinstance(out.get("memory"), dict):' in source
    assert 'if ok and isinstance(out.get("memory"), dict):' not in source

    # Acquire только показывает кандидата: не обещает установку/автопродолжение.
    assert "Это только кандидат: Контур его не устанавливал" in source
    assert "подтвердите, и я добавлю способность в контур" not in source

    # UI честно отделяет локальный sandbox от эксперта Extella и показывает terminal stops.
    assert "локального sandbox-шага (не эксперт Extella)" in html
    assert "Контур завершён: нужен человек" in html
    assert "автоматического продолжения нет" in html
    assert "Собрал и запустил эксперта" not in html

    # Production agentic path создаёт/читает/saves draft и запускает только через run_expert;
    # локальный _sandbox_run в нём не используется.
    assert "_create_or_update(draft_name" in agentic
    assert "run_expert(draft_name" in agentic
    assert "_promote_expert(draft_name" in agentic
    assert "_sandbox_run" not in agentic
    print("Контур: fail-closed, terminal stops, no fake expert/install, no unverified brain writes ✓")


if __name__ == "__main__":
    main()
