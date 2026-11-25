"""Verify every module app/streamlit_app.py imports is listed in snowflake.yaml.

    python scripts/check_app_artifacts.py

A `snow streamlit deploy` uploads only the files named under `artifacts`. Miss
one and the deploy succeeds, then the app dies at import time in Snowflake -
a failure that cannot happen locally, because locally every file is simply
there. This walks the local imports and checks each against the manifest.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
MANIFEST = APP / "snowflake.yaml"


def local_modules() -> set[str]:
    """Modules importable from app/ - i.e. the ones that must be uploaded."""
    return {p.stem for p in APP.glob("*.py")}


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def listed_artifacts() -> set[str]:
    """Artifact filenames from snowflake.yaml, read without a YAML dependency."""
    out: set[str] = set()
    inside = False
    for line in MANIFEST.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("artifacts:"):
            inside = True
            continue
        if inside:
            if stripped.startswith("- "):
                out.add(stripped[2:].strip())
            elif stripped and not stripped.startswith("#"):
                inside = False
    return out


def main() -> int:
    modules = local_modules()
    artifacts = listed_artifacts()
    print(f"artifacts in snowflake.yaml: {', '.join(sorted(artifacts))}\n")

    missing: list[str] = []
    for source in sorted(APP.glob("*.py")):
        needed = sorted((imports_of(source) & modules) - {source.stem})
        for module in needed:
            filename = f"{module}.py"
            ok = filename in artifacts
            print(f"  {'OK  ' if ok else 'MISSING'}  {source.name} imports {filename}")
            if not ok:
                missing.append(filename)

    entry = "streamlit_app.py"
    if entry not in artifacts:
        print(f"  MISSING  entry point {entry}")
        missing.append(entry)

    orphans = {a for a in artifacts if a.endswith(".py")} - {
        f"{m}.py" for m in modules
    }
    for orphan in sorted(orphans):
        print(f"  WARN     {orphan} is listed but does not exist in app/")

    for artifact in sorted(artifacts):
        if not (APP / artifact).exists():
            print(f"  WARN     {artifact} is listed but not present")

    print()
    if missing:
        print(f"FAIL: {len(set(missing))} file(s) imported but not deployed: "
              f"{', '.join(sorted(set(missing)))}")
        return 1
    print("PASS: every imported module is in the artifacts list.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
