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
    agentic.derive_step_context = lambda prepared, *_args, **_kwargs: prepared
    agentic.prepare_task_context = lambda *args, **kwargs: {}
    agentic._builder_brief = lambda value: value
    sys.modules.update({"wz_platform": platform, "wz_llm": llm, "wz_agentic": agentic})
    spec = importlib.util.spec_from_file_location("wz_build_upc_test", UI / "wz_build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Response:
    def __init__(self, value, as_string=False):
        self.value = value
        self.as_string = as_string

    def json(self):
        return {"result": repr(self.value) if self.as_string else self.value}


def main():
    saved = []
    mod = load_build(saved)
    graph = {
        "schema": "upc/1.0", "process_id": "proc_test", "version": 1, "title": "DAG",
        "steps": [
            {"id": "left", "title": "Left", "dependencies": [], "version": 1,
             "input_contract": {"required": True, "data_schema": {"format": "json"}},
             "implementation": {"mode": "generate", "expert_ref": "left_v1",
                                "capability_ref": None}, "permissions": {}},
            {"id": "right", "title": "Right", "dependencies": [], "version": 1,
             "input_contract": {"required": True, "data_schema": {"format": "pdf"}},
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
        assert out.is_dir(), "orchestrator must create output_dir before expert run"
        out.mkdir(parents=True, exist_ok=True)
        source_path = Path(json["params"]["source_file"])
        if expert == "left_v1":
            assert source_path.name == "input.json", source_path
        elif expert == "right_v1":
            assert source_path.name == "certificate.pdf", source_path
        artifact = out / "step_result.json"
        artifact.write_text(json_module.dumps({"expert": expert}), encoding="utf-8")
        artifacts = [{"kind": "step_result_json", "path": str(artifact)}]
        summary = {"expert": expert, "processed_files": 1}
        output = {"expert": expert}
        if expert == "right_v1":
            output_only = out / "right_rows.json"
            output_only.write_text('[{"right": true}]', encoding="utf-8")
            output["right_rows_json"] = str(output_only)
            # The comparison worker owns business counters; the terminal formatter below
            # deliberately returns only report paths.
            summary["counts"] = {
                "match_count": 1, "only_excel_count": 1, "only_pdf_count": 1}
        if expert == "merge_v1":
            dependency_dir = Path(json["params"]["source_file"])
            dependency_names = {path.name for path in dependency_dir.iterdir() if path.is_file()}
            assert any(name.endswith("right_rows.json") for name in dependency_names), dependency_names
            assert (dependency_dir / "right_rows.json").read_text() == '[{"right": true}]'
            left_canonical = json_module.loads(
                (dependency_dir / "001_left__canonical_step_result.json").read_text())
            assert left_canonical["structured_data"]["expert"] == "left_v1", left_canonical
            assert left_canonical["summary"]["expert"] == "left_v1", left_canonical
            report_md = out / "report.md"; report_md.write_text("# report", encoding="utf-8")
            report_xlsx = out / "report.xlsx"; report_xlsx.write_bytes(b"xlsx")
            artifacts.append({"kind": "report_md", "path": str(report_md)})
            # Deliberately expose XLSX only through output and with a domain-specific key.
            output["discrepancy_report_xlsx"] = str(report_xlsx)
        return Response({"status": "success", "summary": summary,
                         "output": output,
                         "evidence": {"files_used": [Path(json["params"]["source_file"]).name],
                                      "acceptance_checks": [{"criterion": "ran", "passed": True,
                                                             "evidence": expert}]},
                         "artifacts": artifacts}, as_string=(expert == "left_v1"))

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
            inp = root / "source"
            inp.mkdir()
            (inp / "certificate.pdf").write_bytes(b"%PDF-1.7")
            inp = inp / "input.json"
            inp.write_text("{}", encoding="utf-8")
            source_dir = inp.parent
            first = run(str(source_dir), str(root), "token", run_id="r1")
            assert first["status"] == "error" and first["failed_step"] == "merge", first
            assert calls == ["left_v1", "right_v1", "merge_v1"], calls
            calls.clear()
            resumed = run(str(source_dir), str(root), "token", run_id="r1")
            assert resumed["status"] == "success", resumed
            assert calls == ["merge_v1"], calls
            assert resumed["summary"]["processed_files"] == 2
            assert resumed["summary"]["matches"] == 1, resumed["summary"]
            assert resumed["summary"]["only_excel"] == 1, resumed["summary"]
            assert resumed["summary"]["only_pdf"] == 1, resumed["summary"]
            assert resumed["report_md"].endswith("report.md")
            assert resumed["report_xlsx"].endswith("report.xlsx")
            calls.clear()
            again = run(str(source_dir), str(root), "token", run_id="r1")
            assert again["status"] == "success" and calls == [], (again, calls)
            checkpoint = json.loads((root / "upc_runs" / "r1" / "checkpoint.json").read_text(
                encoding="utf-8"))
            assert all(row.get("input_sha256") and row.get("step_contract_sha256") and
                       row.get("checkpoint_sha256") for row in checkpoint["accepted"].values())
            # Changing one scoped root input invalidates only that branch and its merge.
            inp.write_text('{"changed":true}', encoding="utf-8")
            calls.clear()
            changed = run(str(source_dir), str(root), "token", run_id="r1")
            assert changed["status"] == "success"
            assert calls == ["left_v1", "merge_v1"], calls
            # Tampering one branch artifact replays only that branch and its dependent merge.
            checkpoint = json.loads((root / "upc_runs" / "r1" / "checkpoint.json").read_text(
                encoding="utf-8"))
            left_artifact = Path(checkpoint["accepted"]["left"]["artifacts"][0]["path"])
            left_artifact.write_text("tampered", encoding="utf-8")
            calls.clear()
            repaired = run(str(source_dir), str(root), "token", run_id="r1")
            assert repaired["status"] == "success"
            assert calls == ["left_v1", "merge_v1"], calls
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
