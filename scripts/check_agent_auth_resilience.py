#!/usr/bin/env python3
"""Regression: platform auth loss must never masquerade as a missing agent."""
import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SERVER = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
HTML = (ROOT / "ui" / "wizard.html").read_text(encoding="utf-8")


def extracted_helpers():
    tree = ast.parse(SERVER)
    names = {"_platform_auth_error", "_platform_auth_message", "_llm_error_human"}
    nodes = [node for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {"json": json}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 "server.py", "exec"), namespace)
    return namespace


def main():
    ns = extracted_helpers()
    is_auth = ns["_platform_auth_error"]
    human = ns["_llm_error_human"]
    missing_user = {"status": "error", "http_code": 401,
                    "message": "Authentication required (user_id missing)"}
    provider_key = {"status": "error", "http_code": 401,
                    "message": "Incorrect provider API key"}
    assert is_auth(missing_user)
    assert not is_auth(provider_key)
    message = human(missing_user)
    assert "Start" in message and "Агент не удалён" in message and "новый не нужно" in message
    assert '_platform_auth_error(g)' in SERVER
    assert '"platform_ready": False' in SERVER
    assert '"auth_required": (not ok and _platform_auth_error(r))' in SERVER
    assert 'st.platform_ready===false' in HTML
    assert 'Существующий агент не удалён' in HTML
    assert 'Не создавайте и не привязывайте его повторно' in HTML
    print("auth gate: user_id missing != agent missing; recovery points to Start/sign-in ✓")


if __name__ == "__main__":
    main()
