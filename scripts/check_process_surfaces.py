#!/usr/bin/env python3
"""Proof that Wizard, Chat, Composer and Workspace adapt one canonical UPC sidecar."""
import ast
import importlib.util
import json
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui"
SERVER = ROOT / "ui" / "server.py"
WIZARD = ROOT / "ui" / "wizard.html"
WORKSPACE_SERVER = ROOT / "dist" / "workspace" / "server.py"
WORKSPACE_HTML = ROOT / "dist" / "workspace" / "index.html"
sys.path.insert(0, str(UI))

from wz_process import (atomic_write_json, checkpoint, process_from_blueprint,  # noqa: E402
                        process_status)


def composer_function(sess_dir):
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
              and node.name == "_composer_process_contract")
    ns = {
        "datetime": datetime, "timezone": timezone, "uuid": uuid, "SESS_DIR": sess_dir,
        "qwen_agent": lambda: "agent_qwen", "process_from_blueprint": process_from_blueprint,
        "process_checkpoint": checkpoint, "universal_process_status": process_status,
        "process_atomic_write": atomic_write_json,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
                 "server.py", "exec"), ns)
    return ns["_composer_process_contract"]


def load_workspace_server():
    spec = importlib.util.spec_from_file_location("workspace_process_adapter", WORKSPACE_SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    server_text = SERVER.read_text(encoding="utf-8")
    wizard_text = WIZARD.read_text(encoding="utf-8")
    workspace_text = WORKSPACE_HTML.read_text(encoding="utf-8")
    assert 'elif path == "/x/process":' in server_text
    assert 'elif self.path == "/x/process_action":' in server_text
    assert 'surface": "chat"' in server_text and "process_repair_step" in server_text
    assert 'response["process_contract"] = upc' in server_text
    assert 'jpost("/x/process_action"' in wizard_text
    assert "upcProcesses" in workspace_text and "api('/process_action'" in workspace_text

    with tempfile.TemporaryDirectory(prefix="upc_surfaces_") as td:
        sessions = Path(td)
        compose = composer_function(sessions)
        created = compose("Проверь файл неизвестным способом и подготовь отчёт", {
            "status": "success", "steps": [{"expert": "known_reader", "title": "Прочитать файл"}],
            "missing": ["Новая проверка, которой нет в каталоге"],
            "card": {"name": "Проверка файла"},
        })
        sid = created["session_id"]
        graph = json.loads((sessions / (sid + "_process.json")).read_text(encoding="utf-8"))
        assert graph["process_id"] == created["process_id"]
        assert [x["implementation"]["mode"] for x in graph["steps"]] == ["reuse", "generate"]
        assert graph["steps"][1]["dependencies"] == [graph["steps"][0]["id"]]
        session = json.loads((sessions / (sid + ".json")).read_text(encoding="utf-8"))
        assert session["process_contract"]["path"] == str(sessions / (sid + "_process.json"))

        graph["steps"][0]["status"] = "succeeded"
        graph["steps"][0]["artifact_refs"] = [{"kind": "step_result_json", "path": "/tmp/result.json"}]
        graph["steps"][0]["evidence"] = [{"criterion": "read", "passed": True}]
        calls = []
        workspace = load_workspace_server()

        def fake_wizard(path, body=None, timeout=20):
            calls.append((path, body))
            if path == "/x/sessions":
                return {"sessions": [{"session_id": sid, "client_name": "Проверка файла",
                                       "process_id": graph["process_id"], "updated_at": "2026-01-01"}]}
            if path.startswith("/x/process?"):
                return {"status": "success", "process_status": "running", "process": graph,
                        "events": [{"type": "step_accepted", "step_id": graph["steps"][0]["id"]}]}
            if path == "/x/process_action":
                return {"status": "success", "action": body.get("action"), "step_id": body.get("step_id")}
            raise AssertionError(path)

        workspace._wizard_call = fake_wizard
        listing = workspace.process_list(10)
        assert listing["source"] == "wizard-upc/1.0" and len(listing["processes"]) == 1
        projected = listing["processes"][0]
        assert projected["process_id"] == graph["process_id"]
        assert projected["steps"][0]["input_contract"] == graph["steps"][0]["input_contract"]
        assert projected["steps"][0]["artifacts"][0]["path"] == "/tmp/result.json"
        assert projected["steps"][0]["evidence"][0]["passed"] is True
        assert projected["events"][0]["type"] == "step_accepted"
        action = workspace.process_action({"session_id": sid, "action": "repair",
                                           "step_id": graph["steps"][1]["id"]})
        assert action["status"] == "success"
        assert calls[-1][0] == "/x/process_action" and calls[-1][1]["surface"] == "workspace"

    print("Process surfaces: Wizard + Chat + Composer + Workspace share one UPC sidecar ✓")


if __name__ == "__main__":
    main()
