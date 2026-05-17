"""
VGDD2 card table dumper - MVP v2

Changes from v1:
- Does NOT stop when card_id == 0.
- Iterates a fixed number of rows or for a fixed number of seconds.
- Prints every card found with a non-empty name to the terminal.
- Duplicate card IDs are replaced by the latest seen row.

Install:
    pip install pymem

Run while the game is open:
    python dump_vgdd2_cards_v2.py

Optional:
    python dump_vgdd2_cards_v2.py --rows 5000
    python dump_vgdd2_cards_v2.py --seconds 10
    python dump_vgdd2_cards_v2.py --start 0x1778D8E4710
"""

from __future__ import annotations

import argparse
import csv
import time
from dataclasses import dataclass
from pathlib import Path

import pymem


PROCESS_NAME = "VGDD2.exe"

# Current-session table start. Change this after restarting the game.
DEFAULT_START_ROW = 0x1778D8E4710

ROW_SIZE = 0x128
CARD_ID_OFF = 0x00
STAT_PTR_OFF = 0x10
NAME_PTR_OFF = 0xA0

ATTACK_OFF = 0x48
SHIELD_OFF = 0x4C

OUTPUT_CSV = "vgdd2_cards_dump.csv"

DEFAULT_MAX_ROWS = 10000
DEFAULT_SECONDS = 0.0  # 0 = no time limit, row limit only
MAX_NAME_CHARS = 160


@dataclass
class Card:
    card_id: int
    name: str
    attack: int
    shield: int
    row_addr: int
    stat_ptr: int
    name_ptr: int


def read_u32(pm: pymem.Pymem, addr: int) -> int:
    return pm.read_uint(addr)


def read_u64(pm: pymem.Pymem, addr: int) -> int:
    return pm.read_ulonglong(addr)


def read_utf16z(pm: pymem.Pymem, addr: int, max_chars: int = MAX_NAME_CHARS) -> str:
    if addr == 0:
        return ""

    raw = bytearray()

    for i in range(max_chars):
        try:
            two = pm.read_bytes(addr + i * 2, 2)
        except Exception:
            break

        if two == b"\x00\x00":
            break

        raw.extend(two)

    try:
        text = raw.decode("utf-16-le", errors="replace")
    except Exception:
        return ""

    # Remove obvious junk/control chars but keep Japanese/English text.
    text = text.replace("\x00", "").strip()
    return text


def looks_valid_pointer(ptr: int) -> bool:
    # Broad x64 user-space sanity check.
    # Your current pointers are around 0x00000177...
    return 0x10000 <= ptr <= 0x7FFFFFFFFFFF


def looks_like_name(name: str) -> bool:
    if not name:
        return False

    # Avoid random single-character / garbage decodes.
    if len(name) < 2:
        return False

    # If it is mostly replacement/control characters, reject.
    bad = sum(1 for ch in name if ch == "\ufffd" or ord(ch) < 32)
    return bad <= max(1, len(name) // 4)


def dump_cards(start_row: int, max_rows: int, seconds: float) -> list[Card]:
    pm = pymem.Pymem(PROCESS_NAME)

    cards_by_id: dict[int, Card] = {}
    start_time = time.monotonic()

    row = start_row

    for i in range(max_rows):
        if seconds > 0 and (time.monotonic() - start_time) >= seconds:
            print(f"[STOP] Time limit reached after {seconds:.2f}s at row {i}, addr 0x{row:X}")
            break

        try:
            card_id = read_u32(pm, row + CARD_ID_OFF)
            stat_ptr = read_u64(pm, row + STAT_PTR_OFF)
            name_ptr = read_u64(pm, row + NAME_PTR_OFF)
        except Exception as e:
            print(f"[WARN] Failed reading row {i} at 0x{row:X}: {e}")
            row += ROW_SIZE
            continue

        # Do NOT stop on card_id == 0 anymore.
        # Just skip unusable-looking rows.
        name = ""
        attack = 0
        shield = 0

        if looks_valid_pointer(name_ptr):
            name = read_utf16z(pm, name_ptr)

        if looks_valid_pointer(stat_ptr):
            try:
                attack = read_u32(pm, stat_ptr + ATTACK_OFF)
                shield = read_u32(pm, stat_ptr + SHIELD_OFF)
            except Exception:
                attack = 0
                shield = 0

        if card_id != 0 and looks_like_name(name):
            card = Card(
                card_id=card_id,
                name=name,
                attack=attack,
                shield=shield,
                row_addr=row,
                stat_ptr=stat_ptr,
                name_ptr=name_ptr,
            )

            replaced = card_id in cards_by_id
            cards_by_id[card_id] = card

            tag = "REPLACE" if replaced else "FOUND"
            print(
                f"[{tag}] row={i:05d} "
                f"id=0x{card_id:04X} "
                f"atk={attack:<5} shield={shield:<5} "
                f"name={name!r} "
                f"row=0x{row:X}"
            )

        elif i % 500 == 0:
            print(
                f"[SCAN] row={i:05d} id=0x{card_id:04X} "
                f"namePtr=0x{name_ptr:X} statPtr=0x{stat_ptr:X}"
            )

        row += ROW_SIZE

    return list(cards_by_id.values())


def write_csv(cards: list[Card], path: str | Path = OUTPUT_CSV) -> None:
    cards = sorted(cards, key=lambda c: c.card_id)

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "card_id_hex",
                "card_id_dec",
                "name",
                "attack",
                "shield",
            ],
        )
        writer.writeheader()

        for c in cards:
            writer.writerow(
                {
                    "card_id_hex": f"{c.card_id:04X}",
                    "card_id_dec": c.card_id,
                    "name": c.name,
                    "attack": c.attack,
                    "shield": c.shield,
                }
            )


def parse_int(value: str) -> int:
    return int(value, 0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=parse_int, default=DEFAULT_START_ROW)
    parser.add_argument("--rows", type=int, default=DEFAULT_MAX_ROWS)
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS)
    parser.add_argument("--out", type=str, default=OUTPUT_CSV)
    args = parser.parse_args()

    print(f"[INFO] Process: {PROCESS_NAME}")
    print(f"[INFO] Start row: 0x{args.start:X}")
    print(f"[INFO] Row size: 0x{ROW_SIZE:X}")
    print(f"[INFO] Max rows: {args.rows}")
    print(f"[INFO] Seconds: {args.seconds if args.seconds > 0 else 'no limit'}")
    print()

    cards = dump_cards(args.start, args.rows, args.seconds)
    write_csv(cards, args.out)

    print()
    print(f"[DONE] Dumped {len(cards)} unique non-empty card IDs to {args.out}")
    print("[NOTE] Duplicate IDs were replaced by the latest seen row.")


if __name__ == "__main__":
    main()
