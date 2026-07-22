#!/usr/bin/env python3
"""Read-only release gate for the Wizard's shared system experts.

Client installations may execute/read global experts but are not release owners and therefore
must never call /api/expert/save. This check proves that the three experts needed by the local
bridge are already published from the same source revision before any client files are copied.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
REQUIRED = ("wz_auto_compose", "wz_build_plan", "wz_generate_blueprint")
META_HEADER = re.compile(r"^#\s*(expert|description|params)\s*:", re.I)


def normalized(code: str) -> str:
    lines = (code or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    while index < len(lines) and (not lines[index].strip() or META_HEADER.match(lines[index])):
        index += 1
    return "\n".join(line.rstrip() for line in lines[index:]).strip()


def fingerprint(code: str) -> str:
    return hashlib.sha256(normalized(code).encode("utf-8")).hexdigest()


def remote_code(base: str, token: str, name: str) -> str:
    payload = json.dumps({"name": name, "global": True}).encode("utf-8")
    request = urllib.request.Request(
        base.rstrip("/") + "/api/expert/get",
        data=payload,
        headers={
            "X-Auth-Token": token,
            "Content-Type": "application/json",
            "X-Profile-Id": "default",
            "X-Agent-Id": "agent_extella_default",
        },
        method="POST",
    )
    last_error = ""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
            expert = result.get("expert") or result
            return result.get("expert_code") or expert.get("code") or ""
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if exc.code < 500:
                break
        except Exception as exc:  # network errors are retried; no secrets enter the message
            last_error = type(exc).__name__
        if attempt < 2:
            time.sleep(attempt + 1)
    raise RuntimeError(last_error or "platform unavailable")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path.home() / "extella_wizard/app/config.json"))
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    token = str(config.get("auth_token") or "").strip()
    if not token:
        raise SystemExit("В config.json нет auth_token — системные эксперты не проверены.")
    base = str(config.get("api_base") or "https://api.extella.ai")

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(REQUIRED)) as pool:
        futures = {name: pool.submit(remote_code, base, token, name) for name in REQUIRED}
        results = {}
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except Exception as exc:
                print(f"  ❌ {name}: общий реестр недоступен ({exc})")
                results[name] = ""

    failed = []
    for name in REQUIRED:
        local_path = ROOT / "experts" / f"{name}.py"
        remote = results.get(name, "")
        if not remote:
            failed.append(name)
            continue
        if fingerprint(local_path.read_text(encoding="utf-8")) != fingerprint(remote):
            print(f"  ❌ {name}: в общем реестре опубликована другая версия")
            failed.append(name)
        else:
            print(f"  ✅ {name}: версия подтверждена")

    if failed:
        print("Обновление остановлено до копирования файлов: общие эксперты релиза ещё не опубликованы владельцем.")
        raise SystemExit(2)
    print("  ✅ три общих системных эксперта соответствуют QA-релизу")


if __name__ == "__main__":
    main()
