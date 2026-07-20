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
    learned_node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and
                        node.name == "_proc_learned_rules_push")
    learned_calls = []

    def learned_api(endpoint, payload, agent_id):
        learned_calls.append((endpoint, payload, agent_id))
        if endpoint == "/api/rules/list":
            return {"status": "success", "results": [
                {"id": "owner", "rule": "[процесс:wz_x] Никогда не отправлять наружу"},
                {"id": "old", "rule": "[выучено:wz_x] Старое правило"},
            ]}
        return {"status": "success", "rule_id": "new"}

    learned_ns = {"_api_agent": learned_api, "LEARNED_RULE_TAG": "[выучено:%s]"}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[learned_node], type_ignores=[])),
                 str(SERVER), "exec"), learned_ns)
    learned = learned_ns["_proc_learned_rules_push"]("wz_x", ["Проверять все строки"], "agent_x")
    assert learned["ok"] and learned["total"] == 1
    assert any(ep == "/api/rules/delete" and p.get("rule_id") == "old" for ep, p, _ in learned_calls)
    assert not any(ep == "/api/rules/delete" and p.get("rule_id") == "owner" for ep, p, _ in learned_calls)
    assert any(ep == "/api/rules/add" and "[выучено:wz_x] Проверять" in p.get("rule", "")
               for ep, p, _ in learned_calls)
    assert "_proc_concepts_push(sid, _concepts, agent_id)" in source
    assert "_proc_rules_push(sid, s.get(\"rules\") or [], agent_id)" in source
    assert "_proc_learned_rules_push(sid, _learned_rules, agent_id)" in source
    assert 'x.get("status") == "verified"' in source
    assert 'x.get("kind") == "concept"' in source and 'x.get("kind") == "rule"' in source
    assert '"package": {"experts":' in source
    assert '"output_dir": "/tmp/extella_" + sid + "_agent"' in source
    assert 'params=" + _agent_params_text' in source

    # Любой Qwen-провайдер допустим. Pro-копия без BYOK не попадает в рабочий реестр, но её id
    # сохраняется для привязки пользовательского ключа/endpoint вместо безвозвратного удаления.
    create_nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and
                    node.name in {"_is_qwen_agent_record", "_agent_probe_ok", "_agent_create_copy"}]
    agent_calls, registered = [], []

    def agent_api(endpoint, payload, timeout=0):
        agent_calls.append((endpoint, payload))
        if endpoint == "/api/agent/get":
            return {"provider": "openrouter", "model": "qwen/qwen-test", "instructions": "", "tools": []}
        if endpoint == "/api/agent/create":
            return {"id": "agent_qa", "name": "QA"}
        if endpoint == "/api/agent/run":
            return {"status": "error", "message": "pro_key_required: provider API key"}
        return {"status": "success"}

    create_ns = {"api": agent_api, "CONFIG": {}, "BASE_QWEN_AGENT": "agent_base",
                 "CURAGENT_KV": "current", "qwen_agents": lambda: ["agent_user_qwen"],
                 "_kv_read": lambda *a: "", "_scrub": str,
                 "_agent_register": lambda *a: registered.append(a)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=create_nodes, type_ignores=[])),
                 str(SERVER), "exec"), create_ns)
    refused = create_ns["_agent_create_copy"]("QA")
    assert refused["ok"] is False and "BYOK" in refused["err"] and refused["id"] == "agent_qa"
    assert refused["needs_byok"] is True
    assert not any(ep == "/api/agent/delete" for ep, _ in agent_calls)
    created = next(payload for ep, payload in agent_calls if ep == "/api/agent/create")
    assert created["provider"] == "openrouter" and "qwen" in created["model"]
    assert registered == []

    link_nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and
                  node.name in {"_is_qwen_agent_record", "_agent_probe_ok", "_agent_link"}]
    linked = []

    def link_api(endpoint, payload, timeout=0):
        if endpoint == "/api/agent/get":
            return {"id": payload["agent_id"], "name": "Local Qwen", "provider": "custom",
                    "model": "my-qwen-alias"}
        if endpoint == "/api/agent/run":
            return {"status": "completed", "output_text": "готов"}
        return {"status": "error"}

    link_ns = {"api": link_api, "_scrub": str, "_agent_register": lambda *a: linked.append(a)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=link_nodes, type_ignores=[])),
                 str(SERVER), "exec"), link_ns)
    accepted = link_ns["_agent_link"]("agent_custom")
    assert accepted["ok"] is True and linked and linked[0][0] == "agent_custom"
    print("упаковка агента: эксперт + синхронизированные concepts/rules ✓")


if __name__ == "__main__":
    main()
