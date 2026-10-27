#!/usr/bin/env python3
"""hermestrace - Export Hermes Agent sessions to Markdown, XML, and an
interactive HTML workflow viewer (the Hermes counterpart of cctrace).

Hermes stores everything in plaintext SQLite at ~/.hermes/state.db:

    sessions  - one row per conversation (model, tokens, cost, cwd, ...)
    messages  - every user/assistant/tool message in order, with the
                assistant's tool_calls JSON, tool results, reasoning,
                token counts and timestamps.

Usage:
    hermestrace.py list [--limit N]
    hermestrace.py export SESSION_ID [--format md|xml|html|all] [--outdir DIR]
    hermestrace.py latest [--format all] [--outdir DIR]
"""

import argparse
import html
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
DB_PATH = HERMES_HOME / "state.db"


def connect():
    if not DB_PATH.exists():
        sys.exit(f"error: {DB_PATH} not found")
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def load_session(con, session_id):
    s = con.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not s:
        sys.exit(f"error: session {session_id!r} not found")
    msgs = con.execute(
        "SELECT * FROM messages WHERE session_id = ? AND active = 1 "
        "ORDER BY timestamp, id",
        (session_id,),
    ).fetchall()
    return dict(s), [dict(m) for m in msgs]


def parse_tool_calls(raw):
    if not raw:
        return []
    try:
        calls = json.loads(raw)
    except (ValueError, TypeError):
        return []
    out = []
    for c in calls:
        fn = c.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = fn.get("arguments")
        out.append({
            "call_id": c.get("call_id") or c.get("id"),
            "name": fn.get("name") or c.get("name") or "?",
            "arguments": args,
        })
    return out


def fmt_ts(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "?"


def truncate(text, limit=4000):
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text)} chars total]"


def build_trace(con, session_id):
    """Normalize a session into a list of timeline events."""
    sess, msgs = load_session(con, session_id)
    events = []
    pending = {}  # call_id -> event index for tool results

    for m in msgs:
        ts = m["timestamp"]
        role = m["role"]
        if role == "assistant":
            text = (m["content"] or "").strip()
            calls = parse_tool_calls(m.get("tool_calls"))
            if text:
                events.append({
                    "kind": "assistant_text", "role": role, "ts": ts,
                    "text": text, "reasoning": (m.get("reasoning_content") or m.get("reasoning") or "").strip(),
                    "tokens": m.get("token_count"), "finish_reason": m.get("finish_reason"),
                })
            for c in calls:
                ev = {
                    "kind": "tool_call", "role": role, "ts": ts,
                    "name": c["name"], "call_id": c["call_id"],
                    "arguments": c["arguments"], "result": None, "tokens": m.get("token_count"),
                }
                pending[c["call_id"]] = len(events)
                events.append(ev)
            if not text and not calls:
                events.append({"kind": "assistant_empty", "role": role, "ts": ts, "text": ""})
        elif role == "user":
            events.append({
                "kind": "user", "role": role, "ts": ts,
                "text": truncate(m["content"] or "", 8000),
            })
        elif role == "tool":
            ev = {
                "kind": "tool_result", "role": role, "ts": ts,
                "name": m.get("tool_name"), "call_id": m.get("tool_call_id"),
                "text": truncate(m["content"] or "", 8000),
            }
            idx = pending.pop(ev["call_id"], None)
            if idx is not None:
                events[idx]["result"] = ev["text"]
                events[idx]["result_ts"] = ts
            else:
                events.append(ev)
        else:
            events.append({"kind": role, "role": role, "ts": ts,
                           "text": truncate(m["content"] or "", 2000)})
    return sess, events


# ---------------------------------------------------------------- markdown

def to_markdown(sess, events):
    lines = [f"# Hermes Session `{sess['id']}`", ""]
    lines.append(f"- **Model:** {sess.get('model') or '?'}")
    lines.append(f"- **Started:** {fmt_ts(sess.get('started_at'))}")
    lines.append(f"- **CWD:** {sess.get('cwd') or '?'}")
    lines.append(f"- **Messages:** {sess.get('message_count')}  **Tool calls:** {sess.get('tool_call_count')}")
    lines.append(f"- **Tokens:** in={sess.get('input_tokens')} out={sess.get('output_tokens')} "
                 f"cache_read={sess.get('cache_read_tokens')}")
    if sess.get("estimated_cost_usd") is not None:
        lines.append(f"- **Est. cost:** ${sess['estimated_cost_usd']:.4f}")
    lines.append("")

    for ev in events:
        ts = fmt_ts(ev["ts"])
        if ev["kind"] == "user":
            lines += [f"## 👤 User  ({ts})", "", ev["text"], ""]
        elif ev["kind"] == "assistant_text":
            head = f"## 🤖 Assistant  ({ts})"
            if ev.get("tokens"):
                head += f"  · {ev['tokens']} tokens"
            lines += [head, "", ev["text"], ""]
            if ev.get("reasoning"):
                lines += ["<details><summary>reasoning</summary>", "", ev["reasoning"], "", "</details>", ""]
        elif ev["kind"] == "tool_call":
            lines += [f"### 🔧 {ev['name']}  ({ts})", "",
                      "Input:",
                      "```json",
                      json.dumps(ev["arguments"], ensure_ascii=False, indent=2)[:6000],
                      "```", ""]
            if ev["result"] is not None:
                lines += ["Result:", "", "```", ev["result"], "```", ""]
            else:
                lines += ["*(result not found in session store)*", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------- xml

def xml_escape(s):
    return html.escape(s or "", quote=True)


def to_xml(sess, events):
    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<hermes_session>"]
    out.append(f"  <metadata>")
    for k in ("id", "source", "model", "cwd", "git_branch", "message_count",
              "tool_call_count", "input_tokens", "output_tokens",
              "cache_read_tokens", "estimated_cost_usd"):
        out.append(f"    <{k}>{xml_escape(str(sess.get(k)))}</{k}>")
    out.append(f"    <started_at>{fmt_ts(sess.get('started_at'))}</started_at>")
    out.append("  </metadata>")
    out.append("  <timeline>")
    for i, ev in enumerate(events):
        out.append(f'    <event index="{i}" type="{xml_escape(ev["kind"])}" '
                   f'time="{fmt_ts(ev["ts"])}">')
        if ev["kind"] == "tool_call":
            out.append(f'      <tool name="{xml_escape(ev["name"])}" call_id="{xml_escape(ev["call_id"])}">')
            out.append(f"        <arguments>{xml_escape(json.dumps(ev['arguments'], ensure_ascii=False))}</arguments>")
            if ev["result"] is not None:
                out.append(f"        <result>{xml_escape(ev['result'])}</result>")
            out.append("      </tool>")
        elif ev.get("text"):
            out.append(f"      <text>{xml_escape(ev['text'])}</text>")
        if ev.get("reasoning"):
            out.append(f"      <reasoning>{xml_escape(ev['reasoning'])}</reasoning>")
        out.append("    </event>")
    out.append("  </timeline>")
    out.append("</hermes_session>")
    return "\n".join(out)


# --------------------------------------------------------------- html view

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>hermestrace · __SESSION_ID__</title>
<style>
  :root {
    --bg:#0d1117; --panel:#161b22; --border:#30363d; --text:#e6edf3;
    --dim:#8b949e; --accent:#58a6ff; --user:#3fb950; --tool:#d29922;
    --assistant:#a371f7; --danger:#f85149;
  }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,'Segoe UI',Roboto,'Noto Sans SC',sans-serif;
         background:var(--bg); color:var(--text); }
  header { position:sticky; top:0; z-index:10; background:var(--panel);
           border-bottom:1px solid var(--border); padding:12px 20px; }
  header h1 { margin:0 0 4px; font-size:18px; }
  header h1 code { color:var(--accent); font-size:14px; }
  .meta { color:var(--dim); font-size:12px; display:flex; gap:16px; flex-wrap:wrap; }
  .controls { display:flex; gap:8px; margin-top:8px; flex-wrap:wrap; align-items:center; }
  .controls button, .controls input { background:#21262d; color:var(--text);
    border:1px solid var(--border); border-radius:6px; padding:4px 10px; font-size:12px; cursor:pointer; }
  .controls input { cursor:text; width:200px; }
  .controls button.active { border-color:var(--accent); color:var(--accent); }
  .layout { display:flex; gap:0; }
  nav#toc { width:280px; flex:none; position:sticky; top:96px; height:calc(100vh - 96px);
            overflow-y:auto; border-right:1px solid var(--border); padding:8px; font-size:12px; }
  nav#toc .toc-item { padding:4px 8px; border-radius:6px; cursor:pointer;
                      white-space:nowrap; overflow:hidden; text-overflow:ellipsis; color:var(--dim); }
  nav#toc .toc-item:hover { background:#21262d; color:var(--text); }
  nav#toc .toc-item.user { color:var(--user); }
  nav#toc .toc-item.assistant_text { color:var(--assistant); }
  nav#toc .toc-item.tool_call { color:var(--tool); }
  main { flex:1; min-width:0; padding:20px 28px 120px; }
  .stats { display:flex; gap:12px; flex-wrap:wrap; margin-bottom:20px; }
  .stat { background:var(--panel); border:1px solid var(--border); border-radius:8px;
          padding:10px 14px; min-width:120px; }
  .stat .n { font-size:20px; font-weight:600; color:var(--accent); }
  .stat .l { font-size:11px; color:var(--dim); }
  .event { border:1px solid var(--border); border-radius:8px; margin-bottom:14px;
           background:var(--panel); overflow:hidden; }
  .event.hidden { display:none; }
  .ev-head { display:flex; align-items:center; gap:10px; padding:8px 12px;
             cursor:pointer; user-select:none; border-bottom:1px solid transparent; }
  .event.open .ev-head { border-bottom-color:var(--border); }
  .badge { font-size:11px; font-weight:600; padding:2px 8px; border-radius:10px; flex:none; }
  .badge.user { background:rgba(63,185,80,.15); color:var(--user); }
  .badge.assistant_text { background:rgba(163,113,247,.15); color:var(--assistant); }
  .badge.tool_call { background:rgba(210,153,34,.15); color:var(--tool); }
  .badge.tool_result { background:rgba(210,153,34,.08); color:var(--tool); }
  .ev-title { font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .ev-time { color:var(--dim); font-size:11px; margin-left:auto; flex:none; }
  .ev-body { display:none; padding:10px 14px; font-size:13px; }
  .event.open .ev-body { display:block; }
  pre { background:#0a0d12; border:1px solid var(--border); border-radius:6px;
        padding:10px; overflow-x:auto; font-size:12px; line-height:1.5;
        white-space:pre-wrap; word-break:break-word; max-height:420px; overflow-y:auto; }
  .label { font-size:11px; color:var(--dim); margin:8px 0 2px; text-transform:uppercase; letter-spacing:.5px; }
  .arrow { color:var(--dim); font-size:11px; margin-left:4px; }
  a { color:var(--accent); }
</style>
</head>
<body>
<header>
  <h1>hermestrace · <code>__SESSION_ID__</code></h1>
  <div class="meta">
    <span>model: __MODEL__</span><span>started: __STARTED__</span>
    <span>cwd: __CWD__</span><span>tools: __TOOLCOUNT__</span>
  </div>
  <div class="controls">
    <button data-f="all" class="active">全部</button>
    <button data-f="user">用户</button>
    <button data-f="assistant_text">助手回复</button>
    <button data-f="tool_call">工具调用</button>
    <button id="expand">展开全部</button>
    <button id="collapse">收起全部</button>
    <input id="search" placeholder="搜索...">
  </div>
</header>
<div class="layout">
  <nav id="toc"></nav>
  <main>
    <div class="stats">__STATS__</div>
    <div id="timeline">__EVENTS__</div>
  </main>
</div>
<script>
const events = [...document.querySelectorAll('.event')];
const toc = document.getElementById('toc');
events.forEach((ev, i) => {
  ev.querySelector('.ev-head').onclick = () => ev.classList.toggle('open');
  const item = document.createElement('div');
  item.className = 'toc-item ' + ev.dataset.kind;
  item.textContent = (i+1) + '. ' + ev.dataset.title;
  item.title = ev.dataset.title;
  item.onclick = () => {
    ev.classList.add('open');
    ev.scrollIntoView({behavior:'smooth', block:'start'});
  };
  toc.appendChild(item);
});
document.querySelectorAll('.controls button[data-f]').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.controls button[data-f]').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const f = btn.dataset.f;
    events.forEach(ev => {
      ev.classList.toggle('hidden', f !== 'all' && ev.dataset.kind !== f);
    });
  };
});
document.getElementById('expand').onclick = () => events.forEach(e => e.classList.add('open'));
document.getElementById('collapse').onclick = () => events.forEach(e => e.classList.remove('open'));
document.getElementById('search').oninput = e => {
  const q = e.target.value.toLowerCase();
  events.forEach(ev => {
    ev.classList.toggle('hidden', q && !ev.textContent.toLowerCase().includes(q));
  });
};
</script>
</body>
</html>
"""


def to_html(sess, events):
    stats = []
    n_tools = sum(1 for e in events if e["kind"] == "tool_call")
    n_calls = sum(1 for e in events if e["kind"] == "assistant_text")
    used = {}
    for e in events:
        if e["kind"] == "tool_call":
            used[e["name"]] = used.get(e["name"], 0) + 1
    stats.append(f'<div class="stat"><div class="n">{len(events)}</div><div class="l">事件总数</div></div>')
    stats.append(f'<div class="stat"><div class="n">{n_tools}</div><div class="l">工具调用</div></div>')
    stats.append(f'<div class="stat"><div class="n">{n_calls}</div><div class="l">助手轮次</div></div>')
    for name, cnt in sorted(used.items(), key=lambda kv: -kv[1])[:6]:
        stats.append(f'<div class="stat"><div class="n">{cnt}×</div><div class="l">{html.escape(name)}</div></div>')

    cards = []
    for i, ev in enumerate(events):
        kind = ev["kind"]
        ts = fmt_ts(ev["ts"])
        if kind == "user":
            title = (ev["text"] or "")[:80].replace("\n", " ")
            body = f'<div class="label">消息</div><pre>{html.escape(ev["text"])}</pre>'
            badge = "用户"
        elif kind == "assistant_text":
            title = (ev["text"] or "")[:80].replace("\n", " ") or "(空)"
            body = f'<div class="label">回复</div><pre>{html.escape(ev["text"])}</pre>'
            if ev.get("reasoning"):
                body += f'<div class="label">推理 reasoning</div><pre>{html.escape(ev["reasoning"])}</pre>'
            badge = "助手"
        elif kind == "tool_call":
            title = f"{ev['name']}({json.dumps(ev['arguments'], ensure_ascii=False)[:60]})"
            args = json.dumps(ev["arguments"], ensure_ascii=False, indent=2)
            body = f'<div class="label">输入参数</div><pre>{html.escape(args[:6000])}</pre>'
            if ev["result"] is not None:
                body += f'<div class="label">结果 ↓</div><pre>{html.escape(ev["result"])}</pre>'
            else:
                body += '<div class="label">结果</div><pre>(未在会话库中找到)</pre>'
            badge = f"🔧 {html.escape(ev['name'])}"
        else:
            title = (ev.get("text") or kind)[:80]
            body = f'<pre>{html.escape(ev.get("text") or "")}</pre>'
            badge = kind
        cards.append(
            f'<div class="event" data-kind="{kind}" data-title="{html.escape(title)}">'
            f'<div class="ev-head"><span class="badge {kind}">{badge}</span>'
            f'<span class="ev-title">{html.escape(title)}</span>'
            f'<span class="ev-time">#{i+1} · {ts}</span></div>'
            f'<div class="ev-body">{body}</div></div>'
        )

    page = HTML_TEMPLATE
    page = page.replace("__SESSION_ID__", html.escape(sess["id"]))
    page = page.replace("__MODEL__", html.escape(str(sess.get("model") or "?")))
    page = page.replace("__STARTED__", fmt_ts(sess.get("started_at")))
    page = page.replace("__CWD__", html.escape(str(sess.get("cwd") or "?")))
    page = page.replace("__TOOLCOUNT__", str(sess.get("tool_call_count") or 0))
    page = page.replace("__STATS__", "\n".join(stats))
    page = page.replace("__EVENTS__", "\n".join(cards))
    return page


# -------------------------------------------------------------------- CLI

def cmd_list(args):
    con = connect()
    rows = con.execute(
        "SELECT id, model, title, started_at, message_count, tool_call_count "
        "FROM sessions ORDER BY started_at DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    for r in rows:
        print(f"{r['id']}  {fmt_ts(r['started_at'])}  msgs={r['message_count']:>3} "
              f"tools={r['tool_call_count']:>3}  {r['model'] or ''}  {r['title'] or ''}")


def do_export(args, session_id):
    con = connect()
    sess, events = build_trace(con, session_id)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    formats = ["md", "xml", "html"] if args.format == "all" else [args.format]
    written = []
    if "md" in formats:
        p = outdir / f"{session_id}.md"
        p.write_text(to_markdown(sess, events), encoding="utf-8")
        written.append(p)
    if "xml" in formats:
        p = outdir / f"{session_id}.xml"
        p.write_text(to_xml(sess, events), encoding="utf-8")
        written.append(p)
    if "html" in formats:
        p = outdir / f"{session_id}.html"
        p.write_text(to_html(sess, events), encoding="utf-8")
        written.append(p)
    for p in written:
        print(f"wrote {p}  ({p.stat().st_size:,} bytes)")


def cmd_latest(args):
    con = connect()
    row = con.execute("SELECT id FROM sessions ORDER BY started_at DESC LIMIT 1").fetchone()
    if not row:
        sys.exit("no sessions found")
    print(f"latest session: {row['id']}")
    do_export(args, row["id"])


def cmd_watch(args):
    """Re-export a session periodically so the HTML stays fresh while a task runs.

    The recording itself needs no helper: Hermes writes every message and tool
    call into state.db in real time. This command only regenerates the
    rendered views on an interval. Repeated 'wrote ...' lines go to stdout,
    so run it with background=true and refresh the HTML in a browser to see
    the task's tool-use trace grow live.
    """
    session_id = args.session_id
    if session_id in (None, "latest"):
        row = connect().execute(
            "SELECT id FROM sessions ORDER BY started_at DESC LIMIT 1").fetchone()
        if not row:
            sys.exit("no sessions found yet — start the task first")
        session_id = row["id"]
        print(f"watching latest session: {session_id}", flush=True)

    class A:  # shim so do_export can be reused unchanged
        pass
    a = A()
    a.format, a.outdir = args.format, args.outdir

    last_len = -1
    while True:
        try:
            con = connect()
            sess, events = build_trace(con, session_id)
            con.close()
            if len(events) != last_len:
                do_export(a, session_id)
                try:
                    from dashboard import render_dashboard
                    p, _ = render_dashboard()
                    print(f"[{fmt_ts(time.time())}] {len(events)} events + {p.name}", flush=True)
                except Exception as de:
                    print(f"[{fmt_ts(time.time())}] {len(events)} events (dashboard failed: {de})", flush=True)
                last_len = len(events)
        except SystemExit:
            raise
        except Exception as e:  # DB locked / session rotated — keep watching
            print(f"[watch] {type(e).__name__}: {e}", flush=True)
        time.sleep(args.interval)


def cmd_dashboard(args):
    from dashboard import render_dashboard
    sid = getattr(args, "session_id", None)
    session_filter = [sid] if sid and sid not in ("latest",) else None
    p, d = render_dashboard(session_filter=session_filter)
    print(f"wrote {p} ({p.stat().st_size:,} bytes)")
    print(f"tasks={len(d['tasks'])} requests={sum(t['request_count'] for t in d['tasks'])} "
          f"runs={sum(t['run_count'] for t in d['tasks'])}")


def main():
    ap = argparse.ArgumentParser(prog="hermestrace", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="list sessions")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("export", help="export one session")
    p.add_argument("session_id")
    p.add_argument("--format", choices=["md", "xml", "html", "all"], default="all")
    p.add_argument("--outdir", default=str(HERMES_HOME / "hermestrace"))
    p.set_defaults(func=lambda a: do_export(a, a.session_id))

    p = sub.add_parser("latest", help="export the most recent session")
    p.add_argument("--format", choices=["md", "xml", "html", "all"], default="all")
    p.add_argument("--outdir", default=str(HERMES_HOME / "hermestrace"))
    p.set_defaults(func=cmd_latest)

    p = sub.add_parser("dashboard", help="render the 4-tab dashboard (Sessions/Requests/Context/Runs)")
    p.add_argument("--session-id", default=None, help="limit to one session")
    p.add_argument("--all-tasks", action="store_true", help="include every session")
    p.set_defaults(func=cmd_dashboard, session_id=None, all_tasks=True)

    p = sub.add_parser("watch", help="keep re-exporting a session while a task runs")
    p.add_argument("session_id", nargs="?", default="latest",
                   help="session id, or 'latest' (default)")
    p.add_argument("--format", choices=["md", "xml", "html", "all"], default="html")
    p.add_argument("--outdir", default=str(HERMES_HOME / "hermestrace"))
    p.add_argument("--interval", type=int, default=5, help="seconds between exports")
    p.set_defaults(func=cmd_watch)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
