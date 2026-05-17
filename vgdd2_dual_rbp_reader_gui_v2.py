"""
VGDD2 dual-player RBP reader GUI with card images

MVP flow:
1. In Cheat Engine, get RBP from the zone write instruction:
       dec [rbp+0C]
2. Paste opponent RBP and/or your own RBP into the GUI.
3. The script reads deck/hand and maps card IDs to your CSV.
4. Click a card row to show its image from:
       vanguard_scrape_out/images/

Confirmed structure:
    zone size              = 0x28
    zone + 0x0C            = count
    zone + 0x18            = card object pointer array holder
    array[i]               = card object pointer
    card object + 0x18     = raw card ID

Zone mapping from an RBP that points at Zone 0 / deck:
    deck count             = RBP + 0x0C
    deck array holder      = RBP + 0x18
    hand count             = RBP + 0x34
    hand array holder      = RBP + 0x40

Install:
    pip install pymem pillow

Run:
    python vgdd2_dual_rbp_reader_gui.py --cards vgdd2_cards_dump.csv --images vanguard_scrape_out/images

Pillow is optional but strongly recommended for resizing images.
"""

from __future__ import annotations

import argparse
import csv
import re
import tkinter as tk
from tkinter import ttk, messagebox
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from difflib import SequenceMatcher

import pymem

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False


PROCESS_NAME = "VGDD2.exe"

ZONE_SIZE = 0x28
COUNT_OFF = 0x0C
ARRAY_HOLDER_OFF = 0x18
CARD_OBJECT_ID_OFFSET = 0x18

DECK_ZONE_INDEX = 0
HAND_ZONE_INDEX = 1

MAX_DECK_READ = 60
MAX_HAND_READ = 20

DEFAULT_OPP_RBP = 0x16705B414B0
DEFAULT_MY_RBP = 0x0


@dataclass
class CardInfo:
    card_id: int
    name: str
    attack: str = ""
    shield: str = ""


@dataclass
class CardRow:
    owner: str
    zone: str
    index: int
    obj: int
    card_id: int
    label: str
    name: str


def parse_hex_or_dec(s: str) -> int:
    s = s.strip().replace("`", "").replace(" ", "")
    if not s:
        raise ValueError("Empty address")
    if "=" in s:
        s = s.split("=")[-1].strip()
    if s.lower().startswith("0x"):
        return int(s, 16)
    if any(ch in "abcdefABCDEF" for ch in s) or len(s) > 10:
        return int(s, 16)
    return int(s, 10)


def normalize_name(s: str) -> str:
    s = s.lower()
    s = s.replace("&", " and ")
    s = re.sub(r"\.(png|jpg|jpeg|webp)$", "", s)
    # remove card number prefix from scraped filenames:
    # EB02_030EN__Drive_Quartet,_Ressac -> Drive_Quartet,_Ressac
    if "__" in s:
        s = s.split("__", 1)[1]
    # remove standalone card code just in case
    s = re.sub(r"\b[a-z0-9]+[_/-][a-z0-9]+en\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_card_id(card_id: int) -> int:
    """
    VGDD2 sometimes stores your-side cards with the high flag bit set:
        80003AC6 -> real card id 3AC6
    Opponent-side cards often appeared as:
        00003CFC -> real card id 3CFC

    For CSV lookup, strip the 0x80000000 flag.
    """
    return card_id & 0x7FFFFFFF


def load_card_csv(path: Path) -> dict[int, CardInfo]:
    cards: dict[int, CardInfo] = {}

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            raw_id = (
                row.get("card_id_hex")
                or row.get("card_id")
                or row.get("id")
                or row.get("card_id_dec")
                or ""
            ).strip()

            if not raw_id:
                continue

            try:
                if raw_id.lower().startswith("0x"):
                    card_id = int(raw_id, 16)
                elif any(ch in raw_id for ch in "ABCDEFabcdef"):
                    card_id = int(raw_id, 16)
                else:
                    card_id = int(raw_id, 10)
            except ValueError:
                continue

            name = (row.get("name") or row.get("card_name") or "").strip()
            attack = str(row.get("attack") or row.get("power") or "").strip()
            shield = str(row.get("shield") or "").strip()

            cards[card_id & 0xFFFFFFFF] = CardInfo(
                card_id=card_id & 0xFFFFFFFF,
                name=name,
                attack=attack,
                shield=shield,
            )

    return cards


class ImageIndex:
    def __init__(self, images_dir: Path):
        self.images_dir = images_dir
        self.files: list[Path] = []
        self.norm_to_file: dict[str, Path] = {}

        if images_dir.exists():
            for p in images_dir.rglob("*"):
                if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                    self.files.append(p)
                    norm = normalize_name(p.stem)
                    if norm and norm not in self.norm_to_file:
                        self.norm_to_file[norm] = p

    def find(self, card_name: str) -> Optional[Path]:
        target = normalize_name(card_name)
        if not target:
            return None

        # exact normalized match
        if target in self.norm_to_file:
            return self.norm_to_file[target]

        # contains either way, e.g.
        # "Drive Quartet Ressac" vs "EB02_030EN__Drive_Quartet,_Ressac"
        for norm, p in self.norm_to_file.items():
            if target in norm or norm in target:
                return p

        # token subset matching
        tset = set(target.split())
        best: tuple[float, Optional[Path]] = (0.0, None)

        for norm, p in self.norm_to_file.items():
            nset = set(norm.split())
            if not nset:
                continue

            overlap = len(tset & nset) / max(1, len(tset | nset))
            ratio = SequenceMatcher(None, target, norm).ratio()
            score = max(overlap, ratio * 0.92)

            if score > best[0]:
                best = (score, p)

        # Conservative enough to avoid totally wrong images, loose enough for punctuation variants.
        if best[0] >= 0.72:
            return best[1]

        return None


class MemoryReader:
    def __init__(self) -> None:
        self.pm = pymem.Pymem(PROCESS_NAME)

    def read_u32(self, addr: int) -> int:
        return self.pm.read_uint(addr)

    def read_u64(self, addr: int) -> int:
        return self.pm.read_ulonglong(addr)

    def safe_u32(self, addr: int) -> Optional[int]:
        try:
            return self.read_u32(addr)
        except Exception:
            return None

    def safe_u64(self, addr: int) -> Optional[int]:
        try:
            return self.read_u64(addr)
        except Exception:
            return None


def zone_base_from_rbp(rbp: int, zone_index: int) -> int:
    return rbp + zone_index * ZONE_SIZE


def zone_count_addr(rbp: int, zone_index: int) -> int:
    return zone_base_from_rbp(rbp, zone_index) + COUNT_OFF


def zone_array_holder_addr(rbp: int, zone_index: int) -> int:
    return zone_base_from_rbp(rbp, zone_index) + ARRAY_HOLDER_OFF


def card_display(card_id: int, db: dict[int, CardInfo]) -> tuple[str, str]:
    full_id = card_id & 0xFFFFFFFF
    real_id = normalize_card_id(full_id)
    info = db.get(real_id)

    id_text = f"0x{real_id:04X}"
    if full_id != real_id:
        id_text = f"0x{full_id:08X} -> 0x{real_id:04X}"

    if not info:
        return "<unknown>", f"{id_text}    <unknown>"

    stats = []
    if info.attack:
        stats.append(f"ATK {info.attack}")
    if info.shield:
        stats.append(f"SH {info.shield}")

    stat_text = f" ({', '.join(stats)})" if stats else ""
    return info.name, f"{id_text}    {info.name}{stat_text}"

def read_zone(
    mr: MemoryReader,
    rbp: int,
    owner: str,
    zone_name: str,
    zone_index: int,
    db: dict[int, CardInfo],
    max_cards: int,
) -> tuple[int | None, int | None, list[CardRow]]:
    count_addr = zone_count_addr(rbp, zone_index)
    array_holder = zone_array_holder_addr(rbp, zone_index)

    count = mr.safe_u32(count_addr)
    array_base = mr.safe_u64(array_holder)

    if count is None or array_base is None:
        return count, array_base, []

    out: list[CardRow] = []
    safe_count = max(0, min(count, max_cards))

    for i in range(safe_count):
        slot_addr = array_base + i * 8
        card_obj = mr.safe_u64(slot_addr)

        if not card_obj:
            out.append(CardRow(owner, zone_name, i, 0, 0, "<null>", ""))
            continue

        card_id = mr.safe_u32(card_obj + CARD_OBJECT_ID_OFFSET)

        if card_id is None:
            out.append(CardRow(owner, zone_name, i, card_obj, 0, "<failed card id read>", ""))
            continue

        name, label = card_display(card_id, db)
        out.append(CardRow(owner, zone_name, i, card_obj, card_id, label, name))

    return count, array_base, out


class App:
    def __init__(self, root: tk.Tk, csv_path: Path, images_dir: Path, opp_rbp: int, my_rbp: int):
        self.root = root
        self.root.title("VGDD2 Dual RBP Reader + Images")
        self.root.geometry("1380x850")

        self.csv_path = csv_path
        self.images_dir = images_dir
        self.card_db = load_card_csv(csv_path)
        self.image_index = ImageIndex(images_dir)

        self.mr: Optional[MemoryReader] = None
        self.auto_refresh = tk.BooleanVar(value=True)

        self.opp_rbp_var = tk.StringVar(value=f"{opp_rbp:X}" if opp_rbp else "")
        self.my_rbp_var = tk.StringVar(value=f"{my_rbp:X}" if my_rbp else "")

        self.opp_rbp = opp_rbp
        self.my_rbp = my_rbp

        self.row_by_iid: dict[str, CardRow] = {}
        self.current_photo = None

        self.build_ui()
        self.connect()

    def build_ui(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill=tk.X)

        self.status = tk.StringVar(value="Not connected")
        ttk.Label(top, textvariable=self.status).pack(side=tk.LEFT)

        ttk.Button(top, text="Reconnect", command=self.connect).pack(side=tk.RIGHT, padx=4)
        ttk.Button(top, text="Refresh now", command=self.refresh).pack(side=tk.RIGHT, padx=4)
        ttk.Checkbutton(top, text="Auto-refresh", variable=self.auto_refresh).pack(side=tk.RIGHT, padx=10)

        rbp_frame = ttk.LabelFrame(self.root, text="Paste RBP values from `dec [rbp+0C]`", padding=8)
        rbp_frame.pack(fill=tk.X, padx=8, pady=(0, 8))

        ttk.Label(rbp_frame, text="Opponent RBP:").grid(row=0, column=0, sticky="w")
        opp_entry = ttk.Entry(rbp_frame, textvariable=self.opp_rbp_var, width=30)
        opp_entry.grid(row=0, column=1, padx=6)
        opp_entry.bind("<Return>", lambda _e: self.apply_rbps())

        ttk.Label(rbp_frame, text="My RBP:").grid(row=0, column=2, sticky="w", padx=(18, 0))
        my_entry = ttk.Entry(rbp_frame, textvariable=self.my_rbp_var, width=30)
        my_entry.grid(row=0, column=3, padx=6)
        my_entry.bind("<Return>", lambda _e: self.apply_rbps())

        ttk.Button(rbp_frame, text="Apply RBP(s)", command=self.apply_rbps).grid(row=0, column=4, padx=8)

        self.addr_label = tk.StringVar(value="")
        ttk.Label(rbp_frame, textvariable=self.addr_label).grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))

        body = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=3)
        body.add(right, weight=1)

        self.tree = ttk.Treeview(
            left,
            columns=("owner", "zone", "idx", "id", "name", "obj"),
            show="headings",
            height=30,
        )
        self.tree.heading("owner", text="Owner")
        self.tree.heading("zone", text="Zone")
        self.tree.heading("idx", text="#")
        self.tree.heading("id", text="ID")
        self.tree.heading("name", text="Name / stats")
        self.tree.heading("obj", text="Object ptr")

        self.tree.column("owner", width=90, anchor="w")
        self.tree.column("zone", width=70, anchor="w")
        self.tree.column("idx", width=35, anchor="center")
        self.tree.column("id", width=80, anchor="w")
        self.tree.column("name", width=520, anchor="w")
        self.tree.column("obj", width=150, anchor="w")

        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<<TreeviewSelect>>", self.on_select_row)

        self.info_label = tk.StringVar(value="Click a card to preview image")
        ttk.Label(right, textvariable=self.info_label, wraplength=330).pack(anchor="w", pady=(0, 8))

        self.image_label = ttk.Label(right, text="No image selected", anchor="center")
        self.image_label.pack(fill=tk.BOTH, expand=True)

        self.update_addr_label()
        self.root.after(1000, self.refresh_loop)

    def connect(self):
        try:
            self.mr = MemoryReader()
            self.status.set(
                f"Connected to {PROCESS_NAME}. Loaded {len(self.card_db)} card IDs; indexed {len(self.image_index.files)} images."
            )
            self.refresh()
        except Exception as e:
            self.mr = None
            self.status.set("Connection failed")
            messagebox.showerror("Failed to connect", str(e))

    def apply_rbps(self):
        try:
            opp_text = self.opp_rbp_var.get().strip()
            my_text = self.my_rbp_var.get().strip()

            self.opp_rbp = parse_hex_or_dec(opp_text) if opp_text else 0
            self.my_rbp = parse_hex_or_dec(my_text) if my_text else 0

            self.opp_rbp_var.set(f"{self.opp_rbp:X}" if self.opp_rbp else "")
            self.my_rbp_var.set(f"{self.my_rbp:X}" if self.my_rbp else "")

            self.update_addr_label()
            self.refresh()
        except Exception as e:
            messagebox.showerror("Invalid RBP", str(e))

    def zone_addr_summary(self, label: str, rbp: int) -> str:
        if not rbp:
            return f"{label}: <not set>"
        return (
            f"{label}: deck count=0x{zone_count_addr(rbp, 0):X}, deck array=0x{zone_array_holder_addr(rbp, 0):X}, "
            f"hand count=0x{zone_count_addr(rbp, 1):X}, hand array=0x{zone_array_holder_addr(rbp, 1):X}"
        )

    def update_addr_label(self):
        self.addr_label.set(
            self.zone_addr_summary("Opponent", self.opp_rbp)
            + "    |    "
            + self.zone_addr_summary("Me", self.my_rbp)
        )

    def refresh(self):
        if self.mr is None:
            return

        selected = self.tree.selection()
        selected_key = selected[0] if selected else None

        self.tree.delete(*self.tree.get_children())
        self.row_by_iid.clear()

        all_rows: list[CardRow] = []

        for owner, rbp in [("Opponent", self.opp_rbp), ("Me", self.my_rbp)]:
            if not rbp:
                continue

            for zone_name, zone_index, max_read in [
                ("Deck", DECK_ZONE_INDEX, MAX_DECK_READ),
                ("Hand", HAND_ZONE_INDEX, MAX_HAND_READ),
            ]:
                count, arr, rows = read_zone(
                    self.mr,
                    rbp,
                    owner,
                    zone_name,
                    zone_index,
                    self.card_db,
                    max_read,
                )

                header = CardRow(
                    owner=owner,
                    zone=zone_name,
                    index=-1,
                    obj=0,
                    card_id=0,
                    label=f"count={count} array=0x{arr or 0:X}",
                    name="",
                )
                all_rows.append(header)
                all_rows.extend(rows)

        for n, row in enumerate(all_rows):
            if row.index == -1:
                iid = f"header_{n}"
                self.tree.insert(
                    "",
                    "end",
                    iid=iid,
                    values=(row.owner, row.zone, "", "", row.label, ""),
                )
                continue

            iid = f"row_{n}_{row.owner}_{row.zone}_{row.index}_{row.card_id:X}_{row.obj:X}"
            self.row_by_iid[iid] = row
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    row.owner,
                    row.zone,
                    row.index,
                    (f"0x{row.card_id:08X}->0x{normalize_card_id(row.card_id):04X}" if row.card_id != normalize_card_id(row.card_id) else f"0x{row.card_id:04X}"),
                    row.label,
                    f"0x{row.obj:X}" if row.obj else "",
                ),
            )

        if selected_key and selected_key in self.row_by_iid:
            self.tree.selection_set(selected_key)

    def on_select_row(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return

        row = self.row_by_iid.get(sel[0])
        if not row:
            return

        card_name = row.name
        shown_id = f"0x{row.card_id:08X} -> 0x{normalize_card_id(row.card_id):04X}" if row.card_id != normalize_card_id(row.card_id) else f"0x{row.card_id:04X}"
        self.info_label.set(f"{row.owner} {row.zone}[{row.index}]  id={shown_id}\n{card_name}")

        img_path = self.image_index.find(card_name)

        if img_path is None:
            self.current_photo = None
            self.image_label.configure(text=f"Image not found\n{card_name}", image="")
            return

        try:
            if PIL_AVAILABLE:
                img = Image.open(img_path)
                img.thumbnail((360, 520))
                self.current_photo = ImageTk.PhotoImage(img)
                self.image_label.configure(image=self.current_photo, text="")
            else:
                # Tk PhotoImage supports PNG on modern Tk, but no nice resize.
                self.current_photo = tk.PhotoImage(file=str(img_path))
                self.image_label.configure(image=self.current_photo, text="")
        except Exception as e:
            self.current_photo = None
            self.image_label.configure(text=f"Failed to load image:\n{img_path}\n{e}", image="")

    def refresh_loop(self):
        if self.auto_refresh.get():
            try:
                self.refresh()
            except Exception:
                pass
        self.root.after(1000, self.refresh_loop)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cards", type=Path, default=Path("vgdd2_cards_dump.csv"))
    parser.add_argument("--images", type=Path, default=Path("vanguard_scrape_out/images"))
    parser.add_argument("--opp-rbp", type=str, default=f"{DEFAULT_OPP_RBP:X}")
    parser.add_argument("--my-rbp", type=str, default="")
    args = parser.parse_args()

    opp_rbp = parse_hex_or_dec(args.opp_rbp) if args.opp_rbp else 0
    my_rbp = parse_hex_or_dec(args.my_rbp) if args.my_rbp else 0

    root = tk.Tk()
    App(root, args.cards, args.images, opp_rbp, my_rbp)
    root.mainloop()


if __name__ == "__main__":
    main()
