"""Local runtime for bridge-owned system experts.

These experts are implementation details of the signed Wizard release, not user-created assets.
Running them from an account-scoped platform registry made two clients see different revisions and
made QA updates attempt forbidden writes.  The functions still call the selected Qwen agent; only
the deterministic Python harness is loaded locally on the same Mac as the bridge and its files.
"""
from __future__ import annotations

import ast
import inspect
import json
import re
import sys
import threading
import types
import urllib.error
import urllib.request
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
SYSTEM_EXPERT_DIR = APP_DIR / "system_experts"
LOCAL_SYSTEM_EXPERTS = frozenset({"wz_auto_compose", "wz_build_plan", "wz_generate_blueprint"})
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_EXTENS_LINE = re.compile(r"(?m)^\s*\$extens\([^\n]*\)\s*$")


def _ensure_requests_compat():
    """Provide the tiny requests.post subset used by legacy Fython sources on clean Python."""
    try:
        __import__("requests")
        return
    except ImportError:
        pass

    class Response:
        def __init__(self, status_code, body):
            self.status_code = int(status_code)
            self.text = body.decode("utf-8", errors="replace")

        def json(self):
            return json.loads(self.text)

    def post(url, headers=None, json=None, timeout=None):
        body = __import__("json").dumps(json or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(str(url), data=body, headers=dict(headers or {}), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout or 180) as response:
                return Response(response.status, response.read())
        except urllib.error.HTTPError as exc:
            return Response(exc.code, exc.read())

    module = types.ModuleType("requests")
    module.post = post
    sys.modules.setdefault("requests", module)


def local_system_expert_available(name):
    return str(name) in LOCAL_SYSTEM_EXPERTS and (SYSTEM_EXPERT_DIR / (str(name) + ".py")).is_file()


def _load_local_system_expert(name):
    name = str(name)
    if name not in LOCAL_SYSTEM_EXPERTS:
        raise ValueError("not a bridge-owned system expert")
    path = SYSTEM_EXPERT_DIR / (name + ".py")
    stat = path.stat()
    with _CACHE_LOCK:
        cached = _CACHE.get(name)
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2]
    source = _EXTENS_LINE.sub("", path.read_text(encoding="utf-8"))
    namespace = {"include": lambda *_args, **_kwargs: None}
    exec(compile(source, str(path), "exec"), namespace)
    function = namespace.get(name)
    if not callable(function):
        raise RuntimeError("bundle has no callable " + name)
    with _CACHE_LOCK:
        _CACHE[name] = (stat.st_mtime_ns, stat.st_size, function)
    return function


def run_local_system_expert(name, params):
    """Execute one bundled system expert and normalize its Fython-style return value."""
    try:
        _ensure_requests_compat()
        function = _load_local_system_expert(name)
        signature = inspect.signature(function)
        accepted = {key: value for key, value in dict(params or {}).items()
                    if key in signature.parameters}
        result = function(**accepted)
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            for loader in (json.loads, ast.literal_eval):
                try:
                    parsed = loader(result)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    continue
            return {"status": "error", "code": "local_system_expert_bad_result",
                    "message": "локальный системный эксперт вернул неструктурированный результат"}
        return {"status": "error", "code": "local_system_expert_bad_result",
                "message": "локальный системный эксперт не вернул результат"}
    except Exception as exc:
        return {"status": "error", "code": "local_system_expert_failed",
                "message": "локальный системный эксперт: " + type(exc).__name__ + ": " + str(exc)[:240]}
