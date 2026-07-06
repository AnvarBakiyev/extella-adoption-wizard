# expert: wz_ask_agent
# description: Эксперт wz_ask_agent (Adoption Wizard).
# params: agent_name, agent_id, input_text, previous_response_id, api_token, run_timeout, api_base

$extens("include.py")
include("import requests", ["extella-pip install requests"])

def wz_ask_agent(
    agent_name: str = "",
    agent_id: str = "",
    input_text: str = "",
    previous_response_id: str = "",
    api_token: str = "",
    run_timeout: int = 240,
    api_base: str = "https://api.extella.ai"
) -> dict:
    import json
    import requests
    from pathlib import Path

    if not input_text:
        return {"status": "error", "message": "input_text is required"}
    if not agent_id and not agent_name:
        return {"status": "error", "message": "agent_id or agent_name is required"}

    # Doctrine-friendly token resolution: explicit param, else the device
    # bridge config (written by wz_wizard_serve, chmod 600) - the expert runs
    # on the same device, so no secret ever travels through the agent call.
    if not api_token:
        cfg = Path.home() / "extella_wizard" / "app" / "config.json"
        if cfg.exists():
            try:
                api_token = json.loads(cfg.read_text(encoding="utf-8")).get("auth_token", "")
            except Exception:
                pass
    if not api_token:
        return {"status": "error",
                "message": "api_token not provided and no bridge config on this device (run wz_wizard_serve once)"}

    headers = {"X-Auth-Token": api_token, "Content-Type": "application/json",
               "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}

    def xapi(ep, payload, timeout=300):
        r = requests.post(api_base.rstrip("/") + ep, headers=headers, json=payload, timeout=timeout)
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:300]}
        if r.status_code not in (200, 201):
            return {"status": "error", "http": r.status_code, "message": str(body)[:300]}
        return body

    # Resolve agent by name when id not given
    if not agent_id:
        lst = xapi("/api/agent/list", {})
        agents = lst if isinstance(lst, list) else (lst.get("agents") or lst.get("results") or [])
        needle = agent_name.strip().lower()
        match = [a for a in agents if needle in str(a.get("name", "")).lower()]
        if not match:
            return {"status": "error", "message": "agent not found by name: " + agent_name,
                    "available": [str(a.get("name"))[:40] for a in agents][:15]}
        # prefer exact, else first
        exact = [a for a in match if str(a.get("name", "")).strip().lower() == needle]
        agent_id = (exact or match)[0].get("id")

    payload = {"agent_id": agent_id, "input": input_text[:12000],
               "run_timeout": max(30, min(1800, int(run_timeout))), "store": True}
    if previous_response_id:
        payload["previous_response_id"] = previous_response_id
    res = xapi("/api/agent/run", payload, timeout=max(60, int(run_timeout)) + 60)
    if res.get("status") == "error":
        return {"status": "error", "message": "agent run failed: " + str(res.get("message"))[:250],
                "agent_id": agent_id}

    text = ""
    for item in res.get("output", []) or []:
        if item.get("type") == "message":
            for c in item.get("content", []) or []:
                if c.get("type") == "output_text":
                    text += c.get("text", "")
    return {"status": "success", "agent_id": agent_id,
            "response_id": res.get("id"),
            "reply": text[:6000] if text else "(пустой ответ)",
            "hint": "для продолжения диалога передай previous_response_id=" + str(res.get("id"))}
