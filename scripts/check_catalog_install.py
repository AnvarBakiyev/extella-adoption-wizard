#!/usr/bin/env python3
"""Регрессия: чистый Mac получает каталог, а мост восстанавливает canonical-копию."""
import ast
import json
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SERVER = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
INSTALL = (ROOT / "install.py").read_text(encoding="utf-8")
DELTA = (ROOT / "scripts" / "qa_delta_update.sh").read_text(encoding="utf-8")


def extracted_ensure():
    tree = ast.parse(SERVER)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_ensure_catalog_path")
    ns = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), "server.py", "exec"), ns)
    return ns["_ensure_catalog_path"]


def main():
    catalog = json.loads((ROOT / "catalog" / "catalog.json").read_text(encoding="utf-8"))
    assert catalog.get("capabilities") and catalog.get("process_archetypes")
    assert 'os.path.join(HERE, "catalog", "catalog.json")' in INSTALL
    assert 'os.path.join(cat_dir, "catalog.json")' in INSTALL
    assert 'os.path.join(app_dir, "catalog.json")' in INSTALL
    assert 'cp "$SRC/catalog/catalog.json" "$CAT_DIR/catalog.json"' in DELTA
    assert 'cp "$SRC/catalog/catalog.json" "$APP_DIR/catalog.json"' in DELTA
    assert 'params.setdefault("catalog_path", str(_cp))' in SERVER
    ensure = extracted_ensure()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cat, app = root / "catalog", root / "app"
        app.mkdir()
        (app / "catalog.json").write_text('{"catalog_version":"test"}', encoding="utf-8")
        ensure.__globals__.update({"_CAT_DIR": cat, "APP_DIR": app})
        resolved = ensure()
        assert resolved == cat / "catalog.json" and resolved.exists()
        assert resolved.read_text(encoding="utf-8") == '{"catalog_version":"test"}'
    print("каталог: full install + QA delta + local self-heal ✓")


if __name__ == "__main__":
    main()
