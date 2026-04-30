"""Compare matching files in output/slot and output/sel by sample_id."""

import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SLOT_DIR = REPO_ROOT / "output" / "slot"
SEL_DIR = REPO_ROOT / "output" / "sel"


def load_by_sample_id(path: Path) -> dict[int, dict]:
    with open(path) as f:
        data = json.load(f)
    return {entry["sample_id"]: entry for entry in data}


def compare_file(name: str, slot_dir: Path = SLOT_DIR, sel_dir: Path = SEL_DIR) -> dict:
    slot_map = load_by_sample_id(slot_dir / name)
    sel_map = load_by_sample_id(sel_dir / name)

    slot_only = sorted(set(slot_map) - set(sel_map))
    sel_only = sorted(set(sel_map) - set(slot_map))

    result = {
        "file": name,
        "slot_count": len(slot_map),
        "sel_count": len(sel_map),
        "sample_ids_match": not slot_only and not sel_only,
        "in_slot_not_sel": [
            {"sample_id": sid, "query": slot_map[sid]["query"]} for sid in slot_only
        ],
        "in_sel_not_slot": [
            {"sample_id": sid, "query": sel_map[sid]["query"]} for sid in sel_only
        ],
    }

    if not slot_only and not sel_only:
        print(f"{name}: all sample_ids match ({len(slot_map)} entries)")
        return result

    print(f"\n{'=' * 60}")
    print(f"File: {name}  (slot={len(slot_map)}, sel={len(sel_map)})")

    if slot_only:
        print(f"\n  In slot but not sel ({len(slot_only)}):")
        for sid in slot_only:
            print(f"    [{sid}] {slot_map[sid]['query']}")

    if sel_only:
        print(f"\n  In sel but not slot ({len(sel_only)}):")
        for sid in sel_only:
            print(f"    [{sid}] {sel_map[sid]['query']}")

    return result


def main() -> None:
    slot_files = {p.name for p in SLOT_DIR.glob("*.json")}
    sel_files = {p.name for p in SEL_DIR.glob("*.json")}
    common = sorted(slot_files & sel_files)
    slot_only_files = sorted(slot_files - sel_files)
    sel_only_files = sorted(sel_files - slot_files)

    if slot_only_files:
        print(f"Files only in slot/: {slot_only_files}")
    if sel_only_files:
        print(f"Files only in sel/:  {sel_only_files}")

    if not common:
        print("No matching files to compare.")
        return

    print(f"Comparing {len(common)} file(s): {common}\n")
    for name in common:
        compare_file(name)


if __name__ == "__main__":
    main()
