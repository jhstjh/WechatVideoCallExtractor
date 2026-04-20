"""WeChat Call Record Extractor

Scans a WeChat Windows desktop chat from bottom to top, locates
voice / video call entries via OCR, pairs each with its timestamp,
and writes structured records to a SQLite database.

Usage:
    python scraper.py [--db wechat_calls.db] [--wait 1.0] [--max-pages 500]
"""

import ctypes
import os
import sys
import time
import re
import sqlite3
import hashlib
import argparse

import numpy as np
import pyautogui
import win32gui
import win32ui
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

# ===================== configuration =====================

COUNTDOWN_S = 5

CALL_KEYWORDS = [
    "通话时长", "通话中断",
    "Duration", "Call ended", "Call interrupted",
]

CHAT_AREA_LEFT_RATIO = 0.30
SCROLL_DOWN_CLICKS = 60

DB_FILE = "wechat_calls.db"

ctypes.windll.user32.SetProcessDPIAware()


def capture_window(hwnd) -> Image.Image:
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    hdc = win32gui.GetWindowDC(hwnd)
    src_dc = win32ui.CreateDCFromHandle(hdc)
    mem_dc = src_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(src_dc, w, h)
    mem_dc.SelectObject(bmp)
    ctypes.windll.user32.PrintWindow(hwnd, mem_dc.GetSafeHdc(), 2)

    info = bmp.GetInfo()
    bits = bmp.GetBitmapBits(True)
    img = Image.frombuffer(
        "RGB", (info["bmWidth"], info["bmHeight"]),
        bits, "raw", "BGRX", 0, 1,
    )

    win32gui.DeleteObject(bmp.GetHandle())
    mem_dc.DeleteDC()
    src_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hdc)
    return img


# ===================== bbox helpers =====================

def _y_min(b): return min(p[1] for p in b)
def _y_max(b): return max(p[1] for p in b)
def _x_min(b): return min(p[0] for p in b)
def _x_max(b): return max(p[0] for p in b)
def _cx(b):    return (_x_min(b) + _x_max(b)) / 2
def _cy(b):    return (_y_min(b) + _y_max(b)) / 2


# ===================== text classification =====================

def is_call_entry(text: str) -> bool:
    s = text.strip()
    return any(kw in s for kw in CALL_KEYWORDS)


_TS_RE = re.compile(r"([01]?\d|2[0-3]):[0-5]\d")
_MONTH_RE = re.compile(r"(\d{1,2})月")
_DATE_PREFIX_RE = re.compile(
    r"(\d{1,2}月\d{1,2}日"          # 3月15日
    r"|今天|昨天|前天"               # relative dates
    r"|星期[一二三四五六天日]"        # 星期X
    r"|周[一二三四五六日天])"         # 周X
)


def extract_month(text: str):
    """Return the month number from a timestamp string, or None."""
    m = _MONTH_RE.search(text)
    return int(m.group(1)) if m else None


def looks_like_timestamp(text: str) -> bool:
    """A real WeChat session-timestamp must have BOTH a date marker
    (e.g. '3月15日', '今天', '星期五') and an HH:MM time within 00:00-23:59."""
    s = text.strip()
    if is_call_entry(s):
        return False
    if len(s) > 40:
        return False
    if not _DATE_PREFIX_RE.search(s):
        return False
    return bool(_TS_RE.search(s))


def find_timestamp_above(ocr_hits, target_bbox):
    """Return (bbox, text) of the nearest timestamp above *target_bbox*."""
    ceiling = _y_min(target_bbox)
    best = None
    for bbox, txt, _ in ocr_hits:
        if _y_max(bbox) < ceiling and looks_like_timestamp(txt):
            if best is None or _y_max(bbox) > _y_max(best[0]):
                best = (bbox, txt)
    return best if best else (None, None)


_DUR_HMS = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})")
_DUR_MS = re.compile(r"(\d{1,2}):(\d{2})")


def parse_duration(text: str):
    """Return (duration_str, total_seconds) or (None, None)."""
    m = _DUR_HMS.search(text)
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return m.group(0), h * 3600 + mi * 60 + s
    m = _DUR_MS.search(text)
    if m:
        mi, s = int(m.group(1)), int(m.group(2))
        return m.group(0), mi * 60 + s
    return None, None


def extract_status(text: str) -> str:
    for kw in CALL_KEYWORDS:
        if kw in text:
            return kw
    return text.strip()


def who_called(bbox, frame_w: int) -> str:
    """'me' if the entry is on the right side of the chat area, else 'them'."""
    chat_mid = frame_w * 0.65
    return "me" if _cx(bbox) > chat_mid else "them"


# ===================== database =====================

def open_db(path: str):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calls (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp        TEXT    NOT NULL,
            caller           TEXT    NOT NULL,
            status           TEXT    NOT NULL,
            duration         TEXT,
            duration_seconds INTEGER,
            created_at       TEXT    DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_dedup
        ON calls(timestamp, caller, status, COALESCE(duration, ''))
    """)
    conn.commit()
    return conn


def store(conn, ts, caller, status, dur, dur_s):
    cur = conn.execute(
        "INSERT OR IGNORE INTO calls "
        "(timestamp, caller, status, duration, duration_seconds) "
        "VALUES (?,?,?,?,?)",
        (ts, caller, status, dur, dur_s),
    )
    conn.commit()
    return cur.rowcount > 0


# ===================== main loop =====================

def main():
    ap = argparse.ArgumentParser(description="WeChat call-record extractor")
    ap.add_argument("--db",        default=None, help="SQLite output path (auto-named if --month set)")
    ap.add_argument("--max-pages", type=int,   default=500,
                    help="Safety limit on number of PageUp presses")
    ap.add_argument("--month",     type=int, default=None,
                    help="Only capture entries from this month (1-12). "
                         "Stops when a timestamp outside this month is seen.")
    ap.add_argument("--debug",     action="store_true",
                    help="Print all OCR lines per frame for diagnostics")
    args = ap.parse_args()

    if args.db is None:
        if args.month:
            args.db = f"wechat_calls_m{args.month:02d}.db"
        else:
            args.db = DB_FILE

    # ── countdown — user focuses the WeChat chat window ──
    print("Focus the WeChat chat window now...")
    for i in range(COUNTDOWN_S, 0, -1):
        print(f"  {i}...")
        time.sleep(1)
    print("Starting!\n")

    hwnd = win32gui.GetForegroundWindow()
    title = win32gui.GetWindowText(hwnd)
    if not title:
        sys.exit("ERROR: could not detect foreground window")
    print(f"Using window: \"{title}\"  hwnd={hwnd}")

    l, t, r, b = win32gui.GetWindowRect(hwnd)
    cx, cy = (l + r) // 2, (t + b) // 2

    # ── initialise OCR, DB ──
    print("Loading RapidOCR ...")
    ocr = RapidOCR()

    conn = open_db(args.db)
    print(f"Database: {args.db}")

    seen_hashes = set()
    seen_entries = set()
    pending_calls = []
    total = [0]   # boxed in a list so the inner closure can update it

    def ts_sort_key(ts_text):
        """Parse 'X月Y日[星期Z]HH:MM' into a tuple for chronological compare."""
        m = re.search(
            r"(\d{1,2})月(\d{1,2})日"
            r"(?:星期[一二三四五六天日]|周[一二三四五六日天])?"
            r"(\d{1,2}):(\d{2})",
            ts_text,
        )
        if m:
            return (int(m.group(1)), int(m.group(2)),
                    int(m.group(3)), int(m.group(4)))
        return (0, 0, 0, 0)

    def resolve_pending(candidates):
        """Resolve pending calls using a list of candidate timestamps from the
        current PageUp+scroll cycle's two frames. For each pending entry, only
        candidates that are chronologically OLDER than the entry's recorded
        ceiling are considered; among those, the chronologically latest is
        chosen. Entries that have no valid candidate this cycle remain pending."""
        nonlocal pending_calls
        if not pending_calls or not candidates:
            return
        # Dedupe candidates while preserving the strings
        cand_unique = list({c.strip() for c in candidates if c})
        remaining = []
        for caller, status, dur_str, dur_s, ceiling in pending_calls:
            if ceiling is None:
                valid = cand_unique
            else:
                ceil_key = ts_sort_key(ceiling)
                valid = [c for c in cand_unique if ts_sort_key(c) < ceil_key]
            if not valid:
                remaining.append((caller, status, dur_str, dur_s, ceiling))
                if args.debug:
                    print(f"  >> pending ({caller},{status},{dur_str}) ceiling={ceiling!r}: "
                          f"no candidate older than ceiling — keeping for next cycle")
                continue
            chosen = max(valid, key=ts_sort_key)
            if args.month:
                tsm = extract_month(chosen)
                if tsm is not None and tsm != args.month:
                    continue
            key = (chosen, caller, status, dur_str or "")
            if key in seen_entries:
                continue
            seen_entries.add(key)
            if store(conn, chosen, caller, status, dur_str, dur_s):
                total[0] += 1
                d = dur_str or "--"
                print(f"[FOUND] {chosen} | {caller:>4} | {status} | {d}  "
                      f"(resolved pending; ceiling was {ceiling!r})")
        pending_calls = remaining

    def process_frame(label):
        """OCR + extract from one captured frame.
        Returns (status, ts_strings_in_frame) where status is one of
        'ok', 'duplicate', 'no_ocr', 'out_of_month'."""
        nonlocal pending_calls
        img = capture_window(hwnd)
        img_np = np.array(img)
        fhash = hashlib.md5(img_np.tobytes()).hexdigest()
        if fhash in seen_hashes:
            return "duplicate", []
        seen_hashes.add(fhash)

        results, _ = ocr(img_np)
        if results is None:
            return "no_ocr", []

        frame_w = img.width
        min_x = frame_w * CHAT_AREA_LEFT_RATIO
        results = [r for r in results if _x_min(r[0]) >= min_x]

        if args.debug:
            print(f"--- {label} : {len(results)} OCR lines ---")
            for bbox, text, conf in sorted(results, key=lambda r: _cy(r[0])):
                tag = ""
                if is_call_entry(text):
                    tag = " <<CALL>>"
                elif looks_like_timestamp(text):
                    tag = " <<TS>>"
                print(f"  y={_cy(bbox):6.0f}  x={_x_min(bbox):6.0f}  "
                      f"conf={conf}  \"{text.strip()}\"{tag}")

        ts_strings_in_frame = [txt.strip() for _, txt, _ in results
                               if looks_like_timestamp(txt)]

        if args.month:
            out_of_month = [t for t in ts_strings_in_frame
                            if extract_month(t) is not None and extract_month(t) != args.month]
            if out_of_month and not any(
                extract_month(t) == args.month for t in ts_strings_in_frame
            ):
                return "out_of_month", ts_strings_in_frame

        # Ceiling for any call queued from THIS frame: the chronologically
        # OLDEST visible TS. Since find_timestamp_above will return None for
        # such calls, every visible TS in the frame is BELOW the call (i.e.
        # newer); the oldest is the strictest upper bound on the call's true
        # session timestamp.
        frame_ceiling = (min(ts_strings_in_frame, key=ts_sort_key)
                         if ts_strings_in_frame else None)

        # NOTE: pending-call resolution is deferred — the main loop combines
        # the bottom_ts of the post-pageup and post-scroll frames and resolves
        # pending using the chronologically later one.

        for bbox, text, conf in sorted(results, key=lambda r: _cy(r[0]),
                                       reverse=True):
            if not is_call_entry(text):
                continue

            caller = who_called(bbox, frame_w)

            ts_bbox, ts_text = find_timestamp_above(results, bbox)
            if ts_text is None:
                status = extract_status(text)
                dur_str, dur_s = parse_duration(text)
                key_partial = (caller, status, dur_str or "")
                if key_partial not in [(c, s, d or "") for c, s, d, _, _ in pending_calls]:
                    pending_calls.append((caller, status, dur_str, dur_s, frame_ceiling))
                    if args.debug:
                        print(f"  >> CALL at y={_cy(bbox):.0f}: \"{text.strip()}\" — no timestamp, "
                              f"queued as pending (ceiling={frame_ceiling!r})")
                continue

            if args.month:
                ts_month = extract_month(ts_text)
                if ts_month is not None and ts_month != args.month:
                    continue

            status = extract_status(text)
            dur_str, dur_s = parse_duration(text)

            key = (ts_text.strip(), caller, status, dur_str or "")
            if key in seen_entries:
                d = dur_str or "--"
                print(f"[SKIP]  {ts_text.strip()} | {caller:>4} | {status} | {d}  (already seen)")
                continue
            seen_entries.add(key)

            if store(conn, ts_text.strip(), caller, status, dur_str, dur_s):
                total[0] += 1
                d = dur_str or "--"
                print(f"[FOUND] {ts_text.strip()} | {caller:>4} | {status} | {d}")
            else:
                d = dur_str or "--"
                print(f"[SKIP]  {ts_text.strip()} | {caller:>4} | {status} | {d}  (already in DB)")

        return "ok", ts_strings_in_frame

    print("\n-- scanning (bottom -> top) --\n")

    # Initial frame (whatever the user has on screen). Pending calls created
    # here will be resolved in the first PageUp+scroll cycle below.
    r, _ = process_frame("initial")
    if r == "out_of_month":
        print(f"\nAll timestamps in this frame are outside month {args.month}. Done!")
    else:
        stop = False
        for page in range(args.max_pages):
            # 1. Page up, OCR the new view → collect TSes (set A)
            pyautogui.press("pageup")
            r1, ts_list_a = process_frame(f"page {page} post-pageup")
            if r1 == "duplicate":
                print("\nReached top of chat (duplicate frame). Done!")
                break
            if r1 == "out_of_month":
                print(f"\nAll timestamps in this frame are outside month {args.month}. Done!")
                stop = True

            # 2. Scroll down 60, OCR again → collect TSes (set B). Catches
            #    timestamps that may have been clipped at the bottom of the
            #    post-pageup view.
            ts_list_b = []
            if not stop:
                pyautogui.moveTo(cx, cy)
                pyautogui.scroll(-SCROLL_DOWN_CLICKS)
                r2, ts_list_b = process_frame(f"page {page} post-scroll")
                if r2 == "out_of_month":
                    print(f"\nAll timestamps in this frame are outside month {args.month}. Done!")
                    stop = True

            # 3. Resolve pending using the union of TSes from both frames.
            #    For each pending entry, only TSes chronologically older than
            #    its recorded ceiling are considered; among those the latest
            #    is chosen.
            candidates = list(ts_list_a) + list(ts_list_b)
            if pending_calls and candidates:
                if args.debug:
                    print(f"  >> trying to resolve {len(pending_calls)} pending call(s); "
                          f"candidates={sorted(set(candidates), key=ts_sort_key)}")
                resolve_pending(candidates)

            if stop:
                break
        else:
            print(f"\nStopped after {args.max_pages} pages (use --max-pages to increase)")

    total = total[0]

    conn.close()
    print(f"\nTotal records: {total}")
    print(f"Database: {os.path.abspath(args.db)}")


if __name__ == "__main__":
    main()
