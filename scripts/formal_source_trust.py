#!/usr/bin/env python3
"""Provision, verify, and rotate the formal live-source production trust root."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tripchord.formal_live_source import (
    formal_source_trust_root,
    provision_formal_source_trust_root,
    rotate_formal_source_trust_root,
    verify_formal_source_trust_root,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the owner-only TripChord formal-source trust root"
    )
    parser.add_argument("operation", choices=("init", "verify", "rotate"))
    parser.add_argument(
        "--trust-root",
        type=Path,
        default=formal_source_trust_root(),
        help="owner-only trust-root directory (default: runtime configuration)",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.operation == "init":
        result = provision_formal_source_trust_root(args.trust_root)
    elif args.operation == "verify":
        result = verify_formal_source_trust_root(args.trust_root)
    else:
        result = rotate_formal_source_trust_root(args.trust_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
