#!/usr/bin/env python3
"""Регрессия чата: временный пустой ответ повторяется один раз, постоянная ошибка — нет."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "ui" / "server.py"
WANTED = {"_agent_output_text", "_chat_should_retry", "_run_chat_agent"}


def load_functions(responses):
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in WANTED]
    if {n.name for n in body} != WANTED:
        raise AssertionError("не найдены функции устойчивого чата в ui/server.py")
    calls = []

    def api(path, payload):
        calls.append((path, payload))
        return responses.pop(0)

    ns = {"api": api}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(SERVER), "exec"), ns)
    return ns, calls


def main():
    success = {"status": "completed", "id": "r2", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": "Продолжаю по образцу."}]}
    ]}
    ns, calls = load_functions([{"status": "error", "message": "temporary upstream error"}, success])
    res, text, attempts = ns["_run_chat_agent"]({"agent_id": "qa"})
    assert res["status"] == "completed" and text == "Продолжаю по образцу."
    assert attempts == 2 and len(calls) == 2

    ns2, calls2 = load_functions([{"status": "error", "message": "401 Incorrect API key"}])
    _res2, text2, attempts2 = ns2["_run_chat_agent"]({"agent_id": "qa"})
    assert not text2 and attempts2 == 1 and len(calls2) == 1

    ns3, calls3 = load_functions([None, success])
    res3, text3, attempts3 = ns3["_run_chat_agent"]({"agent_id": "qa"})
    assert res3["status"] == "completed" and text3 and attempts3 == 2 and len(calls3) == 2

    ns4, calls4 = load_functions([{"status": "completed", "output": []}, success])
    res4, text4, attempts4 = ns4["_run_chat_agent"]({"agent_id": "qa"})
    assert res4["status"] == "completed" and text4 and attempts4 == 2 and len(calls4) == 2
    print("чат: временный сбой → один повтор; ключ/права → без бессмысленного повтора ✓")


if __name__ == "__main__":
    main()
