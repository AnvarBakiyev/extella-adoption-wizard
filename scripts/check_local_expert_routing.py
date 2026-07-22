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


def extracted_llm_runner(calls, local=False):
    tree = ast.parse(LLM)
    names = {"_llm_backend_down", "llm_transient_error", "run_llm_expert"}
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]

    def run_expert(name, params, **kwargs):
        calls.append((name, params, kwargs))
        return {"status": "success"}

    ns = {"qwen_agents": lambda: ["agent_qwen"], "run_expert": run_expert,
          "local_system_expert_available": lambda _name: local,
          "run_local_system_expert": lambda _name, _params: {"status": "success"},
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
    remote_calls, local_calls = [], []
    runner = extracted_llm_runner(remote_calls, local=True)
    runner.__globals__["run_local_system_expert"] = lambda name, params: (
        local_calls.append((name, params)) or {"status": "success", "local": True})
    assert runner("wz_build_plan", {"session_id": "wz_local"})["local"] is True
    assert len(local_calls) == 1 and not remote_calls
    assert 'error_code = "plan_transport_failed" if llm_transient_error(r) else "plan_failed"' in BUILD
    assert '"plan_transport_failed": ("Связь прервалась во время составления плана"' in BUILD
    assert '_local_file_experts = {"wz_session", "wz_generate_blueprint", "wz_project_spec", "wz_data_reality_check"}' in SERVER
    assert 'run_llm_expert(expert, params, target=_local_tgt)' in SERVER
    assert 'target=(_local_tgt or None)' in SERVER
    assert 'params.setdefault("session_path", str(SESS_DIR / (_sid_plan + ".json")))' in SERVER
    assert 'params.setdefault("output_path", str(SESS_DIR / (_sid_plan + "_blueprint.json")))' in SERVER
    for name in ("wz_llm.py", "wz_local_experts.py", "wz_platform.py", "wz_process.py", "wizard.html"):
        assert name in DELTA
    assert 'check_local_system_expert_bundle.py' in DELTA
    assert '"$PY" "$SRC/install.py"' not in DELTA
    assert 'SYS_EXPERT_DIR="$APP_DIR/system_experts"' in DELTA
    assert SERVER.count('run_local_system_expert("wz_auto_compose"') >= 1
    print("локальные эксперты: signed bundle + Qwen reasoning + явные пути ✓")


if __name__ == "__main__":
    main()
