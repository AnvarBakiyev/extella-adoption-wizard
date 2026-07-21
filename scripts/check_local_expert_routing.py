#!/usr/bin/env python3
"""Регрессия: файловые эксперты Wizard исполняются только на текущем Listener."""
import ast
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PLATFORM = (ROOT / "ui" / "wz_platform.py").read_text(encoding="utf-8")
LLM = (ROOT / "ui" / "wz_llm.py").read_text(encoding="utf-8")
SERVER = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
BUILD = (ROOT / "ui" / "wz_build.py").read_text(encoding="utf-8")
DELTA = (ROOT / "scripts" / "qa_delta_update.sh").read_text(encoding="utf-8")


def extracted_llm_runner(calls):
    tree = ast.parse(LLM)
    names = {"_llm_backend_down", "llm_transient_error", "run_llm_expert"}
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]

    def run_expert(name, params, **kwargs):
        calls.append((name, params, kwargs))
        return {"status": "success"}

    ns = {"qwen_agents": lambda: ["agent_qwen"], "run_expert": run_expert,
          "time": types.SimpleNamespace(sleep=lambda *_: None), "json": __import__("json")}
    exec(compile(ast.fix_missing_locations(ast.Module(body=fns, type_ignores=[])),
                 "wz_llm.py", "exec"), ns)
    return ns["run_llm_expert"]


def main():
    assert 'Path.home() / ".extella" / "device.txt"' in PLATFORM
    calls = []
    result = extracted_llm_runner(calls)("wz_generate_blueprint", {"session_id": "wz_test"},
                                         target="device-current")
    assert result["status"] == "success"
    assert len(calls) == 1 and calls[0][2].get("target") == "device-current"
    transient_calls = []
    outcomes = [{"status": "error", "message": "The read operation timed out"},
                {"status": "success"}]
    runner = extracted_llm_runner(transient_calls)
    runner.__globals__["run_expert"] = lambda name, params, **kwargs: (
        transient_calls.append((name, params, kwargs)) or outcomes.pop(0))
    assert runner("wz_build_plan", {"session_id": "wz_retry"})["status"] == "success"
    assert len(transient_calls) == 2
    deterministic_calls = []
    runner = extracted_llm_runner(deterministic_calls)
    runner.__globals__["run_expert"] = lambda name, params, **kwargs: (
        deterministic_calls.append((name, params, kwargs)) or
        {"status": "error", "message": "Failed to parse LLM JSON"})
    assert runner("wz_build_plan", {"session_id": "wz_bad"})["status"] == "error"
    assert len(deterministic_calls) == 1
    assert 'error_code = "plan_transport_failed" if llm_transient_error(r) else "plan_failed"' in BUILD
    assert '"plan_transport_failed": ("Связь прервалась во время составления плана"' in BUILD
    assert '_local_file_experts = {"wz_session", "wz_generate_blueprint", "wz_project_spec", "wz_data_reality_check"}' in SERVER
    assert 'run_llm_expert(expert, params, target=_local_tgt)' in SERVER
    assert 'target=(_local_tgt or None)' in SERVER
    assert 'params.setdefault("session_path", str(SESS_DIR / (_sid_plan + ".json")))' in SERVER
    assert 'params.setdefault("output_path", str(SESS_DIR / (_sid_plan + "_blueprint.json")))' in SERVER
    assert 'wz_llm.py wz_platform.py wz_process.py wizard.html' in DELTA
    assert 'experts/wz_generate_blueprint.py,experts/wz_build_plan.py,experts/wz_auto_compose.py' in DELTA
    print("локальные эксперты: target текущего Listener + явные пути + полная дельта модулей ✓")


if __name__ == "__main__":
    main()
