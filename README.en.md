# hermestrace

[简体中文](README.md) | [English](README.en.md)

Session tracing and visualization for Hermes Agent — the Hermes counterpart of
cctrace for Claude Code. Each task (session) is kept separate across four tabs:
**Sessions / Requests / Context / Runs**, with charts showing context usage by
skills, MCP, tools, system prompts, and more.

## Public Demo and Downloads

Open the [hermestrace dashboard](https://zyw313313-collab.github.io/hermestrace/).

- The public demo uses synthetic data bundled with the repository. It supports all four tabs, task switching, timeline search, and expandable events.
- **Download offline dashboard** (`下载离线界面`): Save an HTML file containing the current data, selected task, and active tab. Double-click the file to browse offline.
- **Download project ZIP** (`下载项目 ZIP`): Download the Python tools, dashboard template, sample data, and build script to inspect Hermes data on your own computer.

GitHub Pages deploys automatically through `.github/workflows/pages.yml` whenever
changes are pushed to `main`. The build reads demo data only from `site/demo.json`;
it does not read local session databases or logs.

Preview the public demo locally:

```bash
python scripts/build_site.py
python -m http.server 8000 --directory _site
```

Open `http://localhost:8000` in your browser. To view real sessions, run Hermes
locally first, then generate a dashboard using the commands below. Set the
`HERMES_HOME` environment variable to use a custom data directory.

## No Reverse Engineering Needed: Hermes Data Is Stored Locally in Plaintext

| Tab | Data source | Contents |
|---|---|---|
| **Sessions** | `sessions` / `messages` tables in `~/.hermes/state.db` | One session per task; messages, tool calls as JSON, tool results, and reasoning |
| **Requests** | `~/.hermes/logs/agent.log` | Each provider API call: input/output tokens, cache usage, latency, model, and provider |
| **Context** | `hermes prompt-size --json` + `context_length_cache.yaml` | Fixed overhead for the system prompt, skills index, tool schemas, and memory; breakdowns by skill/toolset; estimates scaled to provider-reported input tokens |
| **Runs** | `state.db` (split by user messages) + `agent.log` (measured durations) | One run covers the agent loop from one user message to the next, including tool calls and their durations |

Field mapping from cctrace to hermestrace:

| cctrace (Claude Code) | hermestrace (Hermes) |
|---|---|
| `~/.claude/projects/**/*.jsonl` | `messages` table in `state.db` |
| `type: "user"` / `"assistant"` | `role` column |
| `tool_use` blocks | `tool_calls` column (JSON array, paired by `call_id`) |
| `tool_use_result` | Rows with `role='tool'`, linked through `tool_call_id` |
| Thinking blocks | `reasoning` / `reasoning_content` columns |
| No direct equivalent | `API call #N` lines in `agent.log`, shown in the Requests tab |

## Usage

```bash
# Generate the four-tab dashboard as a self-contained HTML file.
# Double-click the generated file to open it.
python3 hermestrace.py dashboard

# Show a single task.
python3 hermestrace.py dashboard --session-id <SESSION_ID>

# Refresh exports while a task runs, checking every five seconds.
# Files are rewritten when the timeline changes.
python3 hermestrace.py watch

# List sessions or export a session as Markdown, XML, and standalone HTML.
python3 hermestrace.py list
python3 hermestrace.py export <SESSION_ID> --format all
python3 hermestrace.py latest
```

Output defaults to `~/.hermes/hermestrace/`. The main dashboard is
`dashboard.html`.

## Dashboard Tabs

- **Sessions** — Task cards show the model, request count, tool calls, tokens, and estimated cost. Click a card to scope all tabs to that task. Selecting a single task reveals its searchable, filterable timeline with expandable events.
- **Requests** — Stacked charts show context growth across requests, separating fixed overhead from conversation history. Additional charts show input/output tokens, latency, and cache hit rates. The request table includes request number, time, model, input/output tokens, cache usage, and latency.
- **Context** — A donut chart breaks down the system prompt, tool schemas, rules, skills index, MCP, subagent definitions, memory, and conversation history. A 20 × 5 grid shows context window usage, with each cell representing 1% of the window. Bar charts show skill sizes and toolset/MCP schema usage, and identify skills loaded by the selected task.
- **Runs** — Expand each run to inspect the sequence of assistant responses and tool calls with measured durations. Charts rank tools by call count and average execution time.

## Context Estimation

Token estimates use the same characters-divided-by-four heuristic as Hermes'
`agent/context_breakdown.py`. Fixed overhead comes from
`hermes prompt-size --json`. A single scaling factor aligns the estimates with
the provider-reported `in=` total from API responses, following the approach
used by pi-context-inspector.

The context window is read from `context_length_cache.yaml` for each task's
model. If no value is found, the dashboard labels it as an assumed value.

References: Hermes' `agent/context_breakdown.py`, pi-context-inspector (token
scaling and usage grid), context-treemap (MCP/skills breakdowns), and cctrace
(export formats).

## Classroom Workflow with Two Hermes Agents

- **Agent A (executor):** Run a task normally. Hermes records its activity automatically; no extra recording steps are required.
- **Agent B (observer):** Read the Hermes source in `~/.hermes/hermes-agent/` to understand the workflow, then run `python3 hermestrace.py watch` to refresh Agent A's dashboard every five seconds.
- Open `~/.hermes/hermestrace/dashboard.html` to inspect Agent A's requests, context changes, and tool calls across all four tabs as the task progresses — a visual demonstration of tool use.

## Files

```text
hermestrace.py              # CLI: list / export / latest / watch / dashboard
dashboard.py               # Aggregate state.db, agent.log, and prompt-size data
dashboard_template.html    # Dashboard template with embedded data; single-file output
site/demo.json             # Synthetic data for the public demo
scripts/build_site.py      # Build the GitHub Pages site and source download ZIP
.github/workflows/pages.yml # Automatically deploy the public dashboard
```
