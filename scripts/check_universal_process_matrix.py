#!/usr/bin/env python3
"""Required UPC v1 deterministic acceptance matrix (no network model calls)."""
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))
from wz_process import (accept_step, answer_human, block_for_human, checkpoint,
                        expand_subgraph, memory_entry, normalize_step_result,
                        permission_preflight, process_from_blueprint, process_status,
                        record_approval, recover_after_restart, repair_step, step_map,
                        transition_step)


def blueprint(stages, goal="test"):
    return {"process_name": goal, "goal": goal, "stages": stages}


def artifact(root, name, value="ok"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def succeed(graph, sid, root, name=None, memory=None):
    step = step_map(graph)[sid]
    assert step["status"] == "ready", (sid, step["status"])
    transition_step(graph, sid, "running")
    path = artifact(root, name or (sid + ".json"), json.dumps({"step": sid}))
    raw = {"status": "success", "summary": {"step": sid}, "output": {"step": sid},
           "evidence": {"acceptance_checks": [{"criterion": "postcondition", "passed": True,
                                                 "evidence": "artifact exists"}]},
           "artifacts": [{"kind": "artifact", "path": str(path)}]}
    result = normalize_step_result(raw, sid, step["version"], len(step.get("attempts") or []) + 1)
    accepted = accept_step(graph, sid, result,
                           semantic_verdict={"verdict": "pass", "confidence": 1.0}, memory=memory)
    assert accepted["ok"], accepted
    return path


def test_01_downloads_preview_approval_move_report(root):
    ids = ["scan", "classify", "preview", "approval", "move", "report"]
    stages = []
    for index, sid in enumerate(ids):
        mode = "human" if sid == "approval" else "generate"
        permissions = {"read": ["Downloads"], "create": ["quarantine"]}
        if sid == "move":
            permissions["move"] = ["Downloads -> reversible quarantine"]
        stages.append({"id": sid, "title": sid, "depends_on": ids[index - 1:index],
                       "implementation_mode": mode, "permissions": permissions})
    graph = process_from_blueprint("wz_downloads", blueprint(stages, "Downloads cleanup"))
    for sid in ("scan", "classify", "preview"):
        succeed(graph, sid, root)
    approval = step_map(graph)["approval"]
    block_for_human(graph, "approval", "Подтвердите manifest перемещений")
    answer_human(graph, "approval", "Подтверждаю preview", approved=True)
    succeed(graph, "approval", root)
    move = step_map(graph)["move"]
    request = {"permission": "move", "target": "Downloads", "payload": {"manifest": "preview.json"}}
    denied = permission_preflight(graph, move, request)
    assert denied["code"] == "approval_required"
    record_approval(graph, "move", "move", "Downloads", request["payload"], True)
    assert permission_preflight(graph, move, request)["ok"]
    succeed(graph, "move", root); succeed(graph, "report", root)
    assert process_status(graph) == "succeeded"


def test_02_file_to_telegram_stops_without_token(root):
    graph = process_from_blueprint("wz_tg", blueprint([
        {"id": "read", "title": "read", "implementation_mode": "generate"},
        {"id": "transform", "title": "transform", "depends_on": ["read"],
         "implementation_mode": "llm_worker"},
        {"id": "send", "title": "Telegram", "depends_on": ["transform"],
         "implementation_mode": "human", "permissions": {"send": ["telegram"]}},
    ], "file to Telegram"))
    succeed(graph, "read", root); succeed(graph, "transform", root)
    block_for_human(graph, "send", "Подключите Telegram token")
    assert process_status(graph) == "blocked_human"
    assert step_map(graph)["send"]["human_gate"]["question"] == "Подключите Telegram token"


def test_03_excel_pdf_normalization_local_repair(root):
    graph = process_from_blueprint("wz_compare", blueprint([
        {"id": "excel", "title": "Excel", "implementation_mode": "generate"},
        {"id": "pdf", "title": "PDF", "implementation_mode": "generate"},
        {"id": "compare", "title": "Compare", "depends_on": ["excel", "pdf"],
         "implementation_mode": "generate"},
    ], "Excel PDF reconciliation"))
    succeed(graph, "excel", root); succeed(graph, "pdf", root)
    transition_step(graph, "compare", "running")
    failed = normalize_step_result("[Execution Error] Unicode NFD and numeric normalization", "compare", 1, 1)
    verdict = accept_step(graph, "compare", failed)
    assert not verdict["ok"] and step_map(graph)["compare"]["status"] == "repairing"
    repair_step(graph, "compare", "normalize NFC and int(float(value))")
    assert step_map(graph)["excel"]["status"] == "succeeded"
    assert step_map(graph)["pdf"]["status"] == "succeeded"
    assert step_map(graph)["compare"]["version"] == 2
    lesson = memory_entry("lesson", "NFC + numeric canonicalization required", status="candidate",
                          evidence_refs=["attempt:compare:v1"], step_id="compare", step_version=2)
    succeed(graph, "compare", root, memory=[lesson])


def test_04_parallel_branches_and_merge(root):
    graph = process_from_blueprint("wz_parallel", blueprint([
        {"id": "a", "title": "A", "implementation_mode": "generate"},
        {"id": "b", "title": "B", "implementation_mode": "generate"},
        {"id": "merge", "title": "Merge", "depends_on": ["a", "b"],
         "implementation_mode": "generate"},
    ]))
    assert {x["id"] for x in graph["steps"] if x["status"] == "ready"} == {"a", "b"}
    succeed(graph, "a", root); succeed(graph, "b", root)
    assert step_map(graph)["merge"]["status"] == "ready"
    succeed(graph, "merge", root)


def test_05_failure_does_not_replay_independent_success(root):
    graph = process_from_blueprint("wz_repair", blueprint([
        {"id": "left", "title": "left", "implementation_mode": "generate"},
        {"id": "right", "title": "right", "implementation_mode": "generate"},
        {"id": "join", "title": "join", "depends_on": ["left", "right"],
         "implementation_mode": "generate"},
    ]))
    succeed(graph, "left", root); succeed(graph, "right", root)
    transition_step(graph, "join", "running")
    failed = normalize_step_result({"status": "error", "message": "bad merge"}, "join", 1, 1)
    accept_step(graph, "join", failed)
    repair_step(graph, "join", "fix merge")
    assert len(step_map(graph)["left"]["attempts"]) == 1
    assert len(step_map(graph)["right"]["attempts"]) == 1
    succeed(graph, "join", root)
    assert len(step_map(graph)["left"]["attempts"]) == 1


def test_06_cspl_equals_llm(root):
    graph = process_from_blueprint("wz_llm", blueprint([
        {"id": "reason", "title": "Reason", "implementation_mode": "llm_worker",
         "acceptance": {"semantic_criteria": ["answer grounded in input"]}},
    ]))
    step = step_map(graph)["reason"]
    assert step["implementation"]["mode"] == "llm_worker"
    succeed(graph, "reason", root)


def test_07_delegate_creates_bounded_subgraph(root):
    graph = process_from_blueprint("wz_delegate", blueprint([
        {"id": "meta", "title": "Meta", "implementation_mode": "delegate"},
        {"id": "final", "title": "Final", "depends_on": ["meta"], "implementation_mode": "generate"},
    ]))
    transition_step(graph, "meta", "running")
    out = expand_subgraph(graph, "meta", [
        {"id": "inspect", "title": "Inspect", "implementation_mode": "generate"},
        {"id": "solve", "title": "Solve", "depends_on": ["inspect"], "implementation_mode": "llm_worker"},
    ], "meta expert requested decomposition", 1)
    assert len(out["added"]) == 2 and graph["version"] == 2
    # Accept the delegate; only the subgraph entry becomes ready, while final also waits for its terminal.
    path = artifact(root, "meta.json")
    raw = {"status": "success", "summary": {"planned": 2},
           "evidence": {"acceptance_checks": [{"criterion": "valid subgraph", "passed": True,
                                                 "evidence": "2 steps"}]},
           "artifacts": [{"kind": "artifact", "path": str(path)}]}
    accept_step(graph, "meta", normalize_step_result(raw, "meta", 1, 1))
    added = [step_map(graph)[sid] for sid in out["added"]]
    assert sum(x["status"] == "ready" for x in added) == 1
    assert step_map(graph)["final"]["status"] == "pending"


def test_08_restart_resumes_from_checkpoint(root):
    graph = process_from_blueprint("wz_restart", blueprint([
        {"id": "safe", "title": "safe", "implementation_mode": "generate"},
        {"id": "external", "title": "external", "implementation_mode": "generate",
         "permissions": {"external_write": ["CRM"]}},
    ]))
    transition_step(graph, "safe", "running"); transition_step(graph, "external", "running")
    events = recover_after_restart(graph)
    assert len(events) == 2
    assert step_map(graph)["safe"]["status"] == "ready"
    assert step_map(graph)["external"]["status"] == "blocked_human"
    checkpoint(graph, root / "restart.json", root / "restart.jsonl", {"type": "recovered"})
    restored = json.loads((root / "restart.json").read_text(encoding="utf-8"))
    assert step_map(restored)["safe"]["status"] == "ready"


def test_09_negative_guards(root):
    graph = process_from_blueprint("wz_negative", blueprint([
        {"id": "x", "title": "x", "implementation_mode": "generate"},
    ]))
    transition_step(graph, "x", "running")
    result = normalize_step_result("Traceback (most recent call last): NameError: boom", "x", 1, 1)
    assert result["expert"]["status"] == "error"
    assert not accept_step(graph, "x", result)["ok"]
    delegate = process_from_blueprint("wz_loop", blueprint([
        {"id": "meta", "title": "meta", "implementation_mode": "delegate"},
    ]))
    transition_step(delegate, "meta", "running")
    delegate["budgets"]["max_depth"] = 1
    try:
        expand_subgraph(delegate, "meta", [{"id": "again", "implementation_mode": "delegate"}], "loop", 2)
        raise AssertionError("infinite delegation was not stopped")
    except ValueError as exc:
        assert "depth" in str(exc)


def test_10_legacy_session_compatibility(root):
    legacy = {"process_name": "Legacy", "goal": "legacy task",
              "stages": [{"id": "old", "title": "Old reusable stage", "asset_names": ["old_expert"]}]}
    graph = process_from_blueprint("wz_legacy", legacy)
    step = step_map(graph)["old"]
    assert graph["schema"] == "upc/1.0"
    assert step["implementation"]["mode"] == "reuse"
    assert step["implementation"]["capability_ref"] == "old_expert"
    succeed(graph, "old", root)


def main():
    tests = [value for key, value in sorted(globals().items()) if key.startswith("test_") and callable(value)]
    assert len(tests) == 10, len(tests)
    with tempfile.TemporaryDirectory(prefix="upc_matrix_") as td:
        root = Path(td)
        for test in tests:
            test(root / test.__name__)
            print("ok", test.__name__)
    print("Universal Process acceptance matrix passed: 10/10")


if __name__ == "__main__":
    main()
