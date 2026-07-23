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


def load_helpers():
    tree = ast.parse(BUILD.read_text(encoding="utf-8"))
    names = {"_declared_input_extensions", "_select_root_step_inputs", "_output_file_refs",
             "_dependency_input_bundle"}
    wanted = [node for node in tree.body
              if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in wanted} != names:
        raise AssertionError("missing dependency/input-scoping helper")
    ns = {"Path": Path, "re": re, "shutil": shutil, "json": json,
          "universal_file_sha256": lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()}
    exec(compile(ast.fix_missing_locations(ast.Module(body=wanted, type_ignores=[])),
                 str(BUILD), "exec"), ns)
    return tuple(ns[name] for name in
                 ("_declared_input_extensions", "_select_root_step_inputs",
                  "_dependency_input_bundle"))


def main():
    declared_formats, select_root_inputs, helper = load_helpers()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pdf = root / "certificate.pdf"
        xlsx = root / "registry.xlsx"
        csv = root / "rows.csv"
        pdf.write_bytes(b"%PDF-1.7")
        xlsx.write_bytes(b"PK")
        csv.write_text("id,value\n1,a\n", encoding="utf-8")
        physical = [pdf, xlsx, csv]
        pdf_step = {"input_contract": {
            "artifacts": [{"name": "certificate", "format": "application/pdf"}],
            "data_schema": {"type": "records", "format": "pdf"}}}
        excel_step = {"input_contract": {
            "artifacts": [{"name": "registry.xlsx"}],
            "data_schema": {"type": "table", "format": "excel"}}}
        live_excel_step = {"input_contract": {
            "artifacts": ["excel_file"], "data_schema": {"excel_path": "str"},
            "required": True}}
        live_pdf_step = {"input_contract": {
            "artifacts": ["pdf_file"], "data_schema": {"pdf_path": "str"},
            "required": True}}
        assert declared_formats(pdf_step) == {"pdf"}
        assert select_root_inputs(pdf_step, physical) == [pdf]
        assert declared_formats(excel_step) == {"xlsx", "xls", "xlsm"}
        assert select_root_inputs(excel_step, physical) == [xlsx]
        assert select_root_inputs(live_excel_step, physical) == [xlsx]
        assert select_root_inputs(live_pdf_step, physical) == [pdf]
        assert select_root_inputs({"input_contract": {"required": True}}, physical) == physical
        assert select_root_inputs(
            {"input_contract": {"data_schema": {"format": "json"}}}, physical) == []

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
        promoted = root / "batch_002" / "outputs" / "canonical_rows.json"
        promoted.write_text('[{"id": 2}]', encoding="utf-8")
        by_id["batch_002"]["output"]["canonical_rows_json"] = str(promoted)
        step = {"id": "integrate", "version": 2, "dependencies": list(by_id)}
        files = helper(step, by_id, root / "build")
        # canonical/prefixed files stay collision-safe; the unique output also gets its contract
        # basename as an alias, while four colliding source_facts.json files do not.
        assert len(files) == 11, files
        assert len({Path(path).name for path in files}) == 11
        assert len({Path(path).parent for path in files}) == 1
        manifest = json.loads(Path(files[0]).read_text(encoding="utf-8"))
        assert manifest["schema"] == "upc-dependency-bundle/1.3"
        assert [row["name"] for row in manifest["aliases"]] == ["canonical_rows.json"]
        assert (Path(files[0]).parent / "canonical_rows.json").read_text() == '[{"id": 2}]'
        assert not (Path(files[0]).parent / "source_facts.json").exists()
        assert [row["step_id"] for row in manifest["dependencies"]] == list(by_id)
        for row in manifest["dependencies"]:
            expected_count = 3 if row["step_id"] == "batch_002" else 2
            assert len(row["artifacts"]) == expected_count
            canonical = json.loads(Path(row["artifacts"][0]["path"]).read_text())
            assert canonical["schema"] == "upc-dependency-result/1.0"
            assert canonical["structured_data"] == by_id[row["step_id"]]["output"]
            assert Path(row["artifacts"][1]["path"]).read_text() == original_text[row["step_id"]][1]
            assert all(item["source_path"] != item["path"] for item in row["artifacts"])
        batch_two = next(row for row in manifest["dependencies"]
                         if row["step_id"] == "batch_002")
        assert Path(batch_two["artifacts"][2]["path"]).read_text() == '[{"id": 2}]'
    print("dependency bundle: 4 batches -> unique files + manifest in one source directory OK")


if __name__ == "__main__":
    main()
