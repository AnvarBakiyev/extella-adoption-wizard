#!/usr/bin/env python3
"""Regression: rapid repeated repair/build clicks must start exactly one worker."""
import ast
import json
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = ROOT / "ui" / "server.py"
WIZARD_PATH = ROOT / "ui" / "wizard.html"
BUILD_PATH = ROOT / "ui" / "wz_build.py"


def load_start_build(run_dir, sessions, starts):
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
    fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
              and node.name == "_start_build_job")

    def update_session(sid, mutate):
        mutate(sessions[sid])
        return sessions[sid]

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None):
            self.target, self.args = target, args

        def start(self):
            starts.append(self.args)

    ns = {
        "datetime": datetime, "timezone": timezone, "uuid": uuid, "json": json,
        "RUNS_DIR": run_dir, "SAFE_ID": __import__("re").compile(r"^[A-Za-z0-9_.-]+$"),
        "_update_session": update_session, "threading": type("T", (), {"Thread": FakeThread}),
        "_run_build": lambda *_: None,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
                 "server.py", "exec"), ns)
    return ns["_start_build_job"]


def main():
    server = SERVER_PATH.read_text(encoding="utf-8")
    wizard = WIZARD_PATH.read_text(encoding="utf-8")
    build = BUILD_PATH.read_text(encoding="utf-8")
    assert "_process_action_lock(sid)" in server and "action_lock.acquire(blocking=False)" in server
    assert '"already_started": True' in server
    assert "PROCESS_REPAIR_PENDING" in wizard and "data-process-repair" in wizard
    assert "Ремонт запускается" in wizard
    assert "function clearProcessRepairPending()" in wizard
    assert wizard.count("clearProcessRepairPending();") >= 3
    assert 'str(_s.get("building") or "") == build_id' in build

    with tempfile.TemporaryDirectory(prefix="wz_build_dedupe_") as td:
        runs = Path(td)
        sessions = {"wz_test": {"session_id": "wz_test", "building": "build_live"}}
        live = runs / "build_live"
        live.mkdir()
        (live / "build_progress.json").write_text(json.dumps({"status": "running"}), encoding="utf-8")
        starts = []
        start_build = load_start_build(runs, sessions, starts)

        returned = start_build("wz_test")
        assert returned == "build_live" and starts == []
        assert sessions["wz_test"]["building"] == "build_live"
        assert [p.name for p in runs.iterdir()] == ["build_live"]

        (live / "build_progress.json").write_text(json.dumps({"status": "error"}), encoding="utf-8")
        returned = start_build("wz_test")
        assert returned != "build_live" and len(starts) == 1
        assert sessions["wz_test"]["building"] == returned

    print("rapid click: immediate UI ack + one atomic build worker per session ✓")


if __name__ == "__main__":
    main()
