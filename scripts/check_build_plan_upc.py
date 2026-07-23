#!/usr/bin/env python3
"""Planner regression: an unknown/reused-looking task survives as generate with full contracts."""
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "experts" / "wz_build_plan.py").read_text(encoding="utf-8")


class Response:
    status_code = 200
    text = ""

    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


class Requests:
    @staticmethod
    def post(url, **kwargs):
        if url.endswith("/api/blocks/search"):
            return Response({"matches": []})
        plan = {"process_name": "Downloads", "namespace": "qa",
                "tasks": [{"id": "scan", "stage_id": "cleanup", "expert_name": "bad name",
                           "action": "reuse", "implementation_mode": "reuse",
                           "reuse_of": "unknown_downloads_cleaner", "purpose": "scan and preview",
                           "cspl": "bad", "depends_on": [],
                           "permissions": {"read": ["Downloads"], "move": ["quarantine"]},
                           "acceptance": {"deterministic_checks": ["manifest exists"],
                                          "semantic_criteria": [
                                              "A-101 присутствует как во входе; D-404 тоже обязана присутствовать"],
                                          "required_artifacts": ["manifest.json"]},
                           "retry_policy": {"max_attempts": 99}},
                          {"id": "run_all", "expert_name": "qa_run_pipeline", "action": "build",
                           "implementation_mode": "generate", "purpose": "Оркестратор процесса",
                           "cspl": "nohup", "depends_on": ["scan"]}],
                "orchestrator": {"expert_name": "qa_run_pipeline", "task_order": ["scan", "run_all"]},
                "human_gates": [], "risks": []}
        return Response({"choices": [{"message": {"content": json.dumps(plan)}}]})


def load():
    source = "\n".join(line for line in SOURCE.splitlines()
                       if not line.strip().startswith("$extens"))
    ns = {"include": lambda *args, **kwargs: None, "requests": Requests}
    exec(compile(source, "wz_build_plan.py", "exec"), ns)
    return ns["wz_build_plan"]


def main():
    with tempfile.TemporaryDirectory(prefix="upc_plan_") as td:
        root = Path(td)
        session = root / "wz_plan.json"
        bp = root / "wz_plan_blueprint.json"
        out = root / "wz_plan_build_plan.json"
        session.write_text(json.dumps({
            "session_id": "wz_plan", "blueprint_path": str(bp),
            "answers": {"result": {"answer": "A-101 присутствует во входе"}},
        }), encoding="utf-8")
        bp.write_text(json.dumps({"blueprint": {"process_name": "Downloads", "goal": "cleanup",
                                                "stages": [{"id": "cleanup", "title": "Cleanup"}]}}),
                      encoding="utf-8")
        old = sys.modules.get("requests")
        sys.modules["requests"] = Requests
        try:
            result = load()(session_path=str(session), blueprint_path=str(bp), output_path=str(out),
                            namespace="qa", api_token="token", api_key="key",
                            base_url="https://llm.invalid/v1", model="qwen")
        finally:
            if old is None:
                sys.modules.pop("requests", None)
            else:
                sys.modules["requests"] = old
        assert result["status"] == "success", result
        saved = json.loads(out.read_text(encoding="utf-8"))
        task = saved["plan"]["tasks"][0]
        assert len(saved["plan"]["tasks"]) == 1, saved["plan"]["tasks"]
        assert saved["plan"]["orchestrator"]["task_order"] == ["scan"]
        assert any("runtime orchestrator removed" in warning for warning in saved["warnings"])
        assert task["implementation_mode"] == "generate" and task["action"] == "build", task
        assert task["reuse_of"] is None and task["capability_ref"] is None
        assert task["expert_name"].startswith("qa_") and task["cspl"] == "fython"
        assert task["permissions"]["move"] == ["quarantine"]
        assert task["acceptance"]["required_artifacts"] == ["manifest.json"]
        assert "D-404" not in json.dumps(task["acceptance"], ensure_ascii=False)
        assert any("synthetic record claims removed" in warning for warning in saved["warnings"])
        assert task["acceptance"]["semantic_criteria"] == [
            "Результат соответствует фактическим данным текущих входов и заявленной бизнес-логике шага"]
        assert task["retry_policy"]["max_attempts"] == 10
    print("wz_build_plan: catalog miss survives as bounded generate step ✓")


if __name__ == "__main__":
    main()
