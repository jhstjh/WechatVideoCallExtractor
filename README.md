# WeChat Video Call Extractor

Extracts video/voice call records from the **WeChat Windows desktop client** by OCR'ing the chat-history search panel, then renders the data as a WeChat-style HTML page (with optional PNG export and a stats panel).

The recommended capture path is `history_scraper.py`, which scrapes WeChat's "Chat History" (聊天记录搜索) results window — every message there is a self-contained card with its own timestamp, so no clipping or backtracking is needed.

## Features

- **Self-contained card OCR** of WeChat's Chat-History search window. Each card carries its own date/time, sender name, and call duration.
- **Caller identification by name** via `--me NAME` (substring match against the sender label on each card).
- **Multi-threaded OCR pipeline**:
  - Main thread captures screenshots in a tight loop and pumps them into a bounded queue.
  - A pool of OCR worker threads (default 10) consumes the queue, runs RapidOCR, and writes to SQLite directly.
  - ONNX Runtime releases the GIL during inference, so workers genuinely run in parallel with each other and with the main thread.
- **Two screenshot backends**:
  - **Windows.Graphics.Capture (default)** — GPU-accelerated, ~2-5 ms/frame, pushes frames at the compositor rate (60-144 Hz). Requires Windows 10 1803+.
  - **PrintWindow GDI fallback** (`--no-wgc`) — works everywhere, ~15-20 ms/frame.
- **Microsecond change-detection** via a 256-pixel signature instead of full-frame hashing — main thread can spin at the display refresh rate without burning CPU on md5.
- **Optional GPU OCR** (`--gpu`) via `onnxruntime-gpu` — falls back to CPU with a warning if CUDA isn't available.
- **Month scoping** (`--month N`) — auto-names the DB and the rendered HTML (e.g. `wechat_calls_m04.db`, `wechat_calls_m04.html`) and stops scrolling once the chat passes that month.
- **WeChat-style HTML renderer** with rounded-square avatars, sent/received bubbles with proper tail and call-icon orientation, a "..." menu for **call statistics** and **export to PNG** (via `html2canvas`, no extra Python deps).

## Install

```powershell
pip install -r requirements.txt
```

Replace `avatars/me.jpg` and `avatars/them.jpg` with the avatars you want shown.

Optional add-ons:

| Want | `pip install` |
|------|---------------|
| GPU OCR (`--gpu`) | `onnxruntime-gpu` (and remove the CPU `onnxruntime` that came with `rapidocr-onnxruntime`) plus a working CUDA install |
| Force PrintWindow only | nothing extra — just pass `--no-wgc` |

## Usage

### 1. Capture call records from Chat History

Open WeChat → open a chat → click the menu → **Chat History (聊天记录)** → switch to a search/filter that shows the calls you want (e.g. "Calls" filter). The result is a scrollable list of self-contained call cards.

Then run:

```powershell
python history_scraper.py
# or scope to a single month
python history_scraper.py --month 4
# specify your sender name (default "Jihui") for caller=me detection
python history_scraper.py --month 4 --me Alice
```

You get a 5-second countdown — focus the WeChat **Chat-History** window during that time. The script then:

1. Captures the current frame and OCRs it (full window, all visible cards).
2. Presses **Up Up** to roll one new card into view at the top.
3. Captures, OCRs only the top 400 px (where the new card appears), submits to a worker.
4. Loops until `--max-pages` is hit, the chat history runs out, or the timestamps fall outside `--month`.
5. Writes deduped rows to SQLite and prints `[FOUND]` / `[SKIP]` lines per entry.

#### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--db PATH` | auto | SQLite output path (auto-named `wechat_calls_mNN.db` if `--month` set) |
| `--month N` | none | Stop scanning once timestamps fall outside month N |
| `--max-pages N` | 5000 | Safety cap on arrow-up presses |
| `--me NAME` | `Jihui` | Substring match on sender name to flag `caller=me` (case-insensitive) |
| `--workers N` | **10** | Parallel OCR worker threads. See "Performance tuning" below. |
| `--no-wgc` | off | Use legacy PrintWindow capture instead of WGC |
| `--gpu` | off | Try CUDA inference for RapidOCR |
| `--debug` | off | Print every OCR line per frame, plus capture/render diagnostics |

Stop with **Ctrl+C** at any time — the script drains the in-flight OCR queue before exiting so nothing is lost.

### 2. Render to HTML

```powershell
python renderer.py
# or
python renderer.py --month 4
```

Open the resulting `wechat_calls.html` (or `wechat_calls_mNN.html`) in any browser.

Click the **"..."** button in the top-right for:

- **通话统计** — full stats panel (total calls, total duration, missed counts, per-side breakdown)
- **导出图片** — downloads the chat content as a PNG (no header bar, just the bubbles)

## Performance tuning

The default `--workers 10` works well on most modern multi-core machines, but ONNX Runtime sets `intra_op_num_threads = num_physical_cores` per session by default. With many workers, that's `workers × cores` total threads competing for `cores` physical CPUs — cache thrashing can erode throughput past a certain point.

If `--workers 10` regresses on your box, cap each ORT session's thread count via env var:

```powershell
$env:OMP_NUM_THREADS=2          # ~physical_cores / workers
python history_scraper.py --month 4
```

Sweet spot is usually `OMP_NUM_THREADS = floor(physical_cores / workers)`. On an 8-core CPU, `--workers 4` with `OMP_NUM_THREADS=2` is often faster than `--workers 10` with default ORT threading.

For best results, also enable WGC (the default) and consider `--gpu` if you have CUDA. With WGC + GPU OCR, the bottleneck moves entirely to WeChat's render time after each arrow-up press.

## Files

| File | Purpose |
|------|---------|
| `history_scraper.py` | **Main scraper.** Captures the WeChat Chat-History window, OCRs cards, writes to SQLite |
| `scraper.py` | Legacy scraper that operates on the main chat window (PageUp + nudge-scroll). Kept for reference; less reliable than `history_scraper.py` due to clipped entries and grouped timestamps |
| `renderer.py` | Renders SQLite records into a WeChat-themed HTML page |
| `avatars/` | Avatar images used by the renderer (`me.jpg`, `them.jpg`) |
| `requirements.txt` | Python dependencies |

## Notes

- **Windows-only.** Uses `pywin32` for window handling and either `windows-capture` (WGC) or `PrintWindow` (GDI) for screenshots.
- **DPI-aware** — works on 4K / scaled displays.
- The script does **not** distinguish video vs. voice calls. WeChat denotes them only by icon, and template matching proved unreliable. Most modern WeChat chats are video by default, so all entries are recorded as the call status text returned by OCR (e.g. `通话时长 22:10`).
- `seen_entries` (in-memory) plus a SQLite `UNIQUE(timestamp, caller, status, duration)` index ensure idempotent reruns — running the scraper repeatedly only adds new entries.
