#!/usr/bin/env python3
"""Deterministic contract tests for UPC v1; no platform/network required."""
import json
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ui"))

from wz_process import (  # noqa: E402
    STEP_RESULT_SCHEMA, accept_step, adaptive_failure_decision, answer_human,
    assess_reuse_compatibility, block_for_human, checkpoint,
    budget_preflight, canonical_artifact_requirements, grant_step_budget, is_budget_gate, make_step,
    memory_entry, normalize_step_result,
    expand_failure_subgraph, input_fingerprint,
    permission_preflight, process_from_blueprint,
    project_runtime_contract,
    process_status, ready_steps, reconcile_checkpoints, record_approval, recover_after_restart, repair_step,
    record_usage, step_contract_fingerprint, step_map, transition_step, upgrade_process_graph,
    validate_process,
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


def test_planner_prose_is_canonical_result_not_imaginary_file():
    label = "структурированные данные, готовые к проверке"
    task = {
        "id": "extract", "title": "Извлечь данные", "action": "build",
        "output_contract": {"artifacts": [label], "data_schema": {"type": "object"}},
        "acceptance": {"required_artifacts": [label], "semantic_criteria": []},
    }
    step = make_step(task, task, 1)
    assert step["acceptance"]["required_artifacts"] == ["step_result_json"]
    assert canonical_artifact_requirements(["report.xlsx"]) == ["step_result_json", "report_xlsx"]

    # An unfinished graph downloaded before this fix is upgraded in place, so accepted neighbours
    # are not rebuilt and a targeted repair can continue with the canonical contract.
    graph = process_from_blueprint("legacy_artifact", {
        "process_name": "Legacy", "stages": [{"id": "extract", "title": "Извлечь данные"}]})
    legacy = graph["steps"][0]
    legacy["status"] = "repairing"
    legacy["acceptance"]["required_artifacts"] = [label]
    legacy["output_contract"]["artifacts"] = [label]
    migrated = upgrade_process_graph(graph)
    assert legacy["acceptance"]["required_artifacts"] == ["step_result_json"]
    assert legacy["acceptance"]["artifact_labels"] == [label]
    assert any(x.endswith(":artifact_contract") for x in migrated["changed"])


def test_cabinet_projects_local_folder_without_fake_file_or_delivery():
    blueprint = {"process_name": "Уборка папки Downloads", "goal": "Убраться в папке ~/Downloads",
                 "stages": [
        {"id": "scan", "title": "Сканирование папки Downloads",
         "business_description": "Прочитать локальную папку ~/Downloads",
         "inputs": ["локальная папка"], "outputs": ["список файлов"]},
        {"id": "move", "title": "Разложить файлы", "depends_on": ["scan"],
         "business_description": "Создать подпапки и переместить файлы",
         "outputs": ["журнал перемещений"]},
    ]}
    graph = process_from_blueprint("wz_downloads", blueprint)
    session = {"goal": blueprint["goal"], "answers": {"task": {"answer": "Сортировать Downloads"}}}
    build = {"source_file": "/tmp/build_fixture/task_input.json",
             "source_files": ["/tmp/build_fixture/task_input.json"]}
    contract = project_runtime_contract(graph, session, build, blueprint)
    assert contract["input"]["kind"] == "local_folder"
    assert contract["input"]["path"] == "~/Downloads"
    assert not contract["input"]["manual_upload"]
    assert not contract["delivery"]["enabled"]


def test_cabinet_keeps_real_file_and_real_delivery():
    blueprint = {"process_name": "Отчёт", "goal": "Прочитать Excel и отправить отчёт",
                 "stages": [{"id": "read", "title": "Прочитать Excel", "inputs": ["xlsx"]},
                            {"id": "send", "title": "Отправить в Telegram", "depends_on": ["read"],
                             "permissions": {"send": ["telegram:owner"]},
                             "outputs": ["total_count", "total_sum"]}]}
    graph = process_from_blueprint("wz_report", blueprint)
    contract = project_runtime_contract(graph, {}, {"source_file": "/tmp/report.xlsx"}, blueprint)
    assert contract["input"]["kind"] == "manual_file"
    assert contract["delivery"]["enabled"]
    assert contract["output"]["supports_count"] and contract["output"]["supports_sum"]


def test_cabinet_ui_is_gated_by_runtime_contract():
    html = (ROOT / "ui" / "wizard.html").read_text(encoding="utf-8")
    assert "if(inp.kind!=='manual_file') return '';" in html
    assert "var deliverySection = _wzDeliveryEnabled(a)" in html
    assert ".replace(/\\{count\\}/g,'128')" not in html
    assert ".replace(/\\{sum\\}/g,'26 000 000')" not in html


def test_cabinet_does_not_treat_generated_document_as_input_file():
    blueprint = {"process_name": "Создать памятку", "goal": "Создать PDF-памятку для сотрудников",
                 "stages": [{"id": "write", "title": "Сформировать памятку",
                             "inputs": [], "outputs": ["памятка.pdf"]}]}
    graph = process_from_blueprint("wz_generate_pdf", blueprint)
    contract = project_runtime_contract(graph, {},
                                        {"source_file": "/tmp/build_fixture/task_input.json"}, blueprint)
    assert contract["input"]["kind"] == "none"


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


def test_adaptive_failure_controller_changes_action():
    first = adaptive_failure_decision({"message": "The read operation timed out", "input_count": 1,
                                       "run_timeout_seconds": 600, "duration_seconds": 2})
    assert first["failure_class"] == "transient_transport" and first["action"] == "retry_transient"
    overloaded = adaptive_failure_decision({"message": "task timed out", "input_count": 13,
                                            "input_formats": ["pdf", "xlsx"], "can_split": True})
    assert overloaded == {**overloaded, "failure_class": "workload_timeout", "action": "split_step"}
    missing = adaptive_failure_decision({"issues": ["missing required artifact: report.xlsx"]})
    assert missing["action"] == "repair_output_contract"
    repeated = adaptive_failure_decision({"code_seen": True, "input_count": 13, "can_split": True})
    assert repeated["failure_class"] == "no_progress" and repeated["action"] == "split_step"
    technical = adaptive_failure_decision({"message": "NameError: x", "owner_question": "Как исправить код?"})
    assert technical["action"] == "repair_code" and not technical["owner_question"]
    owner_question = ("Выберите бизнес-правило: A — учитывать дату создания или B — дату оплаты. "
                      "Доказательство: обе колонки присутствуют. Предполагаю B, потому что цель — денежный поток. "
                      "Ответ изменит фильтр периода и итоговый отчёт.")
    semantic = adaptive_failure_decision({"semantic_ambiguity": True,
                                          "owner_question": owner_question})
    assert semantic["action"] == "ask_owner" and semantic["owner_question"] == owner_question


def test_failure_driven_decomposition_is_bounded_and_resumable():
    graph = graph_three()
    transition_step(graph, "t1", "running")
    decision = {"failure_class": "workload_timeout", "action": "split_step",
                "evidence": ["13 mixed inputs timed out"], "owner_question": ""}
    sources = [{"path": "/tmp/a.pdf", "name": "a.pdf", "format": "pdf"},
               {"path": "/tmp/b.xlsx", "name": "b.xlsx", "format": "xlsx"}]
    expanded = expand_failure_subgraph(graph, "t1", sources=sources, failure=decision)
    assert step_map(graph)["t1"]["status"] == "succeeded"
    assert len(expanded["added"]) == 3
    ready = ready_steps(graph)
    assert len(ready) == 2 and all("source_refs" in (x.get("input_contract") or {}) for x in ready)
    downstream = step_map(graph)["t2"]["dependencies"]
    assert "t1" in downstream and expanded["terminal_step_ids"][0] in downstream
    try:
        expand_failure_subgraph(graph, "t1", sources=sources, failure=decision)
    except ValueError:
        pass
    else:
        raise AssertionError("same failure split must not loop")


def test_failure_children_extract_local_facts_and_merge_global_goal():
    graph = process_from_blueprint("scoped", {"process_name": "Mixed", "goal": "Reconcile sources",
        "stages": [{"id": "root", "title": "Reconcile all inputs"}]})
    root = step_map(graph)["root"]
    transition_step(graph, "root", "running")
    decision = {"failure_class": "workload_timeout", "action": "split_step",
                "evidence": ["mixed input workload"], "owner_question": ""}
    sources = [{"path": "/tmp/document.pdf", "name": "document.pdf", "format": "pdf"},
               {"path": "/tmp/table.xlsx", "name": "table.xlsx", "format": "xlsx"}]
    expansion = expand_failure_subgraph(graph, "root", sources=sources, failure=decision)
    children = [step_map(graph)[sid] for sid in expansion["added"]]
    maps = [x for x in children if (x.get("input_contract") or {}).get("scope") == "map_partition"]
    merge = [x for x in children if (x.get("input_contract") or {}).get("scope") == "map_merge"]
    assert len(maps) == 2 and len(merge) == 1
    assert all("source_facts.json" in (x.get("acceptance") or {}).get("required_artifacts", []) for x in maps)
    assert all(not (x.get("acceptance") or {}).get("semantic_criteria") for x in maps)
    assert all("глобальное сопоставление" in x["purpose"] for x in maps)
    assert "Reconcile all inputs" in merge[0]["purpose"]
    assert len((maps[0].get("input_contract") or {}).get("process_source_manifest") or []) == 2


def test_runtime_and_delivery_steps_never_partition_by_business_files():
    graph = process_from_blueprint("roles", {"process_name": "Any process", "stages": [
        {"id": "read", "title": "Read input"},
        {"id": "schedule", "title": "Регулярный запуск по расписанию"},
        {"id": "send", "title": "Доставка отчёта ответственным лицам"},
    ]})
    roles = {x["id"]: x.get("execution_role") for x in graph["steps"]}
    assert roles == {"read": "data", "schedule": "runtime_setup", "send": "delivery"}
    for sid in ("schedule", "send"):
        step = step_map(graph)[sid]
        transition_step(graph, sid, "running")
        try:
            expand_failure_subgraph(graph, sid, sources=[{"path": "/tmp/a.pdf", "format": "pdf"}],
                                    failure={"failure_class": "workload_timeout", "action": "split_step"})
        except ValueError as exc:
            assert "cannot be partitioned" in str(exc)
        else:
            raise AssertionError("runtime wrapper was split by source format")


def test_mixed_input_failure_recovers_through_children_and_checkpoint_resume():
    """Reproduce the failure shape, not Gulzhan's domain data: split, repair, resume, merge."""
    graph = process_from_blueprint("wz_mixed_recovery", {
        "process_name": "Mixed input recovery", "goal": "Produce one proved result",
        "stages": [{"id": "root", "title": "Process mixed inputs",
                    "business_description": "Process every declared source and integrate the results"}],
    })
    parent = graph["steps"][0]
    sources = []
    with tempfile.TemporaryDirectory(prefix="upc_mixed_recovery_") as td:
        root = Path(td)
        for index in range(13):
            suffix = ".pdf" if index < 5 else ".xlsx"
            source = root / ("input_%02d%s" % (index, suffix))
            source.write_bytes(("fixture-%d" % index).encode("utf-8"))
            sources.append({"path": str(source), "name": source.name,
                            "format": suffix.lstrip("."), "sha256": input_fingerprint([source])})

        transition_step(graph, parent["id"], "running")
        failure = adaptive_failure_decision({"message": "task timed out", "duration_seconds": 240,
                                             "run_timeout_seconds": 240, "input_count": 13,
                                             "input_formats": ["pdf", "xlsx"], "can_split": True})
        assert failure["failure_class"] == "workload_timeout" and failure["action"] == "split_step"
        expanded = expand_failure_subgraph(graph, parent["id"], sources=sources, failure=failure)
        assert len(expanded["added"]) == 5  # four bounded batches plus deterministic integration

        first_batch = ready_steps(graph)[0]
        transition_step(graph, first_batch["id"], "running")
        missing = accept_step(graph, first_batch["id"], success_result(first_batch),
                              semantic_verdict={"verdict": "pass", "confidence": 1.0})
        assert not missing["ok"]
        repair = adaptive_failure_decision({"issues": missing["validation"]["issues"],
                                            "input_count": len((first_batch.get("input_contract") or {})
                                                               .get("source_refs") or [])})
        assert repair["failure_class"] == "output_contract_missing"
        assert repair["action"] == "repair_output_contract"
        repair_step(graph, first_batch["id"], "repair only the missing result envelope")

        accepted_ids = []
        repaired_id = first_batch["id"]
        while process_status(graph) != "succeeded":
            runnable = ready_steps(graph)
            assert runnable, process_status(graph)
            for step in runnable:
                transition_step(graph, step["id"], "running")
                result = success_result(step)
                required = (step.get("acceptance") or {}).get("required_artifacts") or []
                if required:
                    result["artifacts"] = []
                    for index, kind in enumerate(required, 1):
                        artifact = root / (step["id"] + "_artifact_%02d.json" % index)
                        artifact.write_text(json.dumps({"step_id": step["id"], "kind": kind,
                                                        "ok": True}), encoding="utf-8")
                        result["artifacts"].append({"path": str(artifact), "kind": kind})
                result["provenance"] = {
                    "input_sha256": input_fingerprint([
                        Path(x) for x in ((step.get("input_contract") or {}).get("source_refs") or [])]),
                    "expert_ref": "expert_" + step["id"],
                    "expert_code_sha256": "code_" + step["id"],
                    "dependency_checkpoint_sha256": [
                        (step_map(graph)[dep].get("checkpoint") or {}).get("checkpoint_sha256", "")
                        for dep in step.get("dependencies") or []],
                }
                accepted = accept_step(graph, step["id"], result,
                                       semantic_verdict={"verdict": "pass", "confidence": 1.0})
                assert accepted["ok"], accepted
                accepted_ids.append(step["id"])

            # Persist after the repaired child, then prove restart does not replay it.
            if repaired_id in accepted_ids and not graph.get("_resume_proved"):
                checkpoint_path = root / "mixed_process.json"
                checkpoint(graph, checkpoint_path)
                graph = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                assert not recover_after_restart(graph)
                assert step_map(graph)[repaired_id]["status"] == "succeeded"
                assert repaired_id not in [x["id"] for x in ready_steps(graph)]
                graph["_resume_proved"] = True

        assert process_status(graph) == "succeeded"
        assert step_map(graph)[repaired_id]["version"] == 2
        assert all(step_map(graph)[sid].get("checkpoint") for sid in expanded["added"])
        assert len(accepted_ids) == len(set(accepted_ids))


def test_graph_sizes_1_8_20_40_keep_valid_state_transitions():
    for size in (1, 8, 20, 40):
        stages = []
        tasks = []
        for index in range(size):
            sid = "s%03d" % index
            tid = "t%03d" % index
            stages.append({"id": sid, "title": "Step %d" % (index + 1)})
            tasks.append({"id": tid, "stage_id": sid, "title": "Step %d" % (index + 1),
                          "action": "build", "depends_on": [] if index == 0 else ["t%03d" % (index - 1)]})
        graph = process_from_blueprint("wz_size_%d" % size,
                                       {"process_name": "Sized graph", "stages": stages},
                                       {"tasks": tasks})
        assert validate_process(graph)["ok"]
        for index in range(size):
            ready = ready_steps(graph)
            assert [x["id"] for x in ready] == ["t%03d" % index]
            step = ready[0]
            transition_step(graph, step["id"], "running")
            assert accept_step(graph, step["id"], success_result(step))["ok"]
        assert process_status(graph) == "succeeded"


def test_reuse_requires_semantic_compatibility():
    graph = process_from_blueprint("reuse", {"process_name": "Excel", "goal": "Classify Excel",
        "stages": [{"id": "s1", "title": "Классифицировать Excel", "inputs": ["xlsx"],
                    "outputs": ["categories"], "capability_ref": "cx_parse_dialogues"}]})
    step = graph["steps"][0]
    candidate = {"expert": "cx_parse_dialogues", "purpose": "Parse support chat dialogues and sentiment",
                 "params": {"input": "transcript", "output": "dialogue tags"}}
    verdict = assess_reuse_compatibility(step, candidate)
    assert not verdict["ok"] and verdict["failure_class"] == "capability_mismatch"


def test_inflight_graph_upgrade_preserves_proofs_and_unblocks_false_partition_question():
    graph = process_from_blueprint("upgrade", {"process_name": "Mixed", "stages": [
        {"id": "root", "title": "Сверить PDF и Excel"}]})
    root = step_map(graph)["root"]
    root.pop("execution_role", None)
    root["status"] = "succeeded"
    root["checkpoint"] = {
        "schema": "upc-checkpoint/1.0", "step_id": "root", "step_version": 1,
        "step_contract_sha256": "legacy", "checkpoint_sha256": "legacy",
    }
    child_spec = {
        "id": "root_d1_batch_001", "title": "Обработать ограниченную партию 1 (pdf)",
        "input_contract": {"required": True, "source_refs": ["/tmp/a.pdf"]},
    }
    child = make_step(child_spec, child_spec, 2)
    child.pop("execution_role", None)
    child["delegation"] = {"parent_step_id": "root", "depth": 1}
    child["dependencies"] = ["root"]
    child["status"] = "blocked_human"
    child["human_gate"] = {"question": "Предоставьте второй Excel-файл"}
    graph["steps"].append(child)
    graph["edges"].append({"from": "root", "to": child["id"], "condition": "succeeded"})

    upgraded = upgrade_process_graph(graph)

    assert upgraded["changed"] and upgraded["unblocked"] == [child["id"]]
    assert root["execution_role"] == "data"
    assert root["checkpoint"]["step_contract_sha256"] == step_contract_fingerprint(root)
    assert child["status"] == "ready" and child.get("human_gate") is None
    assert child["input_contract"]["scope"] == "map_partition"
    assert child["acceptance"]["semantic_criteria"] == []


def test_checkpoint_invalidates_changed_input_and_descendants_only():
    graph = graph_three()
    with tempfile.TemporaryDirectory() as td:
        source = Path(td) / "input.txt"
        source.write_text("v1", encoding="utf-8")
        root_sha = input_fingerprint([source])
        root = step_map(graph)["t1"]
        transition_step(graph, "t1", "running")
        result = success_result(root)
        result["provenance"] = {"input_sha256": root_sha, "expert_ref": "e1",
                                "expert_code_sha256": "code-v1"}
        assert accept_step(graph, "t1", result)["ok"]
        # An unrelated accepted root is intentionally outside t1 descendants.
        independent = process_from_blueprint("other", {"process_name": "Other", "goal": "Other"})["steps"][0]
        independent["id"] = "independent"
        independent["status"] = "running"
        graph["steps"].append(independent)
        assert accept_step(graph, "independent", success_result(independent))["ok"]
        source.write_text("v2", encoding="utf-8")
        invalidated = reconcile_checkpoints(graph, input_fingerprint([source]))
        assert "t1" in invalidated["invalidated"]
        assert step_map(graph)["independent"]["status"] == "succeeded"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print("ok", test.__name__)
    print("UPC runtime checks passed:", len(tests))


if __name__ == "__main__":
    main()
