"""hermestrace dashboard data builder.

Gathers everything the four-tab dashboard needs and renders
dashboard_template.html into a single self-contained HTML file.

Data sources:
  * ~/.hermes/state.db      -> sessions, messages (task separation + timeline)
  * ~/.hermes/logs/agent.log -> per-request in/out/cache/latency, tool durations
  * `hermes prompt-size --json` -> fixed context budget (system prompt,
      skills index, tool schemas, memory) + per-skill / per-toolset breakdown
  * ~/.hermes/context_length_cache.yaml -> context window size
"""

import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
DB_PATH = HERMES_HOME / "state.db"
AGENT_LOG = HERMES_HOME / "logs" / "agent.log"
TEMPLATE = Path(__file__).parent / "dashboard_template.html"

CHARS_PER_TOKEN = 4

# ----------------------------------------------------------------- helpers

def _connect():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


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
        out.append({"call_id": c.get("call_id") or c.get("id"),
                    "name": fn.get("name") or "?", "arguments": args})
    return out


def _tok(chars):
    return (chars or 0) // CHARS_PER_TOKEN


# ------------------------------------------------------------ agent.log

_API_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \w+ "
    r"(?:\[(?P<sid>[^\]]+)\] )?agent\.conversation_loop: API call #(?P<n>\d+): "
    r"model=(?P<model>\S+) provider=(?P<provider>\S+) "
    r"in=(?P<in>\d+) out=(?P<out>\d+) total=(?P<total>\d+) "
    r"latency=(?P<latency>[\d.]+)s cache=(?P<cread>\d+)/(?P<cin>\d+) "
    r"\((?P<cpct>\d+)%\)"
)
_TOOL_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \w+ "
    r"(?:\[(?P<sid>[^\]]+)\] )?agent\.tool_executor: tool (?P<name>\S+) "
    r"completed \((?P<dur>[\d.]+)s, (?P<chars>\d+) chars\)"
)


def parse_agent_log(limit_requests=2000):
    """Return (api_requests, tool_runs) sorted by time."""
    requests, tool_runs = [], []
    if not AGENT_LOG.exists():
        return requests, tool_runs
    try:
        text = AGENT_LOG.read_text(errors="replace")
    except OSError:
        return requests, tool_runs
    last_sid = None
    for line in text.splitlines():
        m = _API_RE.match(line)
        if m:
            last_sid = m.group("sid") or last_sid
            requests.append({
                "ts": _log_ts(m.group("ts")), "sid": m.group("sid") or last_sid,
                "n": int(m.group("n")), "model": m.group("model"),
                "provider": m.group("provider"), "in": int(m.group("in")),
                "out": int(m.group("out")), "total": int(m.group("total")),
                "latency": float(m.group("latency")),
                "cache_read": int(m.group("cread")),
                "cache_pct": int(m.group("cpct")),
            })
            continue
        m = _TOOL_RE.match(line)
        if m:
            last_sid = m.group("sid") or last_sid
            tool_runs.append({
                "ts": _log_ts(m.group("ts")), "sid": m.group("sid") or last_sid,
                "name": m.group("name"), "dur": float(m.group("dur")),
                "chars": int(m.group("chars")),
            })
    if len(requests) > limit_requests:
        requests = requests[-limit_requests:]
    return requests, tool_runs


def _log_ts(s):
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))


# ------------------------------------------------------------ prompt-size

def get_prompt_size():
    try:
        out = subprocess.run(
            ["hermes", "prompt-size", "--json"],
            capture_output=True, text=True, timeout=60,
        ).stdout
        return json.loads(out)
    except Exception:
        return None


def get_context_window(model):
    cache = HERMES_HOME / "context_length_cache.yaml"
    assumed = True
    val = None
    if cache.exists():
        for line in cache.read_text(errors="replace").splitlines():
            if ":" in line and line.strip().endswith(tuple("0123456789")):
                k, _, v = line.rpartition(":")
                if model and model.split("@")[0] in k:
                    try:
                        val, assumed = int(v.strip()), False
                    except ValueError:
                        pass
    return val or 131072, assumed


# ------------------------------------------------------------ context cats

def build_fixed_categories(ps):
    """Fixed (per-session) context categories, tokens via chars/4 —
    mirrors hermes agent/context_breakdown.py."""
    if not ps:
        return [], {}, [], []
    sys_chars = ps["system_prompt"]["chars"]
    skills_chars = ps["skills_index"]["chars"]
    mem_chars = ps["memory"]["chars"] + ps["user_profile"]["chars"]
    # rules = the "context" section (AGENTS.md / cwd files)
    rules_chars = 0
    for name, chars, _b in ps.get("sections", []):
        if name.startswith("context"):
            rules_chars = chars
    tools_bytes = ps["tools"]["json_bytes"]
    # split toolsets: delegation = subagent definitions, rest = tools;
    # mcp toolsets (name starts with mcp) count as MCP
    subagent_b = 0
    mcp_b = 0
    tool_b = 0
    for t in ps.get("toolsets_breakdown", []):
        name = t["toolset"]
        if name.startswith("mcp"):
            mcp_b += t["json_bytes"]
        elif name == "delegation":
            subagent_b += t["json_bytes"]
        else:
            tool_b += t["json_bytes"]
    if not ps.get("toolsets_breakdown"):
        tool_b = tools_bytes
    cats = [
        {"id": "system_prompt",   "name": "System prompt",   "tokens": _tok(max(sys_chars - skills_chars - rules_chars - mem_chars, 0)), "color": "#5b9dff"},
        {"id": "tool_definitions", "name": "Tool definitions", "tokens": _tok(tool_b),      "color": "#bc8cff"},
        {"id": "rules",           "name": "Rules (AGENTS.md)", "tokens": _tok(rules_chars), "color": "#e3a008"},
        {"id": "skills",          "name": "Skills index",     "tokens": _tok(skills_chars), "color": "#39c5cf"},
        {"id": "mcp",             "name": "MCP",              "tokens": _tok(mcp_b),       "color": "#ff7ab6"},
        {"id": "subagent_definitions", "name": "Subagent defs", "tokens": _tok(subagent_b), "color": "#d4c04e"},
        {"id": "memory",          "name": "Memory / profile", "tokens": _tok(mem_chars),   "color": "#f85149"},
    ]
    raw = dict(system_prompt=ps["system_prompt"], skills_index=ps["skills_index"],
               memory=ps["memory"], user_profile=ps["user_profile"], tools=ps["tools"])
    skills = [{"name": s["name"],
               "index_tokens": _tok(s.get("index_line_bytes")),
               "skill_md_tokens": _tok(s.get("skill_md_bytes"))}
              for s in ps.get("skills_breakdown", [])]
    toolsets = [{"toolset": t["toolset"], "tool_count": t["tool_count"],
                 "schema_tokens": _tok(t["json_bytes"])}
                for t in ps.get("toolsets_breakdown", [])]
    return cats, raw, skills, toolsets


# ------------------------------------------------------------ sessions

def build_events(msgs):
    """Flatten one session's messages into timeline events with tool
    call/result pairing (call_id join)."""
    events, pending = [], {}
    for m in msgs:
        role, ts = m["role"], m["timestamp"]
        if role == "assistant":
            text = (m["content"] or "").strip()
            calls = parse_tool_calls(m.get("tool_calls"))
            if text:
                events.append({"kind": "assistant_text", "ts": ts, "text": text[:8000],
                               "reasoning": (m.get("reasoning_content") or "").strip()[:4000]})
            for c in calls:
                pending[c["call_id"]] = len(events)
                events.append({"kind": "tool_call", "ts": ts, "name": c["name"],
                               "call_id": c["call_id"], "arguments": c["arguments"],
                               "result": None})
        elif role == "user":
            events.append({"kind": "user", "ts": ts, "text": (m["content"] or "")[:8000]})
        elif role == "tool":
            idx = pending.pop(m.get("tool_call_id"), None)
            body = (m["content"] or "")[:8000]
            if idx is not None:
                events[idx]["result"] = body
            else:
                events.append({"kind": "tool_result", "ts": ts, "text": body})
    return events


def build_runs(msgs, tool_durs):
    """A run = one user message through all following assistant/tool
    messages, up to (excluding) the next user message."""
    runs, cur = [], None
    for m in msgs:
        role, ts = m["role"], m["timestamp"]
        if role == "user":
            if cur:
                runs.append(cur)
            cur = {"ts": ts, "trigger": (m["content"] or "").strip().replace("\n", " ")[:200],
                   "steps": [], "tools": 0}
        elif cur is not None:
            if role == "assistant":
                text = (m["content"] or "").strip()
                if text:
                    cur["steps"].append({"kind": "assistant_text", "ts": ts,
                                         "text": text.replace("\n", " ")[:400]})
                for c in parse_tool_calls(m.get("tool_calls")):
                    dur = _match_dur(tool_durs, c["name"], ts)
                    cur["steps"].append({"kind": "tool_call", "ts": ts, "name": c["name"],
                                         "args": json.dumps(c["arguments"], ensure_ascii=False)[:400],
                                         "dur": dur})
                    cur["tools"] += 1
            elif role == "tool" and not cur["steps"]:
                cur["steps"].append({"kind": "tool_result", "ts": ts,
                                     "text": (m["content"] or "")[:300]})
    if cur:
        runs.append(cur)
    return runs


def _match_dur(durs, name, ts):
    """Nearest log-measured duration for this tool near this message ts."""
    best, best_dt = None, 30.0
    for d in durs:
        if d["name"] != name:
            continue
        dt = abs(d["ts"] - ts)
        if dt < best_dt:
            best, best_dt = d["dur"], dt
    return best


def tool_stats(tool_durs):
    agg = {}
    for d in tool_durs:
        e = agg.setdefault(d["name"], {"name": d["name"], "count": 0,
                                       "total_s": 0.0, "chars": 0})
        e["count"] += 1
        e["total_s"] += d["dur"]
        e["chars"] += d["chars"]
    return sorted(agg.values(), key=lambda x: -x["count"])


# ------------------------------------------------------------ main build

def build_dashboard_data(session_filter=None):
    con = _connect()
    sessions = [dict(r) for r in con.execute(
        "SELECT id, title, source, model, started_at, ended_at, message_count, "
        "tool_call_count, input_tokens, output_tokens, cache_read_tokens, "
        "estimated_cost_usd, cwd FROM sessions ORDER BY started_at")]
    reqs_all, tools_all = parse_agent_log()

    ps = get_prompt_size()
    fixed_cats, ps_raw, skills, toolsets = build_fixed_categories(ps or {})
    if not ps:
        fixed_cats = [{"id": "system_prompt", "name": "System prompt", "tokens": 0, "color": "#5b9dff"}]
        ps_raw, skills, toolsets = {}, [], []

    model = None
    tasks = []
    for s in sessions:
        if session_filter and s["id"] not in session_filter:
            continue
        model = model or s.get("model")
        msgs = [dict(m) for m in con.execute(
            "SELECT role, content, tool_calls, tool_name, tool_call_id, "
            "timestamp, reasoning_content FROM messages "
            "WHERE session_id=? AND active=1 ORDER BY timestamp, id", (s["id"],))]
        reqs = [r for r in reqs_all if r["sid"] == s["id"]]
        for i, r in enumerate(reqs, 1):
            r.setdefault("n", i)
        tdurs = [t for t in tools_all if t["sid"] == s["id"]]
        # tool stats: prefer log-measured; fall back to DB call counts
        tstats = tool_stats(tdurs)
        seen = {t["name"] for t in tstats}
        db_calls = {}
        for m in msgs:
            for c in parse_tool_calls(m.get("tool_calls")):
                db_calls[c["name"]] = db_calls.get(c["name"], 0) + 1
        for name, cnt in db_calls.items():
            if name not in seen:
                tstats.append({"name": name, "count": cnt, "total_s": 0.0, "chars": 0})
        conv_est = sum(len(m.get("content") or "") + len(m.get("tool_calls") or "")
                       for m in msgs) // CHARS_PER_TOKEN

        runs = build_runs(msgs, tdurs)
        tasks.append({
            "id": s["id"], "title": s.get("title"), "source": s.get("source"),
            "model": s.get("model"), "started_at": s["started_at"],
            "cwd": s.get("cwd"), "message_count": s["message_count"],
            "tool_call_count": s["tool_call_count"],
            "request_count": len(reqs),
            "run_count": len(runs),
            "tokens": {"in": s.get("input_tokens"), "out": s.get("output_tokens"),
                       "cache": s.get("cache_read_tokens"),
                       "cost": s.get("estimated_cost_usd")},
            "events": build_events(msgs),
            "runs": runs,
            "tool_stats": tstats,
            "requests": reqs,
            "conv_est": conv_est,
        })
    con.close()

    import datetime
    # context window can differ per model — resolve for every model in scope
    ctx_windows = {}
    for m in {s.get("model") for s in sessions if s.get("model")} | ({model} if model else set()):
        v, a = get_context_window(m or "")
        ctx_windows[m] = {"value": v, "assumed": a}
    primary = (tasks[0]["model"] if tasks else model) or ""
    win, assumed = get_context_window(primary)
    return {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "model": model,
        "tasks": tasks,
        "ctx_fixed": fixed_cats,
        "ctx_window": win,
        "ctx_window_assumed": assumed,
        "ctx_windows": ctx_windows,
        "ps_raw": ps_raw,
        "skills": skills,
        "toolsets": toolsets,
    }


def render_dashboard(out_path=None, session_filter=None):
    data = build_dashboard_data(session_filter)
    tpl = TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = tpl.replace("__HERMESTRACE_DATA__", payload)
    out_path = Path(out_path or (HERMES_HOME / "hermestrace" / "dashboard.html"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path, data


if __name__ == "__main__":
    p, d = render_dashboard()
    print(f"wrote {p} ({p.stat().st_size:,} bytes)")
    print(f"tasks={len(d['tasks'])} "
          f"requests={sum(t['request_count'] for t in d['tasks'])} "
          f"runs={sum(t['run_count'] for t in d['tasks'])}")
