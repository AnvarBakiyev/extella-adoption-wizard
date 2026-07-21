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
                                          "semantic_criteria": [], "required_artifacts": ["manifest.json"]},
                           "retry_policy": {"max_attempts": 99}}],
                "orchestrator": {"expert_name": "qa_run_pipeline", "task_order": ["scan"]},
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
        session.write_text(json.dumps({"session_id": "wz_plan", "blueprint_path": str(bp)}), encoding="utf-8")
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
        task = json.loads(out.read_text(encoding="utf-8"))["plan"]["tasks"][0]
        assert task["implementation_mode"] == "generate" and task["action"] == "build", task
        assert task["reuse_of"] is None and task["capability_ref"] is None
        assert task["expert_name"].startswith("qa_") and task["cspl"] == "fython"
        assert task["permissions"]["move"] == ["quarantine"]
        assert task["acceptance"]["required_artifacts"] == ["manifest.json"]
        assert task["retry_policy"]["max_attempts"] == 10
    print("wz_build_plan: catalog miss survives as bounded generate step ✓")


if __name__ == "__main__":
    main()
