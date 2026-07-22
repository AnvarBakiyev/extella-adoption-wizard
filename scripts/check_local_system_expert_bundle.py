#!/usr/bin/env python3
"""Regression: the signed local bundle contains every bridge-owned system expert."""
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))
import wz_local_experts as runtime  # noqa: E402


def main():
    original_dir = runtime.SYSTEM_EXPERT_DIR
    try:
        runtime.SYSTEM_EXPERT_DIR = ROOT / "experts"
        for name in sorted(runtime.LOCAL_SYSTEM_EXPERTS):
            assert runtime.local_system_expert_available(name), name
            assert callable(runtime._load_local_system_expert(name)), name
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "wz_auto_compose.py").write_text(
                '$extens("include.py")\ninclude("import json", [])\n'
                'def wz_auto_compose(task=""):\n'
                '    import json\n'
                '    return json.dumps({"status":"success","task":task}, ensure_ascii=False)\n',
                encoding="utf-8")
            runtime.SYSTEM_EXPERT_DIR = root
            runtime._CACHE.clear()
            result = runtime.run_local_system_expert("wz_auto_compose", {"task": "тест", "extra": 1})
            assert result == {"status": "success", "task": "тест"}
    finally:
        runtime.SYSTEM_EXPERT_DIR = original_dir
        runtime._CACHE.clear()
    print("локальные системные эксперты: 3/3 загружаются из подписанного bundle ✓")


if __name__ == "__main__":
    main()
