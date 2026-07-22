#!/usr/bin/env python3
"""Regression: a merge step receives every dependency in one collision-free package."""
import ast
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "ui" / "wz_build.py"


def load_helper():
    tree = ast.parse(BUILD.read_text(encoding="utf-8"))
    wanted = [node for node in tree.body
              if isinstance(node, ast.FunctionDef) and node.name == "_dependency_input_bundle"]
    if len(wanted) != 1:
        raise AssertionError("missing dependency bundle helper")
    ns = {"Path": Path, "re": re, "shutil": shutil, "json": json,
          "universal_file_sha256": lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()}
    exec(compile(ast.fix_missing_locations(ast.Module(body=wanted, type_ignores=[])),
                 str(BUILD), "exec"), ns)
    return ns["_dependency_input_bundle"]


def main():
    helper = load_helper()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        original_text = {}
        by_id = {}
        for index, dep in enumerate(("batch_001", "batch_002", "batch_003", "batch_004"), 1):
            out = root / dep / "outputs"
            out.mkdir(parents=True)
            result = out / "step_result.json"
            facts = out / "source_facts.json"
            result.write_text(json.dumps({"status": "success", "batch": dep,
                                          "total_records": index * 100}), encoding="utf-8")
            facts.write_text(json.dumps({"batch": dep, "facts": [index]}), encoding="utf-8")
            original_text[dep] = (result.read_text(), facts.read_text())
            by_id[dep] = {"version": 1, "status": "accepted", "output": {"batch": dep},
                          "artifact_refs": [{"kind": "step_result_json", "path": str(result)},
                                            {"kind": "source_facts", "path": str(facts)}]}
        step = {"id": "integrate", "version": 2, "dependencies": list(by_id)}
        files = helper(step, by_id, root / "build")
        assert len(files) == 9, files  # manifest + two artifacts for each of four batches
        assert len({Path(path).name for path in files}) == 9
        assert len({Path(path).parent for path in files}) == 1
        manifest = json.loads(Path(files[0]).read_text(encoding="utf-8"))
        assert manifest["schema"] == "upc-dependency-bundle/1.1"
        assert [row["step_id"] for row in manifest["dependencies"]] == list(by_id)
        for row in manifest["dependencies"]:
            assert len(row["artifacts"]) == 2
            original = original_text[row["step_id"]]
            assert tuple(Path(item["path"]).read_text() for item in row["artifacts"]) == original
            assert all(item["source_path"] != item["path"] for item in row["artifacts"])
    print("dependency bundle: 4 batches -> unique files + manifest in one source directory OK")


if __name__ == "__main__":
    main()
