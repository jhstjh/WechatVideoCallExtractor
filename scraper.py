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
SCROLL_DOWN_CLICKS = 50

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


_TS_RE = re.compile(r"\d{1,2}:\d{2}")
_MONTH_RE = re.compile(r"(\d{1,2})月")


def extract_month(text: str):
    """Return the month number from a timestamp string, or None."""
    m = _MONTH_RE.search(text)
    return int(m.group(1)) if m else None


def looks_like_timestamp(text: str) -> bool:
    s = text.strip()
    if is_call_entry(s):
        return False
    if len(s) > 40:
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
    total = 0

    print("\n-- scanning (bottom -> top) --\n")

    for page in range(args.max_pages):
        # ── capture frame ──
        img = capture_window(hwnd)
        img_np = np.array(img)
        fhash = hashlib.md5(img_np.tobytes()).hexdigest()

        if fhash in seen_hashes:
            print("\nReached top of chat (duplicate frame). Done!")
            break
        seen_hashes.add(fhash)

        # ── OCR ──
        results, _ = ocr(img_np)
        if results is None:
            pyautogui.press("pageup")
            pyautogui.moveTo(cx, cy)
            pyautogui.scroll(-SCROLL_DOWN_CLICKS)
            continue

        # filter out sidebar text (left portion of window)
        frame_w = img.width
        min_x = frame_w * CHAT_AREA_LEFT_RATIO
        results = [r for r in results if _x_min(r[0]) >= min_x]

        if args.debug:
            print(f"--- page {page} : {len(results)} OCR lines ---")
            for bbox, text, conf in sorted(results, key=lambda r: _cy(r[0])):
                tag = ""
                if is_call_entry(text):
                    tag = " <<CALL>>"
                elif looks_like_timestamp(text):
                    tag = " <<TS>>"
                print(f"  y={_cy(bbox):6.0f}  x={_x_min(bbox):6.0f}  "
                      f"conf={conf}  \"{text.strip()}\"{tag}")

        # ── month boundary check ──
        if args.month:
            ts_lines = [txt for _, txt, _ in results if looks_like_timestamp(txt)]
            out_of_month = [t for t in ts_lines
                            if extract_month(t) is not None and extract_month(t) != args.month]
            if out_of_month and not any(
                extract_month(t) == args.month for t in ts_lines
            ):
                print(f"\nAll timestamps in this frame are outside month {args.month}. Done!")
                break

        # ── resolve pending calls: bottom-most timestamp is the one ──
        if pending_calls:
            all_ts = sorted(
                [(bbox, txt) for bbox, txt, _ in results if looks_like_timestamp(txt)],
                key=lambda x: _cy(x[0]),
                reverse=True,
            )
            if all_ts:
                ts_text = all_ts[0][1]
                if args.month:
                    ts_month = extract_month(ts_text)
                    if ts_month is not None and ts_month != args.month:
                        pending_calls = []
                        continue
                for caller, status, dur_str, dur_s in pending_calls:
                    key = (ts_text.strip(), caller, status, dur_str or "")
                    if key not in seen_entries:
                        seen_entries.add(key)
                        if store(conn, ts_text.strip(), caller, status, dur_str, dur_s):
                            total += 1
                            d = dur_str or "--"
                            print(f"[FOUND] {ts_text.strip()} | {caller:>4} | {status} | {d}")
                pending_calls = []

        # ── process entries bottom -> top ──
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
                if key_partial not in [(c, s, d or "") for c, s, d, _ in pending_calls]:
                    pending_calls.append((caller, status, dur_str, dur_s))
                    if args.debug:
                        print(f"  >> CALL at y={_cy(bbox):.0f}: \"{text.strip()}\" — no timestamp, queued as pending")
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
                total += 1
                d = dur_str or "--"
                print(f"[FOUND] {ts_text.strip()} | {caller:>4} | {status} | {d}")
            else:
                d = dur_str or "--"
                print(f"[SKIP]  {ts_text.strip()} | {caller:>4} | {status} | {d}  (already in DB)")

        # ── scroll up one page, then nudge down to clear any clipped entry ──
        pyautogui.press("pageup")
        pyautogui.moveTo(cx, cy)
        pyautogui.scroll(-SCROLL_DOWN_CLICKS)

    else:
        print(f"\nStopped after {args.max_pages} pages (use --max-pages to increase)")

    conn.close()
    print(f"\nTotal records: {total}")
    print(f"Database: {os.path.abspath(args.db)}")


if __name__ == "__main__":
    main()
