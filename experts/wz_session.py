# expert: wz_session
# description: Adoption Wizard core: file-based session store for the business-process adoption wizard, kept on the client device (Listener) so client data never lea
# params: 

def wz_session(
    action: str = "get",
    session_id: str = "",
    base_dir: str = "",
    client_name: str = "",
    payload_json: str = "",
    stage: str = ""
) -> dict:
    import json, uuid
    from pathlib import Path
    from datetime import datetime, timezone

    def now():
        return datetime.now(timezone.utc).isoformat()

    root = Path(base_dir) if base_dir else Path.home() / "extella_wizard" / "sessions"
    root.mkdir(parents=True, exist_ok=True)

    STAGES = ["intake", "interview", "blueprint", "test", "launch"]

    def spath(sid):
        return root / (sid + ".json")

    def load(sid):
        p = spath(sid)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def save(s):
        s["updated_at"] = now()
        spath(s["session_id"]).write_text(
            json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")

    if action == "create":
        sid = "wz_" + datetime.now(timezone.utc).strftime("%Y%m%d") + "_" + uuid.uuid4().hex[:6]
        s = {"session_id": sid,
             "client_name": client_name or "Без названия",
             "created_at": now(), "updated_at": now(),
             "stage": "interview",
             "answers": {}, "comments": [], "blueprint_path": "",
             "log": [{"ts": now(), "event": "session created"}]}
        save(s)
        return {"status": "success", "session": s, "path": str(spath(sid))}

    if action == "list":
        out = []
        for p in sorted(root.glob("wz_*.json")):
            try:
                s = json.loads(p.read_text(encoding="utf-8"))
                out.append({"session_id": s.get("session_id"),
                            "client_name": s.get("client_name"),
                            "stage": s.get("stage"),
                            "updated_at": s.get("updated_at"),
                            "answers_count": len(s.get("answers", {})),
                            "comments_open": sum(1 for c in s.get("comments", []) if not c.get("resolved"))})
            except Exception:
                continue
        return {"status": "success", "sessions": out, "count": len(out)}

    if not session_id:
        return {"status": "error", "message": "session_id is required for action '" + action + "'"}
    s = load(session_id)
    if s is None:
        return {"status": "error", "message": "session not found: " + session_id}

    if action == "get":
        return {"status": "success", "session": s, "path": str(spath(session_id))}

    if action == "save_answers":
        try:
            answers = json.loads(payload_json)
            assert isinstance(answers, dict) and answers
        except Exception:
            return {"status": "error", "message": "payload_json must be a non-empty JSON object {question_id: answer | {question, answer}}"}
        for qid, ans in answers.items():
            if isinstance(ans, dict):
                rec = {"answer": str(ans.get("answer", "")),
                       "question": str(ans.get("question", ""))}
            else:
                rec = {"answer": str(ans),
                       "question": s.get("answers", {}).get(qid, {}).get("question", "")}
            rec["updated_at"] = now()
            s["answers"][qid] = rec
        s["log"].append({"ts": now(), "event": "answers saved: " + ", ".join(list(answers.keys())[:10])})
        save(s)
        return {"status": "success", "answers_count": len(s["answers"])}

    if action == "add_comment":
        try:
            c = json.loads(payload_json)
            assert isinstance(c, dict) and c.get("text")
        except Exception:
            return {"status": "error", "message": "payload_json must be JSON {block_ref, author, text} with non-empty text"}
        cid = "c_" + uuid.uuid4().hex[:8]
        s["comments"].append({"id": cid,
                              "block_ref": str(c.get("block_ref", "")),
                              "author": str(c.get("author", "client")),
                              "text": str(c["text"]),
                              "created_at": now(), "resolved": False})
        s["log"].append({"ts": now(), "event": "comment added on " + str(c.get("block_ref", ""))})
        save(s)
        return {"status": "success", "comment_id": cid, "comments_count": len(s["comments"])}

    if action == "resolve_comment":
        target_id = payload_json.strip().strip('"')
        for c in s["comments"]:
            if c["id"] == target_id:
                c["resolved"] = True
                s["log"].append({"ts": now(), "event": "comment resolved: " + target_id})
                save(s)
                return {"status": "success", "comment_id": target_id}
        return {"status": "error", "message": "comment not found: " + target_id}

    if action == "set_stage":
        if stage not in STAGES:
            return {"status": "error", "message": "stage must be one of " + ", ".join(STAGES)}
        s["stage"] = stage
        s["log"].append({"ts": now(), "event": "stage -> " + stage})
        save(s)
        return {"status": "success", "stage": stage}

    if action == "add_decision":
        try:
            d = json.loads(payload_json)
            assert isinstance(d, dict) and d.get("decision")
        except Exception:
            return {"status": "error", "message": "payload_json must be JSON {decision, reason, author} with non-empty decision"}
        rec = {"id": "d_" + uuid.uuid4().hex[:8], "decision": str(d["decision"]),
               "reason": str(d.get("reason", "")), "author": str(d.get("author", "builder")),
               "created_at": now()}
        s.setdefault("decisions", []).append(rec)
        s["log"].append({"ts": now(), "event": "decision recorded: " + rec["decision"][:60]})
        save(s)
        return {"status": "success", "decision_id": rec["id"], "decisions_count": len(s["decisions"])}

    if action == "set_blueprint":
        s["blueprint_path"] = payload_json.strip().strip('"')
        s["log"].append({"ts": now(), "event": "blueprint attached: " + s["blueprint_path"]})
        save(s)
        return {"status": "success", "blueprint_path": s["blueprint_path"]}

    return {"status": "error", "message": "unknown action: " + action}