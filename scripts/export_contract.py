#!/usr/bin/env python
"""Export the API schema and regenerate the SPA's TypeScript types.

Run from the repository root:

    uv run python scripts/export_contract.py

The SPA's types are generated from ``/openapi.json`` rather than hand-written, because
a hand-written copy of a contract drifts from it silently -- which is exactly what
happened here: an ``ArchiveEntityListResponse`` written from memory described
``{entities, count, note}`` while the endpoint returned ``{items, total, page, page_size}``.
Declaring the response models made the endpoint reject its own output, which is how the
discrepancy was found rather than shipped.

``--check`` regenerates into a temporary location and fails if the committed files
differ, so CI catches a schema change that was not regenerated.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "apps" / "web" / "openapi.json"
TYPES_PATH = ROOT / "apps" / "web" / "src" / "api" / "schema.gen.ts"
OPENAPI_TYPESCRIPT = "openapi-typescript@7"


def generate_schema() -> str:
    """The OpenAPI document, as a stable string.

    Sorted keys and a fixed indent so regeneration is byte-identical when nothing
    changed; otherwise a view-order change would show up as a spurious diff.
    """
    from metaedit.main import create_app

    spec = create_app().openapi()
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"


def generate_types(schema_file: Path, output: Path) -> None:
    """Run openapi-typescript, using a workspace-local npm cache.

    The default cache lives in ``$HOME``, which is not writable in the sandboxed
    environment this project is developed in.
    """
    cache = ROOT / ".cache" / "npm"
    cache.mkdir(parents=True, exist_ok=True)
    logs = ROOT / ".cache" / "npm-logs"
    logs.mkdir(parents=True, exist_ok=True)
    # Inherit the environment (node may live under a version manager) and override
    # only where the sandbox requires a workspace-local directory.
    env = {
        **os.environ,
        "PATH": f"{ROOT / '.tools'}:{os.environ.get('PATH', '')}",
        "npm_config_cache": str(cache),
        "npm_config_logs_dir": str(logs),
    }
    subprocess.run(
        ["npx", "--yes", OPENAPI_TYPESCRIPT, str(schema_file), "-o", str(output)],
        check=True,
        cwd=ROOT,
        env=env,
    )


def main(argv: list[str]) -> int:
    check = "--check" in argv
    schema = generate_schema()

    if check:
        problems: list[str] = []
        if not SCHEMA_PATH.exists() or SCHEMA_PATH.read_text() != schema:
            problems.append(
                f"{SCHEMA_PATH.relative_to(ROOT)} is stale; run scripts/export_contract.py"
            )
        if problems:
            # Regenerating from a stale or unreadable schema fails inside npx, and its
            # traceback would bury the actual problem. Report and stop.
            for problem in problems:
                print(f"  ✗ {problem}", file=sys.stderr)
            return 1
        try:
            with tempfile.TemporaryDirectory() as tmp:
                fresh = Path(tmp) / "schema.gen.ts"
                generate_types(SCHEMA_PATH, fresh)
                if not TYPES_PATH.exists() or TYPES_PATH.read_text() != fresh.read_text():
                    problems.append(
                        f"{TYPES_PATH.relative_to(ROOT)} is stale; run scripts/export_contract.py"
                    )
        except subprocess.CalledProcessError:
            problems.append(
                f"{SCHEMA_PATH.relative_to(ROOT)} could not be turned into types; "
                "it is likely invalid"
            )
        for problem in problems:
            print(f"  ✗ {problem}", file=sys.stderr)
        if problems:
            return 1
        print("  ✓ the committed contract matches the app")
        return 0

    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(schema)
    generate_types(SCHEMA_PATH, TYPES_PATH)
    paths = len(json.loads(schema)["paths"])
    schemas = len(json.loads(schema)["components"]["schemas"])
    print(f"  ✓ {SCHEMA_PATH.relative_to(ROOT)}: {paths} paths, {schemas} schemas")
    print(f"  ✓ {TYPES_PATH.relative_to(ROOT)}: {len(TYPES_PATH.read_text().splitlines())} lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
