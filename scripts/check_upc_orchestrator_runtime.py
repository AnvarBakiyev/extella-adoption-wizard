#!/usr/bin/env python3
"""Deterministic proof that the generated UPC expert executes DAG/checkpoint/HITL correctly."""
import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui"
sys.path.insert(0, str(UI))


def load_build(saved):
    platform = types.ModuleType("wz_platform")
    platform.CONFIG = {"auth_token": "test"}
    platform.BASE = "https://example.invalid"
    platform.api = lambda path, body, **kwargs: (saved.append((path, body)) or {"status": "success", "id": "x"})
    platform.run_expert = lambda *args, **kwargs: {}
    platform.qwen_agent = lambda: "agent_qwen"
    llm = types.ModuleType("wz_llm")
    llm.run_llm_expert = lambda *args, **kwargs: {}
    llm.design_agent = lambda: "agent_design"
    llm.gen_panel_manifest = lambda *args, **kwargs: {}
    llm.llm_transient_error = lambda _result: False
    agentic = types.ModuleType("wz_agentic")
    agentic.build_agentic_solution = lambda *args, **kwargs: {}
    agentic.prepare_task_context = lambda *args, **kwargs: {}
    agentic._builder_brief = lambda value: value
    sys.modules.update({"wz_platform": platform, "wz_llm": llm, "wz_agentic": agentic})
    spec = importlib.util.spec_from_file_location("wz_build_upc_test", UI / "wz_build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Response:
    def __init__(self, value):
        self.value = value

    def json(self):
        return {"result": self.value}


def main():
    saved = []
    mod = load_build(saved)
    graph = {
        "schema": "upc/1.0", "process_id": "proc_test", "version": 1, "title": "DAG",
        "steps": [
            {"id": "left", "title": "Left", "dependencies": [], "version": 1,
             "implementation": {"mode": "generate", "expert_ref": "left_v1"}, "permissions": {}},
            {"id": "right", "title": "Right", "dependencies": [], "version": 1,
             "implementation": {"mode": "llm_worker", "expert_ref": "right_v1"}, "permissions": {}},
            {"id": "merge", "title": "Merge", "dependencies": ["left", "right"], "version": 1,
             "implementation": {"mode": "generate", "expert_ref": "merge_v1"}, "permissions": {}},
        ],
    }
    name, result, code = mod._make_upc_orchestrator("qa", graph, "/tmp/upc-default")
    assert name == "qa_run_process" and result.get("status") == "success"
    assert saved and saved[-1][1]["name"] == name
    source = "\n".join(code.splitlines()[2:])
    compile(source, "upc_orchestrator.cspl", "exec")

    calls = []
    fail_merge_once = {"value": True}

    def post(url, headers=None, json=None, timeout=None):
        expert = json["expert_name"]
        calls.append(expert)
        if expert == "merge_v1" and fail_merge_once["value"]:
            fail_merge_once["value"] = False
            return Response("[Execution Error] merge failed")
        out = Path(json["params"]["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        artifact = out / "step_result.json"
        artifact.write_text(json_module.dumps({"expert": expert}), encoding="utf-8")
        return Response({"status": "success", "summary": {"expert": expert, "processed_files": 1},
                         "output": {"expert": expert},
                         "evidence": {"files_used": [Path(json["params"]["source_file"]).name],
                                      "acceptance_checks": [{"criterion": "ran", "passed": True,
                                                             "evidence": expert}]},
                         "artifacts": [{"kind": "step_result_json", "path": str(artifact)}]})

    fake_requests = types.ModuleType("requests")
    fake_requests.post = post
    old_requests = sys.modules.get("requests")
    sys.modules["requests"] = fake_requests
    namespace = {"__name__": "upc_generated"}
    try:
        exec(source, namespace)
        run = namespace[name]
        with tempfile.TemporaryDirectory(prefix="upc_orch_") as td:
            root = Path(td)
            inp = root / "input.json"
            inp.write_text("{}", encoding="utf-8")
            first = run(str(inp), str(root), "token", run_id="r1")
            assert first["status"] == "error" and first["failed_step"] == "merge", first
            assert calls == ["left_v1", "right_v1", "merge_v1"], calls
            calls.clear()
            resumed = run(str(inp), str(root), "token", run_id="r1")
            assert resumed["status"] == "success", resumed
            assert calls == ["merge_v1"], calls
            calls.clear()
            again = run(str(inp), str(root), "token", run_id="r1")
            assert again["status"] == "success" and calls == [], (again, calls)
    finally:
        if old_requests is None:
            sys.modules.pop("requests", None)
        else:
            sys.modules["requests"] = old_requests

    risky = {"schema": "upc/1.0", "process_id": "proc_risky", "version": 1, "title": "Risky",
             "steps": [{"id": "move", "title": "Move", "dependencies": [], "version": 1,
                        "implementation": {"mode": "generate", "expert_ref": "move_v1"},
                        "permissions": {"move": ["Downloads -> quarantine"]}}]}
    _, _, risky_code = mod._make_upc_orchestrator("risk", risky, "/tmp/upc-risk")
    risky_source = "\n".join(risky_code.splitlines()[2:])
    compile(risky_source, "upc_risky.cspl", "exec")
    old_requests = sys.modules.get("requests")
    sys.modules["requests"] = fake_requests
    try:
        risky_ns = {"__name__": "upc_risky_generated"}
        exec(risky_source, risky_ns)
        with tempfile.TemporaryDirectory(prefix="upc_risky_") as td:
            root = Path(td)
            inp = root / "input.json"; inp.write_text("{}", encoding="utf-8")
            calls.clear()
            blocked = risky_ns["risk_run_process"](str(inp), str(root), "token", run_id="r2")
            assert blocked["status"] == "blocked_human" and calls == [], blocked
            approved = risky_ns["risk_run_process"](
                str(inp), str(root), "token", run_id="r2",
                approval_json=json.dumps({"move:v1": True}))
            assert approved["status"] == "success" and calls == ["move_v1"], (approved, calls)
    finally:
        if old_requests is None:
            sys.modules.pop("requests", None)
        else:
            sys.modules["requests"] = old_requests
    print("UPC generated orchestrator: DAG + checkpoint + local resume + approval gate ✓")


if __name__ == "__main__":
    # Avoid shadowing the `json` request body parameter inside the fake transport.
    json_module = json
    main()
