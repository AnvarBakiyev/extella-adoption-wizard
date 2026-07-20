#!/usr/bin/env python3
"""Регрессия упаковки рабочего агента: experts + concepts + rules, без дублей redeploy."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "ui" / "server.py"


def main():
    source = SERVER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {"_proc_rule_tag", "_proc_concepts_push"}
    body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    assert {node.name for node in body} == wanted
    calls = []

    def fake_api(endpoint, payload, agent_id):
        calls.append((endpoint, payload, agent_id))
        if endpoint == "/api/concept/list":
            return {"status": "success", "results": [
                {"concept_id": "keep", "concept_text": "[процесс:wz_x] Назначение: сверка"},
                {"concept_id": "old", "concept_text": "[процесс:wz_x] Старый контекст"},
                {"concept_id": "foreign", "concept_text": "чужое знание"},
            ]}
        return {"status": "success", "id": "ok"}

    ns = {"_api_agent": fake_api, "PROC_RULE_TAG": "[процесс:%s]"}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(SERVER), "exec"), ns)
    result = ns["_proc_concepts_push"]("wz_x", ["Назначение: сверка", "Этапы: Excel → PDF"], "agent_x")
    assert result["ok"] and result["total"] == 2
    assert any(ep == "/api/concept/delete" and p.get("concept_id") == "old" for ep, p, _ in calls)
    assert not any(ep == "/api/concept/delete" and p.get("concept_id") == "foreign" for ep, p, _ in calls)
    assert any(ep == "/api/concept/add" and "Этапы: Excel" in p.get("text", "") for ep, p, _ in calls)
    assert "_proc_concepts_push(sid, _concepts, agent_id)" in source
    assert "_proc_rules_push(sid, s.get(\"rules\") or [], agent_id)" in source
    assert '"package": {"experts":' in source
    assert '"output_dir": "/tmp/extella_" + sid + "_agent"' in source
    assert 'params=" + _agent_params_text' in source

    # API может создать внешне валидную, но неисполняемую Pro-копию без BYOK. Она не должна
    # попадать в реестр: обязательный smoke -> delete -> честное объяснение.
    create_fn = next(node for node in tree.body
                     if isinstance(node, ast.FunctionDef) and node.name == "_agent_create_copy")
    agent_calls, registered = [], []

    def agent_api(endpoint, payload, timeout=0):
        agent_calls.append((endpoint, payload))
        if endpoint == "/api/agent/get":
            return {"provider": "alibaba", "model": "qwen-test", "instructions": "", "tools": []}
        if endpoint == "/api/agent/create":
            return {"id": "agent_qa", "name": "QA"}
        if endpoint == "/api/agent/run":
            return {"status": "error", "message": "pro_key_required: provider API key"}
        return {"status": "success"}

    create_ns = {"api": agent_api, "CONFIG": {}, "BASE_QWEN_AGENT": "agent_base",
                 "_scrub": str, "_agent_register": lambda *a: registered.append(a)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[create_fn], type_ignores=[])),
                 str(SERVER), "exec"), create_ns)
    refused = create_ns["_agent_create_copy"]("QA")
    assert refused["ok"] is False and "BYOK" in refused["err"]
    assert any(ep == "/api/agent/delete" for ep, _ in agent_calls)
    assert registered == []
    print("упаковка агента: эксперт + синхронизированные concepts/rules ✓")


if __name__ == "__main__":
    main()
