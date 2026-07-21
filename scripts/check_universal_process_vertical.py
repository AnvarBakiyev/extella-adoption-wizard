#!/usr/bin/env python3
"""Deterministic vertical proof: unknown task -> per-step experts -> local repair -> package.

This exercises the real ``wz_build._run_build`` scheduler with only the platform/LLM boundary
replaced by in-memory fakes. It intentionally fails the middle step on the first build and proves
that the accepted predecessor is not executed again after a versioned repair and checkpoint resume.
"""
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
    platform.CONFIG = {"auth_token": "test", "llm_api_key": "", "llm_model": "qwen",
                       "llm_base_url": "https://example.invalid"}
    platform.BASE = "https://example.invalid"

    def api(path, body, **_kwargs):
        saved.append((path, body))
        if path == "/api/expert/get":
            return {"status": "success", "expert_code": "def accepted_expert():\n    return {}\n"}
        return {"status": "success", "id": "expert_test"}

    platform.api = api
    platform.run_expert = lambda *_args, **_kwargs: {}
    platform.qwen_agent = lambda: "agent_qwen"

    llm = types.ModuleType("wz_llm")
    llm.run_llm_expert = lambda *_args, **_kwargs: {"status": "success"}
    llm.design_agent = lambda: "agent_design"
    llm.gen_panel_manifest = lambda *_args, **_kwargs: {}

    agentic = types.ModuleType("wz_agentic")
    agentic.build_agentic_solution = lambda *_args, **_kwargs: {}
    agentic.prepare_task_context = lambda *_args, **_kwargs: {}
    agentic._builder_brief = lambda value: value
    sys.modules.update({"wz_platform": platform, "wz_llm": llm, "wz_agentic": agentic})
    spec = importlib.util.spec_from_file_location("wz_build_vertical_test", UI / "wz_build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    saved = []
    mod = load_build(saved)
    from wz_process import checkpoint, repair_step, step_map

    with tempfile.TemporaryDirectory(prefix="upc_vertical_") as td:
        root = Path(td)
        sessions = root / "sessions"
        runs = root / "runs"
        sessions.mkdir(); runs.mkdir()
        mod.SESS_DIR = sessions
        mod.RUNS_DIR = runs
        sid = "wz_upc_vertical"
        session_path = sessions / (sid + ".json")
        session_path.write_text(json.dumps({
            "session_id": sid,
            "client_name": "Новая произвольная задача",
            "questionnaire_task": "Собрать ранее неизвестный трёхшаговый процесс",
            "answers": {"goal": {"answer": "получить проверенный итог"}},
            "agent_id": "agent_qwen",
            "stage": "blueprint",
        }, ensure_ascii=False), encoding="utf-8")
        blueprint = {
            "process_name": "Unknown workflow",
            "goal": "Выполнить ранее неизвестную задачу без готовой capability",
            "stages": [
                {"id": "collect", "title": "Собрать вход", "business_description": "Собрать вход"},
                {"id": "transform", "title": "Преобразовать", "business_description": "Преобразовать"},
                {"id": "report", "title": "Сформировать итог", "business_description": "Сформировать итог"},
            ],
        }
        (sessions / (sid + "_blueprint.json")).write_text(
            json.dumps({"blueprint": blueprint}, ensure_ascii=False), encoding="utf-8")
        tasks = [
            {"id": "collect", "stage_id": "collect", "title": "Собрать вход",
             "implementation_mode": "generate", "depends_on": [],
             "acceptance": {"required_artifacts": ["step_result_json"]}},
            {"id": "transform", "stage_id": "transform", "title": "Преобразовать",
             "implementation_mode": "generate", "depends_on": ["collect"],
             "acceptance": {"required_artifacts": ["step_result_json"]}},
            {"id": "report", "stage_id": "report", "title": "Сформировать итог",
             "implementation_mode": "llm_worker", "depends_on": ["transform"],
             "acceptance": {"required_artifacts": ["step_result_json"]}},
        ]

        def plan_expert(_name, _params, **_kwargs):
            (sessions / (sid + "_build_plan.json")).write_text(
                json.dumps({"plan": {"tasks": tasks}}, ensure_ascii=False), encoding="utf-8")
            return {"status": "success"}

        mod.run_llm_expert = plan_expert
        prepared = {
            "ok": True,
            "package": {"task_contract": {"sha256": "task-contract-test"},
                        "working_memory": {}, "inputs": []},
            "source_model": {"status": "ready", "strategy": "compose",
                             "sources": [], "operations": [], "acceptance_criteria": []},
        }
        mod.prepare_task_context = lambda *_args, **_kwargs: prepared
        calls = []
        fail_transform = {"value": True}

        def build_solution(*_args, **kwargs):
            step = kwargs["step_contract"]
            step_id = str(step["id"])
            calls.append((step_id, int(step["version"])))
            if step_id == "transform" and fail_transform["value"]:
                fail_transform["value"] = False
                return {"ok": False, "code": "agentic_acceptance_failed",
                        "detail": "намеренный дефект нормализации", "attempts": [{"attempt": 1}],
                        "draft_created": True, "expert_ran": True,
                        "working_memory": {}, "budgets": {}}
            artifact_dir = runs / kwargs["build_id"] / "fake_step_artifacts"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            artifact = artifact_dir / (step_id + "_v" + str(step["version"]) + ".json")
            artifact.write_text(json.dumps({"step_id": step_id, "version": step["version"]}),
                                encoding="utf-8")
            result = {
                "status": "success", "output": {"step_id": step_id, "accepted": True},
                "evidence": {"acceptance_checks": [
                    {"criterion": "postcondition", "passed": True, "evidence": "fixture"}]},
                "artifacts": [{"kind": "step_result_json", "path": str(artifact)}],
            }
            return {"ok": True, "expert": kwargs["expert_name_override"], "result": result,
                    "attempts": [{"attempt": 1}], "judge": {"verdict": "pass", "confidence": 1.0},
                    "verified_memory": [{"kind": "concept", "text": "Принят шаг " + step_id,
                                         "scope": "process", "confidence": 0.95}],
                    "package_sha256": "pkg-" + step_id}

        mod.build_agentic_solution = build_solution

        mod._run_build(sid, "build_first")
        process_path = sessions / (sid + "_process.json")
        first = json.loads(process_path.read_text(encoding="utf-8"))
        first_steps = step_map(first)
        assert first_steps["collect"]["status"] == "succeeded", first_steps
        assert first_steps["transform"]["status"] == "failed", first_steps
        assert first_steps["report"]["status"] == "pending", first_steps
        assert calls == [("collect", 1), ("transform", 1)], calls
        first_progress = json.loads((runs / "build_first" / "build_progress.json").read_text(encoding="utf-8"))
        assert first_progress["status"] == "error" and first_progress["build_mode"] == "universal_process"

        repair = repair_step(first, "transform", "Исправить только нормализацию")
        assert repair["version"] == 2 and repair["invalidated"] == [], repair
        checkpoint(first, process_path, sessions / (sid + "_process_events.jsonl"), {
            "type": "step_repair_requested", "step_id": "transform", "version": 2})
        calls.clear()

        mod._run_build(sid, "build_resume")
        final = json.loads(process_path.read_text(encoding="utf-8"))
        final_steps = step_map(final)
        assert all(final_steps[s]["status"] == "succeeded" for s in ("collect", "transform", "report"))
        assert calls == [("transform", 2), ("report", 1)], calls
        assert final_steps["collect"]["version"] == 1
        assert final_steps["transform"]["version"] == 2
        assert any(x.get("kind") == "concept" and x.get("status") == "verified"
                   for x in final.get("memory") or []), final.get("memory")
        final_session = json.loads(session_path.read_text(encoding="utf-8"))
        build = final_session["builds"][-1]
        assert final_session["stage"] == "built"
        assert build["build_mode"] == "universal_process"
        assert build["process_contract_version"] == 1
        assert build["manifest"]["contract"] == "upc/1.0"
        assert build["orchestrator"].endswith("_run_process")
        assert any(path == "/api/expert/save" and body.get("name") == build["orchestrator"]
                   for path, body in saved)
        events = (sessions / (sid + "_process_events.jsonl")).read_text(encoding="utf-8")
        assert "step_failed" in events and "step_repair_requested" in events and "process_built" in events

    print("UPC vertical: unknown task -> generated steps -> local repair -> checkpoint resume -> package ✓")


if __name__ == "__main__":
    main()
