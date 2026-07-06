#!/usr/bin/env python3
"""Диагностика: почему мост не находит эксперты. Читает токен из config.json (НЕ печатает его).
Запуск: python3 diag.py"""
import json, os, urllib.request
cfg = json.load(open(os.path.expanduser("~/extella_wizard/app/config.json"), encoding="utf-8"))
tok = cfg.get("auth_token", "")
BASE = cfg.get("api_base", "https://api.extella.ai")
HDR = {"X-Auth-Token": tok, "Content-Type": "application/json",
       "X-Profile-Id": "default", "X-Agent-Id": cfg.get("agent_id", "agent_extella_default")}
print("длина токена:", len(tok), "| agent_id:", cfg.get("agent_id"), "| api_base:", BASE)

def call(path, pl):
    try:
        r = urllib.request.Request(BASE + path, data=json.dumps(pl).encode(), headers=HDR, method="POST")
        with urllib.request.urlopen(r, timeout=30) as x:
            return json.loads(x.read().decode())
    except Exception as e:
        return {"__http_err__": str(e)[:120]}

print("\n1) валиден ли токен (token/validate):")
print("  ", str(call("/api/token/validate", {}))[:160])

print("\n2) есть ли wz_session (get, global=true):")
print("  ", str(call("/api/expert/get", {"name": "wz_session", "global": True}))[:200])

print("\n3) запуск wz_session (global=true):")
print("  ", str(call("/api/expert/run", {"expert_name": "wz_session",
      "params": {"action": "create", "client_name": "diag"}, "global": True}))[:200])

print("\n4) roundtrip: сохранить тест-эксперт и сразу получить (тот же токен/заголовки):")
code = '$extens("include.py")\ndef wz_diagping():\n    return {"ok": True}\n'
sv = call("/api/expert/save", {"name": "wz_diagping", "description": "diag", "code": code,
                               "kwargs": {}, "cspl": "fython", "global": True})
print("   save:", str(sv)[:140])
print("   get :", str(call("/api/expert/get", {"name": "wz_diagping", "global": True}))[:140])
print("   run :", str(call("/api/expert/run", {"expert_name": "wz_diagping", "params": {}, "global": True}))[:140])
