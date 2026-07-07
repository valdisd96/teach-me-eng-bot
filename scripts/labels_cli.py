#!/usr/bin/env python3
"""Bulk label operations for external agents.

Backs the Hermes-side "label new words" skill: `dump` emits everything a
labelling agent needs (unlabelled words + existing taxonomy with examples)
as JSON on stdout; `apply` attaches a proposed mapping to EXISTING words
only, via `vocab.apply_label_mapping` (unknown words are reported, never
inserted). Run with the project venv from the repo root:

    .venv/bin/python scripts/labels_cli.py dump --chat-id 123
    .venv/bin/python scripts/labels_cli.py apply --chat-id 123 \
        --file mapping.json [--dry-run]

`mapping.json` maps word → label list:
    {"horse": ["pos:noun", "type:animal"], "to give up": ["pos:phrase"]}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import vocab  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default=str(db.DEFAULT_DB_PATH), help="path to vocab.db"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_dump = sub.add_parser("dump", help="emit unlabelled words + taxonomy as JSON")
    p_dump.add_argument("--chat-id", type=int, required=True)

    p_apply = sub.add_parser("apply", help="attach a word→labels mapping")
    p_apply.add_argument("--chat-id", type=int, required=True)
    p_apply.add_argument(
        "--file", required=True, help="JSON file mapping word → list of labels"
    )
    p_apply.add_argument(
        "--dry-run", action="store_true", help="report without writing"
    )

    args = parser.parse_args(argv)
    conn = db.connect(args.db)
    try:
        if args.command == "dump":
            out = vocab.dump_labelling_state(conn, args.chat_id)
        else:
            mapping = json.loads(Path(args.file).read_text(encoding="utf-8"))
            if not isinstance(mapping, dict):
                parser.error("--file must contain a JSON object: word → [labels]")
            items = [(word, labels) for word, labels in mapping.items()]
            out = vocab.apply_label_mapping(
                conn, args.chat_id, items, dry_run=args.dry_run
            )
            out["dry_run"] = args.dry_run
        json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
        print()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
