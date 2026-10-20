#!/usr/bin/env python3
"""Generate the deterministic fixture set described by bench/labels.json."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from sloth_valet.fixtures import (  # noqa: E402
    generate_fixtures,
    load_fixture_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--labels",
        type=Path,
        default=PROJECT_ROOT / "bench" / "labels.json",
        help="Path to the committed labels JSON (default: bench/labels.json).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "bench" / "fixtures",
        help="Directory to write fixtures into (default: bench/fixtures).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Regenerate expected fixture files that already exist.",
    )
    arguments = parser.parse_args()

    try:
        manifest = load_fixture_manifest(arguments.labels)
        paths = generate_fixtures(
            manifest,
            arguments.output,
            overwrite=arguments.overwrite,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Generated {len(paths)} fixtures in {arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
