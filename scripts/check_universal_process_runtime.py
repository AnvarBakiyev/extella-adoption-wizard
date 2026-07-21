#!/usr/bin/env python3
"""Deterministic contract tests for UPC v1; no platform/network required."""
import json
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ui"))

from wz_process import (  # noqa: E402
    STEP_RESULT_SCHEMA, accept_step, answer_human, block_for_human, checkpoint,
    budget_preflight, grant_step_budget, is_budget_gate, memory_entry, normalize_step_result,
    permission_preflight, process_from_blueprint,
    process_status, ready_steps, record_approval, recover_after_restart, repair_step,
    record_usage, step_map, transition_step, validate_process,
)


def graph_three():
    blueprint = {
        "process_name": "Тестовый процесс", "goal": "Получить проверенный результат",
        "stages": [
            {"id": "s1", "title": "Вход", "business_description": "Прочитать данные",
             "implementation_mode": "generate"},
            {"id": "s2", "title": "Ветка А", "business_description": "Посчитать А"},
            {"id": "s3", "title": "Ветка Б", "business_description": "Посчитать Б"},
            {"id": "s4", "title": "Merge", "business_description": "Объединить"},
        ]}
    plan = {"tasks": [
        {"id": "t1", "stage_id": "s1", "title": "Вход", "action": "build", "depends_on": []},
        {"id": "t2", "stage_id": "s2", "title": "Ветка А", "action": "build", "depends_on": ["t1"]},
        {"id": "t3", "stage_id": "s3", "title": "Ветка Б", "action": "build", "depends_on": ["t1"]},
        {"id": "t4", "stage_id": "s4", "title": "Merge", "action": "build", "depends_on": ["t2", "t3"]},
    ]}
    return process_from_blueprint("wz_test", blueprint, plan)


def success_result(step, output=None, artifacts=None):
    return {
        "schema": STEP_RESULT_SCHEMA, "step_id": step["id"], "step_version": step["version"],
        "attempt": 1, "transport": {"status": "completed", "task_id": "task_1"},
        "expert": {"status": "success", "expert_ref": "test", "message": ""},
        "output": output or {"ok": True}, "artifacts": artifacts or [], "evidence": [],
        "metrics": {}, "error": None, "started_at": "x", "finished_at": "y", "raw_sha256": "abc",
    }


def test_unknown_task_is_generate():
    graph = process_from_blueprint("wz_unknown", {"process_name": "Unknown", "goal": "Убраться в Downloads"})
    assert validate_process(graph)["ok"]
    assert graph["steps"][0]["implementation"]["mode"] == "generate"
    assert graph["steps"][0]["status"] == "ready"


def test_resource_budgets_survive_and_fail_closed():
    graph = process_from_blueprint("wz_budget", {"process_name": "Budget", "goal": "Bound work"})
    assert budget_preflight(graph, {"attempts": 4, "llm_calls": 8, "tokens": 48000,
                                    "generated_experts": 1})["ok"]
    state = record_usage(graph, attempts=4, llm_calls=8, tokens=48000, generated_experts=1)
    assert state["ok"] and graph["run"]["usage_estimated"] is True
    assert graph["run"]["estimated_cost_usd"] > 0
    graph["budgets"]["max_total_attempts"] = 4
    stopped = budget_preflight(graph, {"attempts": 1})
    assert not stopped["ok"] and stopped["code"] == "budget_exhausted"
    assert stopped["exceeded"][0]["resource"] == "attempts"

    step = graph["steps"][0]
    block_for_human(graph, step["id"], "Лимит процесса исчерпан", {
        "kind": "runtime_budget", "reserve": {"attempts": 4, "llm_calls": 9,
        "tokens": 96000, "cost_usd": 1.0, "generated_experts": 1}})
    assert is_budget_gate(step)
    grant = grant_step_budget(graph, step["id"])
    answer_human(graph, step["id"], "Подтверждаю ещё один ограниченный цикл")
    assert grant["reserve"]["attempts"] == 4
    assert budget_preflight(graph, grant["reserve"])["ok"]


def test_parallel_merge_and_local_repair():
    graph = graph_three()
    assert [x["id"] for x in ready_steps(graph)] == ["t1"]
    s1 = step_map(graph)["t1"]
    transition_step(graph, "t1", "running")
    assert accept_step(graph, "t1", success_result(s1))["ok"]
    assert {x["id"] for x in ready_steps(graph)} == {"t2", "t3"}
    for sid in ("t2", "t3"):
        step = step_map(graph)[sid]
        transition_step(graph, sid, "running")
        assert accept_step(graph, sid, success_result(step))["ok"]
    assert [x["id"] for x in ready_steps(graph)] == ["t4"]
    t4 = step_map(graph)["t4"]
    transition_step(graph, "t4", "running")
    assert accept_step(graph, "t4", success_result(t4))["ok"]
    assert process_status(graph) == "succeeded"
    # Repair only t2; independent t3 stays accepted and merge becomes stale.
    transition_step(graph, "t2", "stale")
    repaired = repair_step(graph, "t2", "new contract")
    assert repaired["version"] == 2 and repaired["invalidated"] == ["t4"]
    assert step_map(graph)["t3"]["status"] == "succeeded"


def test_error_markers_never_success():
    for raw in (
        "[Execution Error] name 'layer' is not defined",
        {"status": "success", "message": "Traceback (most recent call last): NameError: x"},
        {"result": {"status": "success", "message": "EOFError"}},
    ):
        result = normalize_step_result(raw, "s1")
        assert result["expert"]["status"] == "error" and result["error"]


def test_missing_artifact_and_memory_promotion():
    graph = process_from_blueprint("wz_file", {"process_name": "File", "goal": "Report",
        "stages": [{"id": "s1", "title": "Report", "implementation_mode": "generate"}]})
    step = graph["steps"][0]
    step["acceptance"]["required_artifacts"] = ["report.md"]
    transition_step(graph, step["id"], "running")
    candidate = memory_entry("concept", "Report schema is X", evidence_refs=["abc"], step_id=step["id"])
    rejected = accept_step(graph, step["id"], success_result(step), memory=[candidate])
    assert not rejected["ok"] and step["status"] == "repairing"
    assert all(x["status"] != "verified" for x in graph["memory"])
    with tempfile.TemporaryDirectory() as td:
        report = Path(td) / "report.md"
        report.write_text("ok", encoding="utf-8")
        repair_step(graph, step["id"], "add artifact")
        step = step_map(graph)[step["id"]]
        transition_step(graph, step["id"], "running")
        candidate = memory_entry("concept", "Report schema is X", evidence_refs=["def"],
                                 step_id=step["id"], step_version=step["version"])
        accepted = accept_step(graph, step["id"], success_result(
            step, artifacts=[{"path": str(report), "kind": "report.md"}]), memory=[candidate])
        assert accepted["ok"]
        assert any(x["status"] == "verified" and x["kind"] == "concept" for x in graph["memory"])


def test_permission_and_human_resume():
    graph = process_from_blueprint("wz_send", {"process_name": "Send", "goal": "Send to Telegram",
        "stages": [{"id": "s1", "title": "Отправить в Telegram", "business_description": "send telegram"}]})
    step = graph["steps"][0]
    request = {"permission": "send", "target": "telegram:chat", "payload": {"text": "preview"}}
    denied = permission_preflight(graph, step, request)
    assert denied["code"] == "approval_required"
    block_for_human(graph, step["id"], "Подтвердите отправку", request)
    assert process_status(graph) == "blocked_human"
    answer_human(graph, step["id"], "Да", approved=True)
    record_approval(graph, step["id"], "send", "telegram:chat", {"text": "preview"}, True)
    assert permission_preflight(graph, step, request)["ok"]


def test_restart_safety_and_checkpoint():
    graph = process_from_blueprint("wz_restart", {"process_name": "Restart", "goal": "Do",
        "stages": [{"id": "s1", "title": "Read", "business_description": "read"},
                   {"id": "s2", "title": "Delete", "business_description": "delete files",
                    "depends_on": ["s1"]}]})
    transition_step(graph, "s1", "running")
    recover_after_restart(graph)
    assert step_map(graph)["s1"]["status"] == "ready"
    # Dangerous running step requires reconciliation.
    step_map(graph)["s2"]["status"] = "running"
    recover_after_restart(graph)
    assert step_map(graph)["s2"]["status"] == "blocked_human"
    with tempfile.TemporaryDirectory() as td:
        path, events = Path(td) / "p.json", Path(td) / "events.jsonl"
        checkpoint(graph, path, events, {"type": "restart_recovered"})
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["schema"] == "upc/1.0" and events.read_text(encoding="utf-8").strip()


def test_cycle_and_step_budget():
    graph = graph_three()
    step_map(graph)["t1"]["dependencies"] = ["t4"]
    assert not validate_process(graph)["ok"]
    graph = graph_three()
    graph["budgets"]["max_steps"] = 2
    assert not validate_process(graph)["ok"]


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print("ok", test.__name__)
    print("UPC runtime checks passed:", len(tests))


if __name__ == "__main__":
    main()
