#!/usr/bin/env python3
"""Регрессия QA-дельты: фильтр install.py выбирает только явно изменённые артефакты."""
import ast
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.py"
DELTA = ROOT / "scripts" / "qa_delta_update.sh"


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
    delta = DELTA.read_text(encoding="utf-8")
    for name in ("server.py", "wz_agentic.py", "wz_build.py", "wz_llm.py", "wz_local_experts.py",
                 "wz_platform.py", "wz_process.py", "wizard.html"):
        assert name in delta, f"QA-дельта не переносит runtime-файл {name}"
    assert 'check_local_system_expert_bundle.py' in delta
    assert 'EXTELLA_DELTA_FILES=' not in delta
    assert '"$PY" "$SRC/install.py"' not in delta
    assert '.upc-system-experts-v1' not in delta
    assert 'SYS_EXPERT_DIR="$APP_DIR/system_experts"' in delta
    assert 'for name in wz_auto_compose.py wz_build_plan.py wz_generate_blueprint.py wz_session.py; do' in delta
    assert 'cp "$SRC/experts/$name" "$SYS_EXPERT_DIR/$name"' in delta
    install_text = INSTALL.read_text(encoding="utf-8")
    assert 'SYSTEM_AGENT_ID = cfg.get("system_agent_id") or "agent_extella_default"' in install_text
    assert '"X-Agent-Id": SYSTEM_AGENT_ID' in install_text
    assert '"X-Agent-Id": cfg.get("agent_id"' not in install_text
    assert ('BRIDGE_OWNED_EXPERTS = {"wz_auto_compose", "wz_build_plan", '
            '"wz_generate_blueprint", "wz_session"}' in install_text)
    assert 'if name in BRIDGE_OWNED_EXPERTS:' in install_text
    for name in ("dist/workspace/$name", "WS_DIR", "Workspace v1.1.0", "EXTELLA_QA_SHA"):
        assert name in delta, f"QA-дельта не обновляет общий Workspace-адаптер: {name}"
    print("QA-дельта: системный harness локален; пользовательский мозг не переустанавливается ✓")


if __name__ == "__main__":
    main()
