"""Render WeChat call records from SQLite to a WeChat dark-theme HTML page.

Usage:
    python renderer.py [--db PATH] [--output PATH] [--month N]
"""

import os
import re
import sqlite3
import base64
import argparse
from datetime import datetime

DB_FILE = "wechat_calls.db"
OUTPUT_FILE = "wechat_calls.html"
ME_AVATAR = os.path.join("avatars", "me.jpg")
THEM_AVATAR = os.path.join("avatars", "them.jpg")


def img_to_data_uri(path):
    if not path or not os.path.isfile(path):
        return None
    ext = os.path.splitext(path)[1].lower()
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".gif": "image/gif",
            ".webp": "image/webp"}.get(ext, "image/png")
    with open(path, "rb") as f:
        return f"data:{mime};base64,{base64.b64encode(f.read()).decode()}"


def avatar_html(data_uri, name, side):
    if data_uri:
        return f'<img class="avatar" src="{data_uri}" alt="{name}">'
    ch = name[0] if name else "?"
    bg = "#576B95" if side == "me" else "#E45C3A"
    return (f'<div class="avatar avatar-init" style="background:{bg}">'
            f'{ch}</div>')


def fmt_timestamp(ts: str) -> str:
    """Insert spaces between date / day-of-week / time.
       '3月15日星期天00:47'  -> '3月15日 星期天 00:47'
       '3月15日00:47'        -> '3月15日 00:47'
       '昨天00:47' / '今天00:47' -> '昨天 00:47' / '今天 00:47'
    """
    ts = re.sub(r'(日)(星期)', r'\1 \2', ts)
    ts = re.sub(r'(星期[一二三四五六天日]|周[一二三四五六日天])(\d)', r'\1 \2', ts)
    ts = re.sub(r'(\d+月\d+日)(\d)', r'\1 \2', ts)
    ts = re.sub(r'(昨天|今天|前天)(\d)', r'\1 \2', ts)
    return ts


_DATE_RE = re.compile(r"(\d{1,2})月(\d{1,2})日")
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


def ts_sort_key(ts: str):
    """Parse a WeChat session-timestamp string into a tuple suitable for
    chronological sorting (ascending = oldest first).

    Handles:
      "3月15日 22:21"           -> (3, 15, 22, 21)
      "3月15日星期五 22:21"      -> (3, 15, 22, 21)
      "今天 22:21" / "昨天" /     -> bucketed via a pseudo-month so they
        "前天 22:21"              sort AFTER any explicit-date entry in
                                  the same DB (they refer to the days
                                  immediately preceding the scrape run).
    """
    s = ts or ""
    tm = _TIME_RE.search(s)
    hh, mm = (int(tm.group(1)), int(tm.group(2))) if tm else (0, 0)

    md = _DATE_RE.search(s)
    if md:
        return (int(md.group(1)), int(md.group(2)), hh, mm)

    if "前天" in s:
        return (99, 1, hh, mm)
    if "昨天" in s:
        return (99, 2, hh, mm)
    if "今天" in s:
        return (99, 3, hh, mm)

    return (0, 0, hh, mm)


def fmt_dur(seconds):
    if seconds is None:
        return None
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


ME_NAME = "我"
THEM_NAME = "对方"


def compute_stats(rows):
    total = len(rows)
    connected = [r for r in rows if r["duration_seconds"] and r["duration_seconds"] > 0]

    total_s = sum(r["duration_seconds"] for r in connected)
    avg_s = total_s // len(connected) if connected else 0
    longest = max((r["duration_seconds"] for r in connected), default=0)

    me_rows = [r for r in connected if r["caller"] == "me"]
    them_rows = [r for r in connected if r["caller"] == "them"]
    me_total_s = sum(r["duration_seconds"] for r in me_rows)
    them_total_s = sum(r["duration_seconds"] for r in them_rows)

    return {
        "total": total,
        "connected": len(connected),
        "total_dur": fmt_dur(total_s) or "0:00",
        "avg_dur": fmt_dur(avg_s) or "0:00",
        "longest_dur": fmt_dur(longest) or "0:00",
        "me_calls": len(me_rows),
        "me_dur": fmt_dur(me_total_s) or "0:00",
        "them_calls": len(them_rows),
        "them_dur": fmt_dur(them_total_s) or "0:00",
    }


def render(db_path, output_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT timestamp, caller, status, duration, duration_seconds "
        "FROM calls"
    ).fetchall()
    conn.close()

    if not rows:
        print("No records found in database.")
        return

    # Sort chronologically (oldest first) by parsing the timestamp text;
    # the `id` column reflects scrape-insertion order which is NOT
    # chronological for the chat-history scraper.
    rows = sorted(rows, key=lambda r: ts_sort_key(r["timestamp"]))

    stats = compute_stats(rows)
    me_uri = img_to_data_uri(ME_AVATAR)
    them_uri = img_to_data_uri(THEM_AVATAR)

    entries = []
    last_ts = None

    for row in rows:
        ts = row["timestamp"]
        caller = row["caller"]
        dur_s = row["duration_seconds"]
        is_missed = dur_s is None or dur_s == 0
        is_me = caller == "me"

        if ts != last_ts:
            entries.append(f'      <div class="time">{fmt_timestamp(ts)}</div>')
            last_ts = ts

        name = ME_NAME if is_me else THEM_NAME
        uri = me_uri if is_me else them_uri
        av = avatar_html(uri, name, "me" if is_me else "them")
        side = "sent" if is_me else "rcvd"

        if is_missed:
            call_label = "已取消"
        else:
            call_label = f"通话时长 {fmt_dur(dur_s)}"

        cam_left = ('<svg class="cam" viewBox="0 0 21 13" width="17" height="11" '
                    'fill="none" stroke="currentColor" stroke-width="1.4" '
                    'stroke-linejoin="round">'
                    '<rect x="1" y="1" width="12" height="11" rx="2"/>'
                    '<path d="M13 4l6-2v9l-6-2z"/></svg>')
        cam_right = ('<svg class="cam" viewBox="0 0 21 13" width="17" height="11" '
                     'fill="none" stroke="currentColor" stroke-width="1.4" '
                     'stroke-linejoin="round">'
                     '<rect x="8" y="1" width="12" height="11" rx="2"/>'
                     '<path d="M8 4l-6-2v9l6-2z"/></svg>')

        if is_me:
            inner = f'<span class="ct">{call_label}</span>{cam_right}'
        else:
            inner = f'{cam_left}<span class="ct">{call_label}</span>'

        entries.append(f'''      <div class="msg {side}">
        {av}
        <div class="bbl"><div class="tail"></div>
          <div class="call">{inner}</div>
        </div>
      </div>''')

    html = TEMPLATE.format(
        contact_name=THEM_NAME,
        entries="\n".join(entries),
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        me_name=ME_NAME,
        them_name=THEM_NAME,
        **stats,
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Rendered {len(rows)} records -> {os.path.abspath(output_path)}")


TEMPLATE = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>WeChat</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}

body{{
  background:#111;
  font-family:-apple-system,"PingFang SC","Hiragino Sans GB",
              "Microsoft YaHei","Helvetica Neue",Arial,sans-serif;
  -webkit-font-smoothing:antialiased;
  color:#e8e8e8;
}}

/* ── nav bar (44px like iOS WeChat) ── */
.nav{{
  height:44px;
  background:#1E1E1E;
  display:flex;align-items:center;justify-content:center;
  position:sticky;top:0;z-index:20;
}}
.nav-back{{
  position:absolute;left:8px;top:50%;transform:translateY(-50%);
  width:30px;height:30px;display:flex;align-items:center;justify-content:center;
}}
.nav-back svg{{color:#e8e8e8}}
.nav-title{{
  font-size:17px;font-weight:500;color:#e8e8e8;
}}
.nav-more{{
  position:absolute;right:12px;top:50%;transform:translateY(-50%);
  color:#e8e8e8;font-size:18px;letter-spacing:2px;
  cursor:pointer;user-select:none;
  padding:4px 6px;border-radius:4px;
}}
.nav-more:hover{{background:rgba(255,255,255,.08)}}

/* ── dropdown menu ── */
.menu-wrap{{
  position:absolute;right:8px;top:40px;z-index:50;
}}
.menu{{
  display:none;
  background:#2C2C2C;
  border-radius:6px;
  min-width:140px;
  box-shadow:0 4px 20px rgba(0,0,0,.5);
  overflow:hidden;
}}
.menu.open{{display:block}}
.menu-item{{
  display:flex;align-items:center;gap:8px;
  padding:12px 16px;
  font-size:15px;color:#e8e8e8;
  cursor:pointer;white-space:nowrap;
}}
.menu-item:hover{{background:#3A3A3A}}
.menu-item+.menu-item{{border-top:1px solid #3A3A3A}}
.menu-item svg{{flex-shrink:0;color:#ABABAB}}

/* ── stats overlay ── */
.stats-mask{{
  display:none;
  position:fixed;inset:0;z-index:90;
  background:rgba(0,0,0,.5);
}}
.stats-mask.open{{display:block}}

.stats-panel{{
  position:fixed;top:0;right:-340px;bottom:0;width:340px;z-index:100;
  background:#1C1C1E;
  transition:right .28s cubic-bezier(.25,.8,.25,1);
  overflow-y:auto;
  padding:0 0 40px;
}}
.stats-panel.open{{right:0}}

.sp-header{{
  height:44px;display:flex;align-items:center;
  padding:0 16px;
  border-bottom:1px solid #2C2C2C;
}}
.sp-back{{
  cursor:pointer;color:#e8e8e8;display:flex;align-items:center;gap:6px;
  font-size:15px;
}}
.sp-back svg{{flex-shrink:0}}
.sp-title{{
  flex:1;text-align:center;
  font-size:17px;font-weight:500;color:#e8e8e8;
  margin-right:40px;
}}

.stat-section{{padding:20px 16px 8px;}}
.stat-section h3{{
  font-size:13px;font-weight:500;color:#808080;
  text-transform:uppercase;letter-spacing:.5px;
  margin-bottom:12px;
}}
.stat-row{{
  display:flex;justify-content:space-between;align-items:center;
  padding:9px 0;
  border-bottom:1px solid #2A2A2A;
  font-size:15px;
}}
.stat-row:last-child{{border-bottom:none}}
.stat-label{{color:#ABABAB}}
.stat-val{{color:#e8e8e8;font-weight:600;font-variant-numeric:tabular-nums}}
.stat-val.accent{{color:#57C472}}

/* ── chat body ── */
.chat-wrap{{
  max-width:414px;   /* iPhone width */
  margin:0 auto;
  padding:4px 10px 60px;
}}

/* timestamp */
.time{{
  text-align:center;
  padding:12px 0 6px;
  font-size:12px;
  color:#808080;
}}

/* ── message row ── */
.msg{{
  display:flex;
  align-items:flex-start;
  padding:5px 6px;
}}
.msg.sent{{ flex-direction:row-reverse; }}

/* avatar — rounded square like real WeChat */
.avatar{{
  width:40px;height:40px;
  border-radius:4px;
  flex-shrink:0;
  object-fit:cover;
}}
.avatar-init{{
  display:flex;align-items:center;justify-content:center;
  font-size:17px;font-weight:600;color:#fff;
}}

/* ── bubble ── */
.bbl{{
  position:relative;
  display:inline-flex;
  align-items:center;
  border-radius:4px;
  padding:0 12px;
  margin:0 10px;
  height:40px;
}}
.msg.rcvd .bbl{{ background:#2C2C2C; }}
.msg.sent .bbl{{ background:#57C472; }}

/* bubble tail */
.tail{{
  position:absolute;
  top:50%;transform:translateY(-50%);
  width:0;height:0;
}}
.msg.rcvd .tail{{
  left:-6px;
  border-top:5px solid transparent;
  border-bottom:5px solid transparent;
  border-right:6px solid #2C2C2C;
}}
.msg.sent .tail{{
  right:-6px;
  border-top:5px solid transparent;
  border-bottom:5px solid transparent;
  border-left:6px solid #57C472;
}}

/* ── call entry inside bubble ── */
.call{{
  display:flex;
  align-items:center;
  gap:5px;
  white-space:nowrap;
}}

.cam{{
  flex-shrink:0;
  display:block;
}}
.msg.rcvd .cam{{ color:#e8e8e8; }}
.msg.sent .cam{{ color:#111; }}

.ct{{
  font-size:15px;
  line-height:1;
}}
.msg.rcvd .ct{{ color:#e8e8e8; }}
.msg.sent .ct{{ color:#111; }}

/* ── footer ── */
.foot{{
  text-align:center;
  padding:12px;
  font-size:10px;
  color:#333;
}}
</style>
</head>
<body>

<div class="nav">
  <div class="nav-back">
    <svg width="12" height="20" viewBox="0 0 12 20" fill="none">
      <path d="M10 2L2 10L10 18" stroke="currentColor" stroke-width="2"
            stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
  </div>
  <div class="nav-title">{contact_name}</div>
  <div class="nav-more" onclick="toggleMenu(event)">&middot;&middot;&middot;</div>
  <div class="menu-wrap">
    <div class="menu" id="optMenu">
      <div class="menu-item" onclick="openStats()">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="1" y="9" width="3" height="6" rx=".5"/><rect x="6.5" y="5" width="3" height="10" rx=".5"/><rect x="12" y="1" width="3" height="14" rx=".5"/></svg>
        通话统计
      </div>
      <div class="menu-item" onclick="exportPNG()">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M2 10v3a1 1 0 001 1h10a1 1 0 001-1v-3"/><path d="M8 2v8m0 0l-3-3m3 3l3-3"/></svg>
        导出图片
      </div>
    </div>
  </div>
</div>

<div class="chat-wrap">
{entries}
</div>

<div class="foot">Generated {generated}</div>

<!-- stats overlay -->
<div class="stats-mask" id="statsMask" onclick="toggleStats()"></div>
<div class="stats-panel" id="statsPanel">
  <div class="sp-header">
    <div class="sp-back" onclick="toggleStats()">
      <svg width="10" height="16" viewBox="0 0 10 16" fill="none">
        <path d="M8 2L2 8L8 14" stroke="currentColor" stroke-width="2"
              stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </div>
    <div class="sp-title">通话统计</div>
  </div>

  <div class="stat-section">
    <h3>总览</h3>
    <div class="stat-row"><span class="stat-label">总通话次数</span><span class="stat-val">{total}</span></div>
    <div class="stat-row"><span class="stat-label">已接通</span><span class="stat-val accent">{connected}</span></div>
    <div class="stat-row"><span class="stat-label">总通话时长</span><span class="stat-val accent">{total_dur}</span></div>
    <div class="stat-row"><span class="stat-label">平均通话时长</span><span class="stat-val">{avg_dur}</span></div>
    <div class="stat-row"><span class="stat-label">最长通话</span><span class="stat-val">{longest_dur}</span></div>
  </div>

  <div class="stat-section">
    <h3>{me_name}</h3>
    <div class="stat-row"><span class="stat-label">拨出已接通</span><span class="stat-val">{me_calls}</span></div>
    <div class="stat-row"><span class="stat-label">拨出总时长</span><span class="stat-val">{me_dur}</span></div>
  </div>

  <div class="stat-section">
    <h3>{them_name}</h3>
    <div class="stat-row"><span class="stat-label">拨入已接通</span><span class="stat-val">{them_calls}</span></div>
    <div class="stat-row"><span class="stat-label">拨入总时长</span><span class="stat-val">{them_dur}</span></div>
  </div>
</div>

<script src="https://html2canvas.hertzen.com/dist/html2canvas.min.js"></script>
<script>
function toggleMenu(e){{
  e.stopPropagation();
  document.getElementById('optMenu').classList.toggle('open');
}}
document.addEventListener('click',()=>{{
  document.getElementById('optMenu').classList.remove('open');
}});

function openStats(){{
  document.getElementById('optMenu').classList.remove('open');
  document.getElementById('statsPanel').classList.add('open');
  document.getElementById('statsMask').classList.add('open');
}}
function toggleStats(){{
  document.getElementById('statsPanel').classList.toggle('open');
  document.getElementById('statsMask').classList.toggle('open');
}}

function exportPNG(){{
  document.getElementById('optMenu').classList.remove('open');
  var wrap=document.querySelector('.chat-wrap');
  html2canvas(wrap,{{
    backgroundColor:'#111',
    scale:2,
    useCORS:true,
  }}).then(function(canvas){{
    var a=document.createElement('a');
    a.download='wechat_calls.png';
    a.href=canvas.toDataURL('image/png');
    a.click();
  }});
}}
</script>

</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="Render WeChat call records to HTML")
    ap.add_argument("--db",     default=None, help="SQLite database path")
    ap.add_argument("--output", default=None, help="Output HTML path")
    ap.add_argument("--month",  type=int, default=None,
                    help="Month number (1-12). Auto-selects db/output filenames.")
    args = ap.parse_args()

    if args.db is None:
        if args.month:
            args.db = f"wechat_calls_m{args.month:02d}.db"
        else:
            args.db = DB_FILE
    if args.output is None:
        if args.month:
            args.output = f"wechat_calls_m{args.month:02d}.html"
        else:
            args.output = OUTPUT_FILE

    render(args.db, args.output)


if __name__ == "__main__":
    main()
