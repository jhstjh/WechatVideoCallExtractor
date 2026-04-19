# WeChat Video Call Extractor

Extracts video/voice call records from the WeChat Windows desktop client by OCR'ing the chat history, then renders the data as a WeChat-style HTML page (with optional PNG export and stats panel).

## Features

- **Bottom-to-top scan** of an open WeChat chat using `PageUp` + a small mouse-scroll nudge so no entries are clipped.
- Fast OCR via [`rapidocr-onnxruntime`](https://github.com/RapidAI/RapidOCR).
- Detects **caller side** (me / them) from the horizontal position of the bubble.
- Stores `(timestamp, caller, status, duration, duration_seconds)` into SQLite, deduped via a unique index.
- Optional `--month N` flag to scope the scan to a single month — DB filename auto-becomes `wechat_calls_mNN.db` and scanning stops once the chat scrolls past that month.
- Renderer produces a self-contained HTML page that mimics the WeChat phone-app dark theme:
  - chronological timeline with date/time separators
  - rounded-square avatars (replace `avatars/me.jpg` and `avatars/them.jpg` with your own)
  - sent/received bubbles with proper tail and call-icon orientation
  - "..." menu with **call statistics panel** and **export to PNG** (via `html2canvas`, no extra Python deps)

## Install

```bash
pip install -r requirements.txt
```

Replace `avatars/me.jpg` and `avatars/them.jpg` with the avatars you want shown.

## Usage

### 1. Capture call records

```bash
python scraper.py
# or, scope to a single month
python scraper.py --month 4
```

You get a 5-second countdown — focus the WeChat chat window during it. The script will then PageUp through the chat, OCR every frame, and write rows to SQLite.

Useful flags:

| Flag           | Default | Description                                        |
|----------------|---------|----------------------------------------------------|
| `--db PATH`    | auto    | SQLite output path                                 |
| `--month N`    | none    | Stop scanning once timestamps fall outside month N |
| `--max-pages`  | 500     | Safety cap on PageUp presses                       |
| `--debug`      | off     | Print every OCR line per frame                     |

### 2. Render to HTML

```bash
python renderer.py
# or
python renderer.py --month 4
```

Open the resulting `wechat_calls.html` (or `wechat_calls_mNN.html`) in any browser.

Click the "..." button in the top-right for:
- **通话统计** — full stats panel (total calls, total duration, missed counts, per-side breakdown)
- **导出图片** — downloads the chat content as a PNG

## Files

| File             | Purpose                                                  |
|------------------|----------------------------------------------------------|
| `scraper.py`     | Captures the WeChat window, OCRs, writes to SQLite       |
| `renderer.py`    | Renders SQLite records into a WeChat-themed HTML page    |
| `avatars/`       | Avatar images used by the renderer (`me.jpg`, `them.jpg`)|
| `requirements.txt` | Python dependencies                                    |

## Notes

- Windows-only (uses `pywin32` + `PrintWindow` to capture the WeChat client window).
- The script does not distinguish video vs. voice calls — WeChat denotes them only by icon and the difference proved unreliable to detect by template matching.
- DPI-aware: works correctly on 4K / scaled displays.
