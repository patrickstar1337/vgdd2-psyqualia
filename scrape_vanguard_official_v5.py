"""
Vanguard official scraper v5 - loose name matching

Fixes the "cards=0" issue by NOT relying on <a>.innerText.
Instead it parses the whole visible page/body text using card-number regex.

Install:
    pip install playwright beautifulsoup4 requests pandas
    python -m playwright install chromium

Run:
    python scrape_vanguard_official_v5.py --memory-dump vgdd2_cards_dump.csv

Debug one page:
    python scrape_vanguard_official_v5.py --url "https://en.cf-vanguard.com/cardlist/cardsearch/?expansion=5" --debug --headed

Outputs:
    vanguard_scrape_out/cards_official_scrape.csv
    vanguard_scrape_out/images/*.png
"""

from __future__ import annotations

import argparse
import csv
import re
import time
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


DEFAULT_URLS = [
    "https://en.cf-vanguard.com/cardlist/cardsearch/?expansion=5",
    "https://en.cf-vanguard.com/cardlist/cardsearch/?expansion=6",
    "https://en.cf-vanguard.com/cardlist/cardsearch/?expansion=12",
    "https://en.cf-vanguard.com/cardlist/cardsearch/?expansion=15",
    "https://en.cf-vanguard.com/cardlist/cardsearch/?expansion=18",
    "https://en.cf-vanguard.com/cardlist/cardsearch/?expansion=19",
    "https://en.cf-vanguard.com/cardlist/cardsearch/?expansion=14",
]

OUT_DIR = Path("vanguard_scrape_out")
IMG_DIR = OUT_DIR / "images"
CSV_PATH = OUT_DIR / "cards_official_scrape.csv"

CARD_TYPE_PATTERN = (
    r"(?:Normal Unit|Trigger Unit|G Unit|Token Unit|Normal Order|Blitz Order|Set Order|Order|Crest)"
)

CARD_NO_PATTERN = r"[A-Z0-9][A-Z0-9\-_]*/[A-Z0-9\-_]+EN"


@dataclass
class Card:
    expansion_id: str
    expansion_title: str
    card_number: str
    name: str
    card_type: str
    clan_or_nation: str
    grade: str
    power: str
    shield: str
    skill: str
    detail_url: str
    image_url: str
    image_file: str = ""


def with_list_detail_view(url: str) -> str:
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    q["view"] = ["text"]
    q["sort"] = ["no"]
    query = urlencode({k: v[-1] for k, v in q.items()})
    return urlunparse(parsed._replace(query=query))


def expansion_id_from_url(url: str) -> str:
    return parse_qs(urlparse(url).query).get("expansion", [""])[0]


def clean_text(s: str) -> str:
    s = s.replace("\xa0", " ")
    s = s.replace("\u3000", " ")
    return re.sub(r"\s+", " ", s).strip()


def safe_filename(s: str, max_len: int = 170) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r'[\\/:*?"<>|]+', "", s)
    s = re.sub(r"\s+", "_", s.strip())
    s = re.sub(r"_+", "_", s)
    return s[:max_len].strip("_") or "card"


def extract_expected_count(text: str) -> int | None:
    m = re.search(r"(\d+)\s+Results", text)
    return int(m.group(1)) if m else None


def extract_expansion_title(soup: BeautifulSoup) -> str:
    h3 = soup.find("h3")
    if h3:
        return clean_text(h3.get_text(" "))
    text = clean_text(soup.get_text(" "))
    m = re.search(r"Card List > ([^\n]+?) \d+ Results", text)
    return clean_text(m.group(1)) if m else ""


def split_card_entries_from_text(text: str) -> list[str]:
    """
    Finds every card-number occurrence and slices until the next card number.
    Works on body text, not anchor text.
    """
    text = clean_text(text)

    # Remove noisy UI labels before first card.
    matches = list(re.finditer(CARD_NO_PATTERN, text))
    entries: list[str] = []

    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = clean_text(text[start:end])

        # Cut footer if this is the last chunk.
        for stopper in ["FOR BUSINESS", "MEDIA KIT", "SUPPORT", "Privacy Policy", "FIND US ON"]:
            pos = chunk.find(stopper)
            if pos != -1:
                chunk = clean_text(chunk[:pos])
        entries.append(chunk)

    return entries


def parse_card_entry(entry: str) -> dict | None:
    """
    Robust parse for chunks like:
    BT01/001EN King of Knights, Alfred Normal Unit｜ Royal Paladin｜ Grade 3｜ Power 10000｜ Shield - [CONT]...

    v3 failed because the site often has no whitespace before/after the full-width separator:
        Normal Unit｜ Royal Paladin｜ ...
    """
    entry = clean_text(entry)

    # 1) Card number
    m_no = re.match(rf"^(?P<number>{CARD_NO_PATTERN})\s+(?P<rest>.+)$", entry)
    if not m_no:
        return None

    card_number = m_no.group("number")
    rest = m_no.group("rest")

    # 2) Find the first card type occurrence. This separates name from type.
    type_matches = list(re.finditer(CARD_TYPE_PATTERN, rest))
    if not type_matches:
        return None

    type_match = type_matches[0]
    name = clean_text(rest[:type_match.start()])
    card_type = type_match.group(0)
    after_type = clean_text(rest[type_match.end():])

    # 3) Strip leading separators/spaces.
    after_type = re.sub(r"^[\s｜|]+", "", after_type)

    # 4) Split into clan, grade, power, shield+skill.
    # Works even when there are no spaces around ｜.
    parts = [clean_text(p) for p in re.split(r"[｜|]", after_type)]
    if len(parts) < 4:
        return None

    clan_or_nation = parts[0]
    grade_part = parts[1]
    power_part = parts[2]
    shield_and_skill = clean_text("｜".join(parts[3:]))

    mg = re.search(r"Grade\s+(-?\d+)", grade_part)
    mp = re.search(r"Power\s+(-|\d+)", power_part)
    ms = re.search(r"Shield\s+(-|\d+)(?:\s*(.*))?$", shield_and_skill)

    if not (mg and mp and ms):
        return None

    grade = mg.group(1)
    power = mp.group(1)
    shield = ms.group(1)
    skill = clean_text(ms.group(2) or "")

    return {
        "card_number": card_number,
        "name": name,
        "card_type": card_type,
        "clan_or_nation": clan_or_nation,
        "grade": grade,
        "power": power,
        "shield": shield,
        "skill": skill,
    }


def image_url_candidates(card_number: str) -> list[str]:
    prefix = card_number.split("/")[0]
    filename = card_number.replace("/", "_") + ".png"

    folders = [
        prefix.lower(),
        prefix.lower().replace("-", ""),
        prefix.lower().replace("_", ""),
        prefix.split("-")[-1].lower(),
    ]

    out = []
    seen = set()
    for folder in folders:
        if not folder or folder in seen:
            continue
        seen.add(folder)
        out.append(f"https://en.cf-vanguard.com/wordpress/wp-content/images/cardlist/{folder}/{filename}")
    return out


def try_download_image(card: Card, session: requests.Session, img_dir: Path) -> str:
    filename = f"{safe_filename(card.card_number.replace('/', '_'))}__{safe_filename(card.name)}.png"
    path = img_dir / filename
    if path.exists() and path.stat().st_size > 1000:
        return str(path)

    for url in image_url_candidates(card.card_number):
        try:
            r = session.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200 and len(r.content) > 1000 and (
                "image" in r.headers.get("content-type", "") or r.content[:8].startswith(b"\x89PNG")
            ):
                path.write_bytes(r.content)
                card.image_url = url
                return str(path)
        except Exception:
            pass
    return ""


def scrape_expansion(page, url: str, debug: bool = False) -> list[Card]:
    url = with_list_detail_view(url)
    expansion_id = expansion_id_from_url(url)
    print(f"[LOAD] {url}")

    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)

    # Try to ensure text/list detail mode if the page has JS toggles.
    try:
        page.get_by_text("List Detail View", exact=False).click(timeout=1500)
        page.wait_for_timeout(1200)
    except Exception:
        pass

    # Scroll several times in case there is lazy rendering.
    last_text_len = 0
    stable = 0
    for _ in range(12):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(700)
        body_text = page.locator("body").inner_text(timeout=10000)
        if len(body_text) == last_text_len:
            stable += 1
        else:
            stable = 0
        last_text_len = len(body_text)
        if stable >= 3:
            break

    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    body_text = page.locator("body").inner_text(timeout=10000)

    expected = extract_expected_count(body_text)
    expansion_title = extract_expansion_title(soup)

    entries = split_card_entries_from_text(body_text)
    cards: dict[str, Card] = {}

    if debug:
        OUT_DIR.mkdir(exist_ok=True)
        (OUT_DIR / f"debug_expansion_{expansion_id}.txt").write_text(body_text, encoding="utf-8")
        print(f"[DEBUG] body text chars={len(body_text)}, card-number chunks={len(entries)}")
        print(f"[DEBUG] wrote {OUT_DIR / f'debug_expansion_{expansion_id}.txt'}")
        for e in entries[:5]:
            print("[DEBUG ENTRY]", e[:250], "...")

    for entry in entries:
        parsed = parse_card_entry(entry)
        if not parsed:
            if debug and re.match(CARD_NO_PATTERN, entry):
                print("[PARSE FAIL]", entry[:350])
            continue

        card_no = parsed["card_number"]
        detail_url = f"https://en.cf-vanguard.com/cardlist/cardsearch/?cardno={card_no}&expansion={expansion_id}&view=text"

        card = Card(
            expansion_id=expansion_id,
            expansion_title=expansion_title,
            card_number=card_no,
            name=parsed["name"],
            card_type=parsed["card_type"],
            clan_or_nation=parsed["clan_or_nation"],
            grade=parsed["grade"],
            power=parsed["power"],
            shield=parsed["shield"],
            skill=parsed["skill"],
            detail_url=detail_url,
            image_url=image_url_candidates(card_no)[0] if image_url_candidates(card_no) else "",
        )
        cards[card_no] = card

    print(f"[SCRAPED] expansion={expansion_id}, cards={len(cards)}" + (f" / expected {expected}" if expected else ""))

    if expected and len(cards) < expected:
        print(
            "[WARN] Parsed fewer than expected. The site may only render the first batch in HTML. "
            "Still useful, but we may need a hidden API/pagination endpoint for full 92."
        )

    return list(cards.values())


def write_cards_csv(cards: list[Card], path: Path) -> None:
    path.parent.mkdir(exist_ok=True)
    fields = list(Card.__dataclass_fields__.keys())
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for c in cards:
            w.writerow(asdict(c))


def normalize_card_name(name: str) -> str:
    """
    Normalize names for loose matching between:
    - official scrape: King of Knights, Alfred
    - VGDD2 memory dump: King of Knights, Alfred

    Also removes accidental card-number prefixes if they appear.
    """
    if name is None:
        return ""

    s = str(name)
    s = unicodedata.normalize("NFKC", s)
    s = s.lower()

    # Remove card numbers if they somehow got included.
    s = re.sub(r"\b[A-Z0-9][A-Z0-9\-_]*/[A-Z0-9\-_]+EN\b", " ", s, flags=re.I)

    # Normalize smart punctuation / full-width punctuation.
    replacements = {
        "’": "'",
        "‘": "'",
        "“": '"',
        "”": '"',
        "，": ",",
        "　": " ",
    }
    for a, b in replacements.items():
        s = s.replace(a, b)

    # For matching, punctuation usually should not matter.
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def compare_memory_dump(official_cards: list[Card], memory_csv: Path) -> None:
    try:
        import pandas as pd
    except ImportError:
        print("[WARN] pandas not installed; skipping memory dump comparison.")
        return

    if not memory_csv.exists():
        print(f"[WARN] memory dump not found: {memory_csv}")
        return

    off = pd.DataFrame([asdict(c) for c in official_cards])
    mem = pd.read_csv(memory_csv)

    if off.empty:
        print("[WARN] official scrape empty; skipping comparison.")
        return

    off["name_norm"] = off["name"].fillna("").map(normalize_card_name)
    mem["name_norm"] = mem["name"].fillna("").map(normalize_card_name)

    # Official may have multiple variants with same name. Keep a grouped view.
    official_grouped = (
        off.groupby("name_norm", dropna=False)
        .agg(
            official_names=("name", lambda x: " | ".join(sorted(set(map(str, x))))),
            card_numbers=("card_number", lambda x: " | ".join(sorted(set(map(str, x))))),
            grades=("grade", lambda x: " | ".join(sorted(set(map(str, x))))),
            powers=("power", lambda x: " | ".join(sorted(set(map(str, x))))),
            shields=("shield", lambda x: " | ".join(sorted(set(map(str, x))))),
            image_files=("image_file", lambda x: " | ".join(sorted(set(str(v) for v in x if str(v) and str(v) != "nan")))),
        )
        .reset_index()
    )

    official_names = set(official_grouped["name_norm"])
    missing = mem[~mem["name_norm"].isin(official_names)].copy()

    matched = mem.merge(official_grouped, on="name_norm", how="left")
    matched_out = OUT_DIR / "memory_joined_to_official_by_loose_name.csv"
    missing_out = OUT_DIR / "memory_names_not_found_in_scrape.csv"
    official_dupes_out = OUT_DIR / "official_duplicate_names.csv"

    matched.drop(columns=["name_norm"], errors="ignore").to_csv(
        matched_out, index=False, encoding="utf-8-sig"
    )
    missing.drop(columns=["name_norm"], errors="ignore").to_csv(
        missing_out, index=False, encoding="utf-8-sig"
    )

    dupes = off[off.duplicated("name_norm", keep=False)].sort_values(["name_norm", "card_number"])
    dupes.drop(columns=["name_norm"], errors="ignore").to_csv(
        official_dupes_out, index=False, encoding="utf-8-sig"
    )

    print(f"[COMPARE] memory rows={len(mem)}, official rows={len(off)}, unique official names={len(official_names)}")
    print(f"[COMPARE] loose-name matched rows={len(mem) - len(missing)}, missing names={len(missing)}")
    print(f"[COMPARE] wrote {matched_out}")
    print(f"[COMPARE] wrote {missing_out}")
    print(f"[COMPARE] wrote {official_dupes_out}")

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", action="append", default=[])
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--memory-dump", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=CSV_PATH)
    args = ap.parse_args()

    OUT_DIR.mkdir(exist_ok=True)
    IMG_DIR.mkdir(exist_ok=True)

    urls = args.url or DEFAULT_URLS
    all_cards: dict[str, Card] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1400, "height": 1400})

        for url in urls:
            try:
                cards = scrape_expansion(page, url, debug=args.debug)
                for c in cards:
                    all_cards[c.card_number] = c
            except Exception as e:
                print(f"[ERROR] Failed scraping {url}: {e}")

        browser.close()

    cards = list(all_cards.values())
    print(f"[TOTAL] unique cards scraped: {len(cards)}")

    if not args.no_images:
        sess = requests.Session()
        for i, c in enumerate(cards, 1):
            c.image_file = try_download_image(c, sess, IMG_DIR)
            if i % 25 == 0:
                print(f"[IMAGES] {i}/{len(cards)}")
        print(f"[IMAGES] saved under {IMG_DIR}")

    write_cards_csv(cards, args.out)
    print(f"[DONE] wrote {args.out}")

    if args.memory_dump:
        compare_memory_dump(cards, args.memory_dump)


if __name__ == "__main__":
    main()
