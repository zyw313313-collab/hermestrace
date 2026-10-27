# hermestrace

Hermes Agent 会话追踪与可视化 —— cctrace 之于 Claude Code 的 Hermes 等价物。
每个 task(session)完全分开,四大分栏:**Sessions / Requests / Context / Runs**,
含 context 构成(skills、MCP、tools、system prompt…)的占用图表。

## 不需要逆向:Hermes 数据全部明文在本地

| 分栏 | 数据源 | 内容 |
|---|---|---|
| **Sessions** | `~/.hermes/state.db` `sessions`/`messages` 表 | 每个 task 一条 session;消息、tool_calls JSON、工具结果、reasoning |
| **Requests** | `~/.hermes/logs/agent.log` | 每次 provider API 调用:`in/out/cache/latency/model/provider` |
| **Context** | `hermes prompt-size --json` + `context_length_cache.yaml` | system prompt、skills 索引、tool schemas、memory 的常驻开销;按 skill/toolset 细分;按 provider 上报 in= 缩放 |
| **Runs** | `state.db`(按用户消息切分)+ `agent.log`(实测耗时) | 一次 run = 一条用户输入到下一条之间的完整 agent 循环;每步工具调用及耗时 |

字段对应关系(cctrace → hermestrace):

| cctrace (Claude Code) | hermestrace (Hermes) |
|---|---|
| `~/.claude/projects/**/*.jsonl` | `state.db` 的 `messages` 表 |
| `type: "user"/"assistant"` | `role` 列 |
| `tool_use` 块 | `tool_calls` 列(JSON 数组,`call_id` 配对) |
| `tool_use_result` | `role='tool'` 行,`tool_call_id` 关联 |
| thinking blocks | `reasoning` / `reasoning_content` 列 |
| (无) | `agent.log` 的 `API call #N` 行 → Requests 分栏 |

## 用法

```bash
# 生成四分栏仪表盘(单文件 HTML,自包含,可直接双击打开)
python3 hermestrace.py dashboard

# 只看某一个 task
python3 hermestrace.py dashboard --session-id <SESSION_ID>

# 任务运行时后台实时刷新(每 5 秒,数据变化才重写;同时更新时间线)
python3 hermestrace.py watch

# 会话列表 / 单会话导出(md + xml + 独立时间线 html)
python3 hermestrace.py list
python3 hermestrace.py export <SESSION_ID> --format all
python3 hermestrace.py latest
```

输出默认在 `~/.hermes/hermestrace/`:`dashboard.html` 是主仪表盘。

## 仪表盘分栏

- **Sessions** — 任务卡片(模型、请求数、工具调用、token、成本);点击卡片把
  全站范围切到该 task(任务彼此分开);选中单任务后显示可展开/搜索/筛选的会话时间线
- **Requests** — 上下文随请求增长的堆叠图(固定开销 vs 会话历史)、in/out/latency
  折线、缓存命中率曲线、请求明细表(#、时间、model、in、out、cache、延迟)
- **Context** — 分类甜甜圈图(system prompt / 工具 schema / rules / skills 索引 /
  MCP / subagent / memory / 会话历史)、20×5 窗口占用格(每格 1%,参考
  pi-context-inspector)、skills 柱状图(index 常驻 vs SKILL.md 按需)、
  toolsets/MCP schema 占用柱状图,并标注本任务实际加载过的 skill
- **Runs** — run 列表(展开看每一步:用户 → 助手 → 🔧工具+实测耗时)、
  工具调用次数排行、工具平均耗时排行

## Context 估算方法

与 Hermes 内部 `agent/context_breakdown.py` 同一套启发式(字符÷4),
固定开销取自 `hermes prompt-size --json`,再乘单一缩放系数对齐 provider
在 API 响应里上报的 `in=` 总量(参考 pi-context-inspector 的缩放做法)。
context window 按每个 task 自己的模型从 `context_length_cache.yaml` 读取,
读不到则标记"假设值"。

GitHub 参考:Hermes `agent/context_breakdown.py`、pi-context-inspector
(token 缩放 + 占用格)、context-treemap(MCP/skills 占用图)、cctrace(导出格式)。

## 课程作业(两个 Hermes agent)的用法

- **Agent A(执行者)**:正常执行 task,操作自动落库,无需任何额外动作
- **Agent B(观察者)**:读 Hermes 源码(`~/.hermes/hermes-agent/`)理解工作流,
  然后 `python3 hermestrace.py watch` —— A 的仪表盘每 5 秒刷新
- 打开 `~/.hermes/hermestrace/dashboard.html`,即可在四个分栏里实时回放 A 的
  请求、上下文变化与工具调用 —— 正是 "Tool use" 主题的可视化演示

## 文件

```
hermestrace.py            # CLI:list / export / latest / watch / dashboard
dashboard.py              # 数据聚合:state.db + agent.log + prompt-size
dashboard_template.html   # 仪表盘模板(数据内嵌,单文件输出)
```
