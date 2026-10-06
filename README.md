# agent-memory

*Deutsche Fassung: [README.de.md](README.de.md)*

An MCP server that takes the load off local models in LM Studio: memory,
sub-agents, skills, smoke tests, context hand-over — and a gateway to all your
other MCP servers without their tool descriptions eating the context window.

It does not just hand back instructions (*"you are the executor!"*), it
**does the work itself**: starts sub-agents, runs commands, compresses
context, forwards calls to other tools.

---

## The problem in one table

Every server listed in `mcp.json` puts its tool descriptions into **every
single message**. Measured on 10 Aug 2026 on a machine with 13 servers —
counted by LM Studio itself, not estimated:

| registered in `mcp.json` | tools | characters | tokens |
|---|---:|---:|---:|
| all servers | 160 | 140,247 | **35,630** |
| only `agent-memory` | 26 | 18,266 | **5,219** |

**30,411 fewer tokens on every message — 85 %.**

What that means for a context window:

| window | all servers | only agent-memory |
|---|---:|---:|
| 8k | 435 % — impossible | 64 % |
| 16k | 217 % — impossible | 32 % |
| 32k | 109 % — impossible | 16 % |

### Not a calculation — a real test

Both models loaded with a fixed 16k window (everything else unloaded) and
asked to write until nothing more fits:

| 16k window | all 160 tools | only agent-memory |
|---|---|---|
| `qwen3-4b-2507` | **not a single token** | 11,049 tokens in 100 s |
| `qwen3.6-35b-a3b` | **not a single token** | 11,156 tokens in 1,463 s |

The left column is a rejected request, not a number game:

```
400: request (35719 tokens) exceeds the available context size (16384 tokens)
```

Side finding that drives the choice of sub-agent model: for the same ~11,000
tokens the 4B needed **100 s**, the 35B-A3B **1,463 s** — a factor of 15.

Reproduce on your own machine:

```powershell
uv run python scripts/benchmark_kontext.py
uv run python scripts/benchmark_kontext.py --volllaufen   # takes minutes
```

To be fair: the server's own tools still cost ~5,200 tokens. Their
descriptions are deliberately detailed because they teach a small model
*when* to use what — that is the part that pays off.

### Context is only half of it

The real gain comes the **second time** a hard task comes up: `recall()`
already has the solution instead of a model working it out again.

Same question, two ways — measured with the bundled example records
(`scripts/benchmark_gedaechtnis.py`):

> *"A background job makes no progress even though the GPU has 24 GB. Why,
> and what helps?"*

| way | tokens | time | model rounds |
|---|---:|---:|---:|
| **remember** — `recall` + `open_record` | 925 | 2.2 s | 0 |
| **work it out** — sub-agent reads the files | 1,733 | 18.1 s | 2 |

The question shares almost no words with the stored trigger. It is found
anyway with score **0.756** — semantic search, not keyword luck. The
remembered answer was also the **better** one: cause plus four steps, while
the sub-agent returned a list of lines from `config.json`.

---

## Setup

**1. Install dependencies** (needs [uv](https://docs.astral.sh/uv/)):

```powershell
cd agent-memory-mcp
uv sync
```

**2. Check that the server starts** exactly the way LM Studio starts it, as a
child process over stdio:

```powershell
uv run python tests/stdio_check.py
```

Nine checks, none of them needs a model.

**3. Register it in LM Studio** — *Program → Install → Edit mcp.json*, or edit
the file directly (`%USERPROFILE%\.lmstudio\mcp.json` on Windows,
`~/.lmstudio/mcp.json` on macOS/Linux):

```json
{
  "mcpServers": {
    "agent-memory": {
      "command": "C:/Users/YOUR-NAME/AppData/Local/Programs/Python/Python311/Scripts/uv.exe",
      "args": ["--directory", "C:/Users/YOUR-NAME/MCP-Server/agent-memory-mcp", "run", "server.py"],
      "env": {}
    }
  }
}
```

The same file ships as `beispiel-mcp.json`. Things that usually go wrong:
give `uv` with its **full path** (a GUI app's PATH is often shorter than the
terminal's), use **forward slashes** in JSON, and **toggle the server off and
on** in LM Studio after editing — a failed start is silent, the entry just
disappears from the tool list.

**4. Look at the settings.** `config.json` is commented; three places are
marked `ANPASSEN` (adjust). It also runs without any change.

---

## Three principles

### 1. Search returns references. Open returns content.

```
recall("serial connection drops")
  -> [{id: "pb_...", trigger: "LiDAR answers with F0 bytes", score: 0.83}]   ~40 words

open_record("pb_...")
  -> the full playbook                                                      ~600 words
```

The same for skills (`skill_find` shows one line), sub-agents (only the
summary, never the transcript), `verify` (only the tail of the output) and
other servers' tools (`tool_find` shows no schema).

### 2. One server instead of sixteen

```
LM Studio ──▶ agent-memory ──▶ filesystem · terminal · web-search · blender
                              unity · unreal · image-gen · ...
```

`tool_find(query)` searches **all** other MCP servers, `tool_call(name, args)`
runs a tool, `open_record('tool:NAME')` shows its parameter schema. Switching
over, with preview and undo:

```powershell
uv run python scripts/schlank.py            # preview only
uv run python scripts/schlank.py --apply    # reduce mcp.json to one server
uv run python scripts/schlank.py --zurueck  # undo
```

The other servers are **not deleted**; they move to
`data/system/mcp_servers.json`, where the gateway still finds them.

### 3. Going in circles gets stopped

Small models get stuck: a call brought nothing, so they make it again. And
again.

| | |
|---|---|
| **repeat** | the same call with the same arguments three times |
| **circle** | `A → B → A → B`, identical every time |
| **fruitless** | three searches in a row, each without a hit |
| **standstill** | a sub-agent returns neither text nor a tool call |

When stopped, the call is **not executed** — the result would be the same as
last time. Instead the model gets a concrete next step ("open one of the hits
with `open_record()`, or search with DIFFERENT words …"). A bare "loop
detected, stop" would leave it just as stuck.

`verify` → change code → `verify` is explicitly **not** a loop, and waiting on
a sub-agent (`agent_result`) is never stopped.

---

## Tools (27)

**Overview** — `status`
**Memory** — `recall` · `open_record` · `remember`
**Other servers** — `tool_find` · `tool_call` · `tool_refresh`
**Skills** — `skill_find` · `skill_run`
**Sub-agents** — `agent_spawn` · `agent_result` · `agent_cancel`
**Checks** — `verify`
**Context** — `context_checkpoint` · `context_resume` · `model_cycle`
**Other** — `current_datetime` · `ssh_list` · `ssh_edit` · `mcp_registry_list` ·
`mcp_registry_set` · `create_roadmap` · `add_task` · `get_next_task` ·
`update_task_status` · `get_roadmap_status` · `maintenance`

---

## What running real models taught

The fast tests had long been green when the first run against a real model
failed **five times in a row**, each time for a different reason. Four were
not bugs but behaviour of LM Studio and small models. All of them are handled
now:

- **The model is unloaded mid-job** (`HTTP 400: Model unloaded`). Normal for
  local models — idle timeout, or someone picks another model in the chat. A
  background job now re-resolves the model and retries the round (at most
  twice), and says so in its answer.
- **Two models share VRAM.** One round took 266 s — ~0.5 tokens/s for a
  35B-A3B on a 7900 XTX — because two loaded models did not fit into 24 GB
  and LM Studio moved part of the work to the CPU. `status()` now warns when
  more than one language model is loaded.
- **Answers without evidence.** A run returned a config line the model had
  never opened. If a final answer arrives although tools were available and
  **none** was used, the server asks once more; if the model still does not
  read, the answer is delivered with an explicit "unverified" warning.
- **Tool calls written as text.** Some models put
  `{"name": ..., "arguments": ...}` into the reply instead of the tool
  channel and mangle the name. The server extracts such calls (also from
  code fences and `<tool_call>` tags) and maps them to the real tool. Nothing
  is invented: what matches no offered tool stays text.

---

## Settings (`config.json`)

```json
"subagent_modell": "auto",
"erlaubte_mcp_server": ["web-search", "filesystem", "terminal"],
"gateway_server": "alle",
"pfade": { "wissensbasis": "" }
```

- **`subagent_modell`** — `"auto"` uses whatever model is already loaded and
  never loads another one. Measured: the 35B-A3B chat model failed twice; a
  `qwen3-4b-2507` finished in 71 s with 3 real tool calls and the right
  answer. Sub-agents are lots of reading and little thinking — a 4B fits.
- **`erlaubte_mcp_server`** — what a **sub-agent** may use unattended. Kept
  narrow on purpose: nobody is watching. `terminal` is **not** in the default
  list, because typical terminal MCP servers allow any command on the whole
  drive.
- **`gateway_server`** — what the **main model** may reach via `tool_call`.
  Wider, because a human is reading along in the chat.
- **`pfade.wissensbasis`** — optional folder with `wiki/*.md` and
  `skills/*.md`, searched by `recall`/`skill_find` but **never written to**.

The server filters out its own name hard: it cannot call itself, and a
sub-agent cannot start sub-agents.

---

## Storage

One small JSON file per record under `data/` — no database, readable,
versionable.

```
data/records/         own records: playbooks, problems, reviews, notes
data/jobs/            sub-agent jobs with full transcript
data/verifications/   smoke-test logs
data/handoffs/        context hand-overs
data/index/           embeddings, tool inventory (cache, rebuilt on demand)
data/system/          hardware, SSH, registered MCP servers
```

Records link to each other (`related=["solves: pb_…", "depends_on: Pi4"]`).
Back-links are **computed** when a record is opened, not stored — stored
reverse links rot at the first rename. A link to something that does not
exist yet stays visible and shows what is missing. Three example records are
included so that `recall()` finds something from the first call.

---

## Tests

```powershell
uv run python tests/stdio_check.py     # handshake like LM Studio, 9 checks
uv run python tests/smoke.py           # ~91 checks, no model needed, ~60 s
uv run python tests/smoke.py --voll    # plus a real sub-agent run
```

All tests work on a temporary data directory and never touch your records.

---

## Notes

- **`model_cycle` does not clear the chat.** Only a new chat clears the
  context: `context_checkpoint()` → optional `model_cycle()` → open a NEW chat
  → `context_resume()`.
- **`mcp` is pinned to 1.x.** In `mcp 2.0`, `FastMCP` became `MCPServer` and
  `httpx` was replaced — both break this server immediately.
- **Tested on** Windows 11, AMD Radeon RX 7900 XTX, LM Studio, Python 3.11+.
  **Never run on macOS or Linux.** Only LM Studio is supported; model loading
  and the model list are LM-Studio-specific.
- Code comments and tool descriptions are in German.

## Requirements

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- LM Studio with the local server enabled (default `http://localhost:1234`)
- A tool-capable model. A 4B is enough for sub-agents.
