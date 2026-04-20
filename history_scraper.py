"""WeChat Call Record Extractor — Chat-History Edition

Scans WeChat's "Chat History" (聊天记录搜索) results window from bottom
to top. In this view every message is a self-contained card with its
own session timestamp printed at the top-right (e.g. "3月31日 22:21"),
and the message body (e.g. "通话时长 22:10") immediately below.

Navigation: presses the Up arrow key TWICE per iteration (a single
press doesn't always refresh the view, but a double press reliably
rolls exactly one card in at the top). After the press we wait until
the screen actually changes and then OCR the whole frame — entries
already in the SQLite database are silently skipped, so only the newly
exposed card produces a [FOUND] line.

Termination: stop with Ctrl+C, or pass --month N so the loop bails out
once it sees a frame containing only timestamps older than month N.

Performance:
  * OCR runs in a pool of worker threads (--workers N, default 10).
    Workers OCR + write to SQLite + log; the main thread only
    captures screenshots and pumps them into a bounded queue.
  * ONNX Runtime releases the GIL during inference, so the workers
    actually run in parallel with the main thread (and with each
    other, modulo CPU/GPU contention — see --workers help).
  * Main thread tight-loops the capture syscall with no sleep,
    deduping by 256-pixel signature (microseconds) so only newly-
    rendered frames get submitted.
  * Only the top TOP_REGION_PX of each new frame is OCR'd, since each
    arrow-up press exposes one card at the top.
  * Two screenshot backends:
      - Windows.Graphics.Capture (default, ~2-5ms/frame, Win10 1803+,
        requires `pip install windows-capture`)
      - PrintWindow (--no-wgc, ~15-20ms/frame, works everywhere)
  * Pass --gpu to try CUDA inference (requires `pip install
    onnxruntime-gpu` and a working CUDA setup). Falls back to CPU
    with a warning if unavailable.

Usage:
    python history_scraper.py [--db PATH] [--month N]
                              [--max-pages 5000]
                              [--me NAME]
                              [--gpu] [--workers 10] [--no-wgc]
                              [--debug]
"""

import argparse
import ctypes
import os
import queue
import re
import sqlite3
import sys
import threading
import time

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

# Lines whose left edge is left of this fraction of the window width are
# treated as out-of-area chrome (sidebar / filter panel) and dropped.
# History view is usually full-width, so 0 is a safe default.
CHAT_AREA_LEFT_RATIO = 0.0

# Maximum vertical gap (px) between a timestamp at the top of a card and
# the call text below it. Used to bind a TS to its own card and avoid
# stealing the timestamp of a neighbouring entry.
TS_TO_CALL_GAP_MAX = 120

# How many pixels two boxes' centres can differ in y and still be treated
# as belonging to the same row inside a card (e.g. for matching the
# sender-name box to the timestamp box on the card's header row).
SAME_ROW_Y_TOL = 18

# After pressing the arrow keys, WeChat may need a moment to render the
# new view. The main thread tight-loops `capture_frame()` (no sleep)
# until the screenshot hash differs from the previously-submitted one,
# so dispatch latency is bounded only by how fast PrintWindow returns
# (~20ms per call). NO_CHANGE_TIMEOUT_S is the wall-clock budget after
# which we give up on a render and submit the current frame anyway,
# so we don't spin forever if WeChat hangs or we hit the top of the
# chat-history list.
NO_CHANGE_TIMEOUT_S = 1.5

# After the initial frame, only OCR the top this-many rows of the
# window. Each arrow-up press only rolls a single card into view at
# the top, so anything below is already captured/deduped — restricting
# OCR to a slim band at the top is the single biggest speed win.
# Set to 0 to disable cropping (always OCR the whole window).
TOP_REGION_PX = 400

# When a date and a time are OCR'd as separate boxes (e.g. "3月31日" and
# "22:21" on the same row), merge them into a synthetic timestamp if
# they sit on roughly the same y line and are within this many pixels
# apart horizontally.
TS_MERGE_Y_TOL = 12       # px
TS_MERGE_X_GAP_MAX = 80   # px

DB_FILE = "wechat_calls.db"

# How many pixels to sample for cheap "did the frame change?" checks.
# 256 fixed positions spread across the top OCR band give us a
# microsecond-cost signature that's astronomically unlikely to ever
# false-negative (probability of a real scroll producing identical
# values at all 256 positions is effectively zero for screenshots
# with millions of distinct color values).
SIGNATURE_SAMPLES = 256

ctypes.windll.user32.SetProcessDPIAware()


# ===================== window capture backends =====================

class PrintWindowCapture:
    """Classic GDI-based capture using the `PrintWindow` syscall.

    Works on every Windows version and for almost every window, but
    each call costs ~15-20 ms because it allocates a new bitmap and
    copies the whole window through GDI. Used as a fallback when
    Windows.Graphics.Capture isn't available."""

    name = "PrintWindow (GDI)"

    def __init__(self, hwnd):
        self.hwnd = hwnd

    def capture(self):
        hwnd = self.hwnd
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        w, h = r - l, b - t
        hdc = win32gui.GetWindowDC(hwnd)
        src_dc = win32ui.CreateDCFromHandle(hdc)
        mem_dc = src_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(src_dc, w, h)
        mem_dc.SelectObject(bmp)
        # PW_RENDERFULLCONTENT = 2 → grabs DWM-composited content too
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

    def close(self):
        pass


class WgcCapture:
    """Windows.Graphics.Capture (WGC) — the modern GPU-accelerated
    capture API used by OBS, Snipping Tool, etc. Available on
    Windows 10 1803+ (Win 11 needed for hidden/minimised windows).

    Per-capture cost is typically 2-5 ms vs ~15-20 ms for PrintWindow,
    because frames are produced by the DWM compositor directly into
    a Direct3D texture instead of being re-rendered through GDI.

    We use the `windows-capture` Python package as the wrapper —
    writing the D3D11/WinRT plumbing by hand is hundreds of lines of
    fragile COM interop and `windows-capture` (pure-Rust binding) has
    been battle-tested. Install with `pip install windows-capture`.

    Architecture: WGC pushes frames asynchronously into our callback
    at the compositor rate (60-144 Hz). We just stash the latest one
    behind a lock; `capture()` returns whatever's freshest in O(1).
    """

    name = "Windows.Graphics.Capture (WGC)"

    def __init__(self, hwnd):
        # Lazy import — only require windows-capture if the user
        # actually opts into WGC.
        from windows_capture import WindowsCapture, Frame

        self._lock = threading.Lock()
        self._latest_img = None
        self._stop = False
        self._control = None

        capture = WindowsCapture(
            cursor_capture=False,
            draw_border=False,
            window_name=None,  # we'll bind by HWND below
        )
        # Some versions expose a kwarg for HWND; older releases bind
        # by window title. We patch the underlying handle if possible.
        if hasattr(capture, "set_window_handle"):
            capture.set_window_handle(hwnd)
        else:
            # Fall back: pull the title and rebuild by name. Not
            # perfect (multiple windows can share titles) but works
            # for single-instance WeChat.
            title = win32gui.GetWindowText(hwnd)
            capture = WindowsCapture(
                cursor_capture=False, draw_border=False,
                window_name=title,
            )

        @capture.event
        def on_frame_arrived(frame: "Frame", capture_control):
            # frame.frame_buffer is a (h, w, 4) BGRA numpy array.
            arr_bgra = frame.frame_buffer
            # Convert BGRA -> RGB on the fly. PIL needs RGB.
            arr_rgb = arr_bgra[:, :, [2, 1, 0]]
            img = Image.fromarray(arr_rgb, mode="RGB")
            with self._lock:
                self._latest_img = img
            if self._stop:
                capture_control.stop()

        @capture.event
        def on_closed():
            pass

        # start_free_threaded() runs the capture loop on a background
        # thread and returns a control object we can use to stop it.
        self._control = capture.start_free_threaded()

        # Block until the very first frame arrives so callers don't
        # see None on their first `capture()`.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with self._lock:
                if self._latest_img is not None:
                    return
            time.sleep(0.01)
        raise RuntimeError(
            "WGC: no frame produced within 2s — capture failed")

    def capture(self):
        with self._lock:
            return self._latest_img

    def close(self):
        self._stop = True
        try:
            if self._control is not None:
                self._control.stop()
        except Exception:
            pass


# Capture-source factory used by main(). Tries WGC first if requested
# and gracefully falls back to PrintWindow.

def make_capture_source(hwnd, prefer_wgc):
    """Return a capture object exposing .capture() -> PIL.Image."""
    if prefer_wgc:
        try:
            return WgcCapture(hwnd)
        except ImportError as e:
            print(f"WARN: WGC requested but `windows-capture` is not "
                  f"installed ({e!r}). `pip install windows-capture`. "
                  "Falling back to PrintWindow.")
        except Exception as e:
            print(f"WARN: WGC capture init failed ({e!r}); "
                  "falling back to PrintWindow.")
    return PrintWindowCapture(hwnd)


# Back-compat: a module-level convenience that callers can swap in
# for the old top-level capture_window().
def capture_window(hwnd) -> Image.Image:
    return PrintWindowCapture(hwnd).capture()


# Capture-source factory used by main(). Tries WGC first if requested
# and gracefully falls back to PrintWindow.

def make_capture_source(hwnd, prefer_wgc):
    """Return a capture object exposing .capture() -> PIL.Image."""
    if prefer_wgc:
        try:
            return WgcCapture(hwnd)
        except Exception as e:
            print(f"WARN: WGC capture init failed ({e!r}); "
                  "falling back to PrintWindow.")
    return PrintWindowCapture(hwnd)


# Back-compat: a function that callers can swap in for the old
# top-level capture_window().
def capture_window(hwnd) -> Image.Image:
    return PrintWindowCapture(hwnd).capture()


# ===================== change-detection signature =====================

class FrameSignature:
    """Cheap "did the screenshot change?" check.

    Instead of hashing the whole frame (~5 ms md5 of 6 MB), we sample
    SIGNATURE_SAMPLES fixed (y, x) positions inside the top OCR band
    and compare the resulting tiny vector. Total cost per check is
    a few microseconds (one numpy fancy-indexing op).

    The sample positions are seeded deterministically from frame
    dimensions so two FrameSignature instances over the same window
    produce comparable signatures."""

    def __init__(self, height, width, n_samples=SIGNATURE_SAMPLES,
                 seed=0xC0FFEE):
        rng = np.random.default_rng(seed)
        self.ys = rng.integers(0, height, n_samples, dtype=np.int32)
        self.xs = rng.integers(0, width, n_samples, dtype=np.int32)

    def of(self, arr):
        """Return a (n_samples, 3) uint8 array — the signature."""
        return arr[self.ys, self.xs]

    @staticmethod
    def equal(a, b):
        # `is` short-circuits for None vs None / first-frame cases.
        if a is None or b is None:
            return False
        return np.array_equal(a, b)


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
_TIME_ONLY_RE = re.compile(r"^\s*(?:[01]?\d|2[0-3]):[0-5]\d\s*$")
_MONTH_RE = re.compile(r"(\d{1,2})月")
_DATE_PREFIX_RE = re.compile(
    r"(\d{1,2}月\d{1,2}日"           # 3月15日
    r"|今天|昨天|前天"                # relative dates
    r"|星期[一二三四五六天日]"         # 星期X
    r"|周[一二三四五六日天])"          # 周X
)
_DATE_ONLY_RE = re.compile(
    r"^\s*(?:\d{1,2}月\d{1,2}日|今天|昨天|前天)\s*$"
)


def extract_month(text: str):
    """Return the month number from a timestamp string, or None."""
    m = _MONTH_RE.search(text)
    return int(m.group(1)) if m else None


def looks_like_timestamp(text: str) -> bool:
    """A real session-timestamp must have BOTH a date marker and an
    HH:MM time within 00:00-23:59."""
    s = text.strip()
    if is_call_entry(s):
        return False
    if len(s) > 40:
        return False
    if not _DATE_PREFIX_RE.search(s):
        return False
    return bool(_TS_RE.search(s))


def merge_split_timestamps(results):
    """OCR sometimes returns the date and the time of a card timestamp as
    two separate boxes (e.g. "3月31日" and "22:21" on the same row).
    Stitch such pairs into a synthetic combined entry so downstream
    look-ups can find them.

    Returns a NEW list with the originals plus any synthesised entries.
    """
    date_boxes = [(b, t.strip(), c) for b, t, c in results
                  if _DATE_ONLY_RE.match(t.strip())]
    time_boxes = [(b, t.strip(), c) for b, t, c in results
                  if _TIME_ONLY_RE.match(t.strip())]
    if not date_boxes or not time_boxes:
        return results

    extra = []
    for d_box, d_txt, d_conf in date_boxes:
        d_cy = _cy(d_box)
        d_xr = _x_max(d_box)
        # Pick the time box that sits on roughly the same row, to the
        # right of the date and within the X gap budget.
        best = None
        for t_box, t_txt, t_conf in time_boxes:
            if abs(_cy(t_box) - d_cy) > TS_MERGE_Y_TOL:
                continue
            gap = _x_min(t_box) - d_xr
            if gap < 0 or gap > TS_MERGE_X_GAP_MAX:
                continue
            if best is None or gap < best[3]:
                best = (t_box, t_txt, t_conf, gap)
        if best is None:
            continue
        t_box, t_txt, t_conf, _ = best
        # Synthesised bbox is the union of the two.
        xs = [p[0] for p in d_box] + [p[0] for p in t_box]
        ys = [p[1] for p in d_box] + [p[1] for p in t_box]
        merged_box = [
            [min(xs), min(ys)], [max(xs), min(ys)],
            [max(xs), max(ys)], [min(xs), max(ys)],
        ]
        merged_txt = f"{d_txt} {t_txt}"
        merged_conf = min(float(d_conf), float(t_conf)) \
            if isinstance(d_conf, (int, float, str)) else d_conf
        extra.append((merged_box, merged_txt, merged_conf))

    return list(results) + extra


def find_timestamp_for_card(ocr_hits, call_bbox):
    """In the chat-history view every call entry sits inside its own
    card whose session timestamp is printed at the TOP of the card; the
    call text sits BELOW it. Find the timestamp that:
      * lies above the call (y_max < call.y_min), and
      * is within TS_TO_CALL_GAP_MAX pixels of the call (so we don't
        steal a TS from a neighbouring card).
    Returns (bbox, text) or (None, None).
    """
    call_top = _y_min(call_bbox)
    best = None
    for bbox, txt, _ in ocr_hits:
        if not looks_like_timestamp(txt):
            continue
        ts_bottom = _y_max(bbox)
        gap = call_top - ts_bottom
        if 0 < gap <= TS_TO_CALL_GAP_MAX:
            if best is None or _y_max(bbox) > _y_max(best[0]):
                best = (bbox, txt)
    return best if best else (None, None)


def find_name_for_card(ocr_hits, ts_bbox):
    """The sender-name lives on the same header row as the card's
    timestamp, to its left (e.g.  "Jihui ST .... 3月31日 22:21"). Find
    the closest non-TS / non-call text box that:
      * sits on roughly the same y as ts_bbox, and
      * lies to the LEFT of ts_bbox.
    Returns the text, or None.
    """
    if ts_bbox is None:
        return None
    ts_cy = _cy(ts_bbox)
    ts_left = _x_min(ts_bbox)
    best = None
    best_gap = None
    for bbox, txt, _ in ocr_hits:
        s = txt.strip()
        if not s or is_call_entry(s) or looks_like_timestamp(s):
            continue
        if abs(_cy(bbox) - ts_cy) > SAME_ROW_Y_TOL:
            continue
        if _x_max(bbox) >= ts_left:
            continue
        gap = ts_left - _x_max(bbox)
        if best_gap is None or gap < best_gap:
            best = s
            best_gap = gap
    return best


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


# ===================== database =====================

def open_db_init(conn):
    """Create schema on an already-open SQLite connection."""
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


def open_db(path: str):
    """Convenience: open + init in one shot (single-thread callers)."""
    conn = sqlite3.connect(path)
    open_db_init(conn)
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


# ===================== OCR backend & pipeline =====================

def make_ocr(use_gpu: bool):
    """Construct a RapidOCR instance, optionally on GPU.

    GPU support requires `onnxruntime-gpu` (instead of `onnxruntime`)
    plus a working CUDA install. If GPU init fails for any reason we
    fall back to CPU and print a warning."""
    if not use_gpu:
        return RapidOCR(), "CPU"
    try:
        import onnxruntime as ort
        providers = ort.get_available_providers()
        if "CUDAExecutionProvider" not in providers:
            print(f"WARN: onnxruntime providers = {providers}; "
                  "no CUDA available, falling back to CPU.")
            return RapidOCR(), "CPU"
        # rapidocr_onnxruntime forwards these kwargs to the three
        # underlying ONNX sessions (det/cls/rec).
        ocr = RapidOCR(
            det_use_cuda=True,
            cls_use_cuda=True,
            rec_use_cuda=True,
        )
        return ocr, "GPU (CUDA)"
    except Exception as e:
        print(f"WARN: GPU OCR init failed ({e!r}); falling back to CPU.")
        return RapidOCR(), "CPU"


class OcrPool:
    """Pool of OCR worker threads, each with its own RapidOCR instance.

    Architecture:
        main thread  ──submit──>  in_q  ──>  [worker 1, worker 2, ...]
                                                  │
                                          OCR + processor()  (writes DB,
                                                              prints lines)

    The main thread just pumps captured frames into `in_q`; each worker
    pops one, runs OCR (which releases the GIL inside ONNX Runtime),
    then calls `processor(payload, results)` to do filtering, dedup,
    and SQLite writes.

    Backpressure: `submit()` blocks once `in_q` reaches `max_pending`
    so we don't queue up hundreds of 1080×N RGB frames in RAM if the
    workers fall behind.

    Stop signal: any worker returning 'out_of_month' from `processor`
    sets `stop_event`. The main thread polls this between submits and
    bails out of its pump loop.

    Realistic note on scaling:
        A single OCR call is NOT a monolithic CPU-saturating kernel.
        It's det inference (uses all cores well) + Python orchestration
        + N small rec inferences (scale poorly to many cores) +
        postprocessing. So 1 worker only utilises ~50-65% of CPU.
        Two offset workers fill those gaps and typically run
        1.4-1.7x faster on CPU; 3-4 can help on wide CPUs or GPU.
        Past that, you hit diminishing returns and eventually cache
        thrashing.
    """

    def __init__(self, num_workers, ocr_factory, processor,
                 max_pending=None):
        self.processor = processor
        self.in_q = queue.Queue(maxsize=max_pending or 0)
        self.stop_event = threading.Event()
        self.threads = []
        for i in range(num_workers):
            ocr = ocr_factory(i)
            t = threading.Thread(target=self._loop, args=(ocr, i),
                                 name=f"ocr-{i}", daemon=True)
            t.start()
            self.threads.append(t)

    def _loop(self, ocr, worker_id):
        while True:
            job = self.in_q.get()
            try:
                if job is None:
                    return
                payload, arr = job
                try:
                    results, _ = ocr(arr)
                except Exception as e:
                    print(f"WARN: [w{worker_id}] OCR failed "
                          f"for {payload[0]!r}: {e!r}")
                    results = None
                try:
                    if self.processor(payload, results) == "out_of_month":
                        self.stop_event.set()
                except Exception as e:
                    print(f"WARN: [w{worker_id}] processor failed "
                          f"for {payload[0]!r}: {e!r}")
            finally:
                self.in_q.task_done()

    def submit(self, payload, arr):
        """Blocks if the queue is full (backpressure)."""
        self.in_q.put((payload, arr))

    def wait_done(self):
        """Block until all submitted jobs have been processed."""
        self.in_q.join()

    def stop(self):
        for _ in self.threads:
            self.in_q.put(None)
        for t in self.threads:
            t.join(timeout=2.0)


# ===================== main =====================

def main():
    ap = argparse.ArgumentParser(
        description="WeChat call extractor (Chat-History edition)")
    ap.add_argument("--db",        default=None,
                    help="SQLite output path (auto-named if --month set)")
    ap.add_argument("--max-pages", type=int, default=5000,
                    help="Safety limit on arrow-up presses")
    ap.add_argument("--month",     type=int, default=None,
                    help="Only capture entries from this month (1-12). "
                         "Stops once the frame contains only timestamps "
                         "older than this month.")
    ap.add_argument("--me",        default="Jihui",
                    help="Substring (case-insensitive) used to identify "
                         "YOUR sender-name as it appears at the top of a "
                         "history card. If the name on a card matches, "
                         "the entry is recorded with caller='me', "
                         "otherwise caller='them'. Default: 'Jihui'.")
    ap.add_argument("--gpu",       action="store_true",
                    help="Try to run RapidOCR on GPU (CUDA). Requires "
                         "the onnxruntime-gpu package (pip install "
                         "onnxruntime-gpu) and a working CUDA install. "
                         "Falls back to CPU with a warning if unavailable.")
    ap.add_argument("--workers",   type=int, default=10,
                    help="Number of parallel OCR worker threads "
                         "(default 10). A single OCR call is a "
                         "multi-stage pipeline: det inference (uses "
                         "all cores well), then Python "
                         "orchestration + many small rec inferences "
                         "(scale poorly across cores), then "
                         "postprocessing. With many workers fighting "
                         "over physical cores, ORT thread "
                         "contention can erode throughput; if "
                         "10 regresses on your box try setting "
                         "OMP_NUM_THREADS=<physical_cores/workers> "
                         "as an env var before launching, or drop "
                         "back to ~physical_cores/2 workers.")
    ap.add_argument("--no-wgc",    action="store_false", dest="wgc",
                    help="Use legacy PrintWindow (GDI) capture "
                         "instead of the default Windows.Graphics."
                         "Capture (WGC) backend. Use this if WGC "
                         "fails on your machine (very old Windows, "
                         "no `windows-capture` package, etc.). WGC "
                         "is GPU-accelerated (~2-5ms/frame vs "
                         "~15-20ms for PrintWindow) and pushes "
                         "frames at the compositor rate (60-144 Hz).")
    ap.set_defaults(wgc=True)
    ap.add_argument("--debug",     action="store_true",
                    help="Print all OCR lines per frame for diagnostics")
    args = ap.parse_args()

    if args.db is None:
        args.db = (f"wechat_calls_m{args.month:02d}.db"
                   if args.month else DB_FILE)

    me_needle = args.me.strip().lower()
    if not me_needle:
        sys.exit("ERROR: --me cannot be empty")

    print("Focus the WeChat Chat-History window now...")
    for i in range(COUNTDOWN_S, 0, -1):
        print(f"  {i}...")
        time.sleep(1)
    print("Starting!\n")

    hwnd = win32gui.GetForegroundWindow()
    title = win32gui.GetWindowText(hwnd)
    if not title:
        sys.exit("ERROR: could not detect foreground window")
    print(f"Using window: \"{title}\"  hwnd={hwnd}")

    if args.workers < 1:
        sys.exit("ERROR: --workers must be >= 1")

    print(f"Loading RapidOCR x{args.workers} ...")
    ocr_backend = None

    def ocr_factory(worker_idx):
        nonlocal ocr_backend
        ocr_inst, backend = make_ocr(args.gpu)
        if ocr_backend is None:
            ocr_backend = backend
            print(f"OCR backend: {backend}")
        return ocr_inst

    # Single shared SQLite connection. SQLite allows multi-threaded
    # use with check_same_thread=False as long as we serialize writes
    # ourselves; `db_lock` does that. Writes are tiny (1 row each)
    # and far cheaper than OCR, so the lock is essentially uncontended.
    conn = sqlite3.connect(args.db, check_same_thread=False)
    open_db_init(conn)
    db_lock = threading.Lock()
    print(f"Database: {args.db}")

    # Shared mutable state touched by all worker threads.
    seen_entries = set()
    total = [0]
    state_lock = threading.Lock()

    # Set up the screenshot pipeline. WGC if requested + available,
    # otherwise GDI PrintWindow.
    capture_src = make_capture_source(hwnd, prefer_wgc=args.wgc)
    print(f"Capture backend: {capture_src.name}")

    # Frame-change signature: 256 fixed pixel positions inside the
    # band we OCR. Comparing two of these is microseconds — vastly
    # cheaper than md5'ing the whole 6 MB frame. Built lazily on the
    # first capture so we know the actual frame dimensions.
    sig = [None]   # FrameSignature once we've seen a frame

    def capture_frame():
        """Capture the current window and return (img, np-array, signature)."""
        img = capture_src.capture()
        arr = np.array(img)
        if sig[0] is None:
            band_h = min(TOP_REGION_PX, arr.shape[0]) if TOP_REGION_PX > 0 \
                else arr.shape[0]
            sig[0] = FrameSignature(band_h, arr.shape[1])
        signature = sig[0].of(arr)
        return img, arr, signature

    def wait_for_change(prev_sig):
        """Tight-loop `capture_frame()` (no sleep) until the
        signature differs from `prev_sig`, i.e. WeChat has rendered
        the arrow-up scroll. Pumps as fast as the capture backend
        allows (~50 cap/s on PrintWindow, ~200+ cap/s on WGC).
        Bails after NO_CHANGE_TIMEOUT_S wall-clock seconds so we
        don't spin forever if WeChat hangs or we've hit the top
        of the chat-history list."""
        img = arr = s = None
        attempts = 0
        deadline = time.monotonic() + NO_CHANGE_TIMEOUT_S
        while True:
            img, arr, s = capture_frame()
            attempts += 1
            if not FrameSignature.equal(prev_sig, s):
                if args.debug and attempts > 1:
                    print(f"  >> changed after {attempts} captures")
                return img, arr, s
            if time.monotonic() >= deadline:
                if args.debug:
                    print(f"  >> no change after {attempts} captures "
                          f"({NO_CHANGE_TIMEOUT_S:.1f}s) — submitting "
                          "current frame anyway")
                return img, arr, s

    def crop_for_ocr(arr, full):
        """Return the region of `arr` to send to OCR. Crop to the top
        TOP_REGION_PX rows for incremental frames; the initial frame
        is OCR'd in full so all currently-visible cards land in the DB
        immediately."""
        if not full and TOP_REGION_PX > 0 and arr.shape[0] > TOP_REGION_PX:
            return arr[:TOP_REGION_PX]
        return arr

    def process_results(payload, results):
        """Worker-thread callback: filter OCR results, dedupe, write to
        the shared SQLite connection. Returns 'ok'/'no_ocr'/'out_of_month'.

        Runs concurrently from N worker threads, so:
          * `state_lock` guards `seen_entries` and `total`
          * `db_lock` guards the shared sqlite connection
          * print() is single-line and atomic in CPython, so no print
            lock is needed in the normal path; the multi-line --debug
            block holds `state_lock` to keep its lines coherent.
        """
        label, img, arr = payload
        if results is None:
            return "no_ocr"

        frame_w = img.width
        if CHAT_AREA_LEFT_RATIO > 0:
            min_x = frame_w * CHAT_AREA_LEFT_RATIO
            results = [r for r in results if _x_min(r[0]) >= min_x]

        # Stitch any date/time pairs that OCR returned as separate boxes.
        results = merge_split_timestamps(results)

        if args.debug:
            with state_lock:
                print(f"--- {label} : {len(results)} OCR lines ---")
                for bbox, text, conf in sorted(results, key=lambda r: _cy(r[0])):
                    tag = ""
                    if is_call_entry(text):
                        tag = " <<CALL>>"
                    elif looks_like_timestamp(text):
                        tag = " <<TS>>"
                    print(f"  y={_cy(bbox):6.0f}  x={_x_min(bbox):6.0f}  "
                          f"conf={conf}  \"{text.strip()}\"{tag}")

        ts_strings = [t.strip() for _, t, _ in results
                      if looks_like_timestamp(t)]

        if args.month and ts_strings:
            in_month = [t for t in ts_strings
                        if extract_month(t) == args.month]
            older = [t for t in ts_strings
                     if extract_month(t) is not None
                     and extract_month(t) < args.month]
            if older and not in_month:
                return "out_of_month"

        for bbox, text, _ in sorted(results, key=lambda r: _cy(r[0])):
            if not is_call_entry(text):
                continue

            ts_bbox, ts_text = find_timestamp_for_card(results, bbox)
            if ts_text is None:
                if args.debug:
                    print(f"  >> CALL at y={_cy(bbox):.0f}: \"{text.strip()}\" "
                          f"— no TS within {TS_TO_CALL_GAP_MAX}px above, "
                          "skipping")
                continue

            ts_text = ts_text.strip()
            if args.month:
                m = extract_month(ts_text)
                if m is not None and m != args.month:
                    continue

            status = extract_status(text)
            dur_str, dur_s = parse_duration(text)

            name = find_name_for_card(results, ts_bbox)
            if name and me_needle in name.lower():
                caller = "me"
            else:
                caller = "them"
            if args.debug:
                print(f"  >> name on card = {name!r} → caller={caller}")

            key = (ts_text, caller, status, dur_str or "")
            with state_lock:
                if key in seen_entries:
                    is_dup_in_session = True
                else:
                    seen_entries.add(key)
                    is_dup_in_session = False

            if is_dup_in_session:
                if args.debug:
                    d = dur_str or "--"
                    print(f"[SKIP]  {ts_text} | {caller:>4} | {status} | {d}  "
                          "(already seen)")
                continue

            with db_lock:
                inserted = store(conn, ts_text, caller, status, dur_str, dur_s)

            d = dur_str or "--"
            if inserted:
                with state_lock:
                    total[0] += 1
                print(f"[FOUND] {ts_text} | {caller:>4} | {status} | {d}")
            else:
                print(f"[SKIP]  {ts_text} | {caller:>4} | {status} | {d}  "
                      "(already in DB)")

        return "ok"

    print("\n-- scanning chat history (bottom -> top) --\n")
    print("(press Ctrl+C to stop at any time)\n")

    def fire_arrow_up():
        # A single Up press doesn't always refresh the WeChat view; two
        # presses reliably roll exactly one card into view at the top.
        pyautogui.press("up")
        pyautogui.press("up")

    # Spin up the OCR worker pool. Bound the queue so a slow worker
    # can't let RAM balloon with held screenshots — each frame is a
    # window-sized RGB array (~6 MB on a 1080p WeChat). We keep
    # roughly workers*4 slots so each worker always has the next
    # 1-2 frames staged and never sits idle waiting on the pump.
    max_pending = max(8, args.workers * 4)
    pool = OcrPool(
        num_workers=args.workers,
        ocr_factory=ocr_factory,
        processor=process_results,
        max_pending=max_pending,
    )

    def submit(label, img, arr, full):
        ocr_arr = crop_for_ocr(arr, full)
        # Blocks on backpressure when in_q is full — that's exactly
        # when we WANT main thread to slow down.
        pool.submit((label, img, arr), ocr_arr)

    pages_submitted = 0
    try:
        # Initial frame — OCR the WHOLE thing so all already-visible
        # cards land in the DB right away. Submit it, then immediately
        # start advancing for the next frame while OCR is running.
        img, arr, last_sig = capture_frame()
        submit("initial", img, arr, full=True)
        fire_arrow_up()

        # Pure-pump main loop: capture, submit, fire keys, repeat.
        # All OCR + DB writes happen in worker threads. We only check
        # stop_event between submits.
        for page in range(args.max_pages):
            if pool.stop_event.is_set():
                break
            img, arr, last_sig = wait_for_change(last_sig)
            submit(f"page {page + 1}", img, arr, full=False)
            pages_submitted += 1
            fire_arrow_up()
        else:
            print(f"\nStopped after {args.max_pages} arrow-up presses "
                  "(use --max-pages to increase)")

        if pool.stop_event.is_set():
            print(f"\nA worker reported all timestamps outside month "
                  f"{args.month}. Stopping pump.")
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    finally:
        # Let any already-queued frames finish processing so we don't
        # lose entries that were captured but not yet OCR'd.
        print(f"\nDraining {pool.in_q.qsize()} queued frame(s)...")
        pool.wait_done()
        pool.stop()
        capture_src.close()

    conn.close()
    print(f"\nTotal records: {total[0]}")
    print(f"Database: {os.path.abspath(args.db)}")


if __name__ == "__main__":
    main()
