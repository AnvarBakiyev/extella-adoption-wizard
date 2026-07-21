#!/usr/bin/env python3
"""Регрессия: эксперт плана реально входит в prompt/LLM-ветку и сохраняет blueprint."""
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "experts" / "wz_generate_blueprint.py").read_text(encoding="utf-8")


class FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        bp = {
            "process_name": "Тестовый процесс", "goal": "Проверить runtime",
            "stages": [{"id": "read", "title": "Чтение",
                        "capability_ids": ["documents"], "asset_names": ["doc_read"]}],
            "gaps": [], "open_questions": [],
            "suitability": {"score": 80, "risk_level": "low",
                            "self_serve_allowed": True, "rationale": "test"},
            "data_source": {"needs_external": False, "description": "", "suggested": [],
                            "obtained_by": "file"},
            "crux_layer": {"layer": "orchestration", "why": "test"},
        }
        return {"choices": [{"message": {"content": json.dumps(bp, ensure_ascii=False)}}]}


class FakeRequests:
    @staticmethod
    def post(*args, **kwargs):
        return FakeResponse()


def load_expert():
    source = "\n".join(line for line in SRC.splitlines()
                       if not line.strip().startswith("$extens"))
    ns = {"include": lambda *args, **kwargs: None, "requests": FakeRequests}
    exec(compile(source, "wz_generate_blueprint.py", "exec"), ns)
    return ns["wz_generate_blueprint"]


def main():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        sid = "wz_runtime"
        session = root / (sid + ".json")
        output = root / (sid + "_blueprint.json")
        catalog = root / "catalog.json"
        session.write_text(json.dumps({"session_id": sid, "client_name": "QA", "stage": "interview",
                                       "answers": {"pain": {"question": "Боль?", "answer": "Ручная работа"}},
                                       "comments": []}), encoding="utf-8")
        catalog.write_text(json.dumps({
            "catalog_version": "test", "capabilities": [{"id": "documents", "assets": ["doc_read"]}],
            "packs": [], "process_archetypes": [], "knowledge_packs": [], "delivery_extensions": [],
            "suitability_rubric": {"self_serve_allowed": []},
        }), encoding="utf-8")
        old_requests = sys.modules.get("requests")
        sys.modules["requests"] = FakeRequests
        try:
            result = load_expert()(session_path=str(session), catalog_path=str(catalog),
                                   output_path=str(output), api_key="fake", model="qwen-test")
        finally:
            if old_requests is None:
                sys.modules.pop("requests", None)
            else:
                sys.modules["requests"] = old_requests
        assert result.get("status") == "success", result
        saved = json.loads(output.read_text(encoding="utf-8"))
        assert saved["blueprint"]["crux_layer"]["layer"] == "orchestration"
        assert json.loads(session.read_text(encoding="utf-8"))["blueprint_path"] == str(output)
    print("wz_generate_blueprint: prompt строится, LLM-ветка исполняется, файл сохраняется ✓")


if __name__ == "__main__":
    main()
