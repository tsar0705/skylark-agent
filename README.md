# Skylark Drones — Monday.com Business Intelligence Agent

## Live Application

- **Hosted app:** https://tsar0705.github.io/skylark-agent/ 
- **Backend API:** https://skylark-agent-1-ulls.onrender.com
- **Backend health:** https://skylark-agent-1-ulls.onrender.com/health
- **Health check:** https://skylark-agent-1-ulls.onrender.com/health/monday
The frontend is hosted on GitHub Pages and communicates with the FastAPI
backend deployed on Render.

A conversational Business Intelligence (BI) agent for Skylark Drones that
answers founder- and executive-level questions using live data from two
monday.com boards:

- **Work Orders** — project execution, billing, collections, dates and status
- **Deals** — sales pipeline, deal stages, values, probability and sectors

The agent reads from monday.com, normalizes real-world messy data, runs
grounded analysis through a small set of tools, and uses **Groq** (OpenAI-
compatible function calling) for query understanding and natural-language
responses.

It also provides a structured **Leadership Update** endpoint, generated
from a single deterministic `leadership_summary` calculation rather than
several separate model-driven queries, so the numbers are stable and cheap
to produce.

See `DECISION_LOG.md` for assumptions, trade-offs, and what I'd do with
more time.

---

## Architecture

```
                     ┌─────────────────────┐
  Browser (chat UI)  │  frontend/index.html │
        │            └──────────┬───────────┘
        │  fetch()               │ REST (JSON)
        ▼                        ▼
┌───────────────────────────────────────────────────────┐
│                FastAPI backend (app/main.py)            │
│                                                           │
│  /chat ─────────────► Agent (app/agent.py)                 │
│  /leadership-report ─► LeadershipReportGenerator             │
│                              │                                 │
│                              ▼ function-calling loop            │
│                        Groq Chat Completions API                 │
│                     (OpenAI-compatible tool calls)                 │
│                              │ calls tools                          │
│                              ▼                                       │
│                  ToolRunner (app/tools.py)                            │
│         get_schema / run_analysis / get_data_quality_notes /           │
│                      leadership_summary                                 │
│                              │                                            │
│                              ▼                                             │
│                     DataStore (app/tools.py)                                │
│                 loads + caches normalized DataFrames                          │
│                              │                                                  │
│                              ▼                                                   │
│         data_normalizer.py  ◄──  monday_client.py (GraphQL, read-only)            │
└───────────────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                     monday.com boards
                (Work Orders, Deals — live data)
```

**Flow of a question:** user asks something in the chat UI → FastAPI passes
the conversation to `Agent.respond()` → Groq decides which tool(s) it needs
(usually `get_schema` once if unsure of a column name, then one or more
`run_analysis` calls with a structured JSON operation, optionally
`get_data_quality_notes`) → tools execute against DataFrames pulled from
monday.com and normalized → Groq synthesizes a grounded, caveated answer.
The agent tracks exact tool calls already attempted in a turn and refuses
to repeat an identical call, so a confused model can't loop.

## Why `run_analysis` takes structured JSON, not code

An earlier version of this project let the model write short pandas
expressions directly. That works well with Claude-class models, but with
Groq's faster open-weight models it produced malformed or unsafe-looking
code more often. `run_analysis` now takes a fixed JSON shape — `dataset`,
`operation` (`count` / `sum` / `mean` / `group_sum` / `group_count` /
`filter` / `top` / `distinct`), optional `filters`, `group_by`, `limit`,
`order_by` — which the model fills in as tool arguments (schema-validated,
not executed as code) and `ToolRunner` interprets deterministically. This
trades some analytical flexibility for reliability and removes arbitrary
code execution entirely. See `DECISION_LOG.md` for the full reasoning.

---

## Repo layout

```
backend/
  app/
    main.py              FastAPI routes: /chat, /leadership-report, /health
    agent.py              Groq function-calling loop + system prompt for chat
    leadership_report.py  Structured leadership-update generator (Groq)
    tools.py               Tool schemas, structured analysis engine, DataStore
    monday_client.py        Read-only monday.com GraphQL client (pagination, caching)
    data_normalizer.py       Cleans messy fields: dates, money, quantities, casing, sectors
    config.py                 Env var loading + validation
  scripts/
    import_to_monday.py       One-time script: creates the 2 boards + imports the sample data
  delete_old_boards.py         Maintenance script to tear down boards by ID (edit BOARD_IDS before running)
  requirements.txt
  .env.example
frontend/
  index.html                   Single-file chat UI (no build step)
sample_data/
  Work_Order_Tracker_Data.xlsx
  Deal_funnel_Data.xlsx
README.md
DECISION_LOG.md
```

---

## Setup

### 1. Get a monday.com API token
1. Log into monday.com (or create a free account).
2. Click your avatar (top right) → **Developers**. This opens the Developer Center.
3. Click **API token** → **Show**, and copy it.

### 2. Get a Groq API key
1. Go to [console.groq.com](https://console.groq.com) and sign up or log in.
2. Open **API Keys** → **Create API Key**.
3. Copy the key — you won't be able to view it again after leaving the page.

### 3. Configure the backend

```
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `backend/.env`:

```
# Monday.com
MONDAY_API_KEY=your_monday_api_key
MONDAY_WORK_ORDERS_BOARD_ID=
MONDAY_DEALS_BOARD_ID=

# Groq
GROQ_API_KEY=your_groq_api_key
GROQ_MODEL=openai/gpt-oss-20b

# Cache
DATA_CACHE_TTL_SECONDS=120

# CORS
ALLOWED_ORIGINS=*
```

`config.py` has a fallback default if `GROQ_MODEL` is unset — set it
explicitly in `.env` to whichever Groq model your account has access to
(this project's final configuration uses `openai/gpt-oss-20b`).

**Never commit `backend/.env`.** It's already in `.gitignore`. If a real
key ever ends up in a shared file, chat log, or commit, treat it as
compromised and regenerate it immediately (Developer Center → API token →
Regenerate for monday.com; Console → API Keys → revoke + create new for Groq).

### 4. Import the sample data into monday.com

This creates the two boards and loads the real, uncleaned data — the
messiness is preserved on import intentionally; `data_normalizer.py`
cleans it at read time, which is the behavior the assignment is testing.

```
python -m scripts.import_to_monday \
  --work-orders ../sample_data/Work_Order_Tracker_Data.xlsx \
  --deals ../sample_data/Deal_funnel_Data.xlsx
```

Copy the two printed board IDs into `.env`:

```
MONDAY_WORK_ORDERS_BOARD_ID=...
MONDAY_DEALS_BOARD_ID=...
```

(`delete_old_boards.py` is a small utility for tearing down boards by ID if
you need to re-import from scratch — edit the `BOARD_IDS` list at the top
before running it; it permanently deletes those boards.)

### 5. Run the backend

```
uvicorn app.main:app --reload --port 8000
```

Check `http://localhost:8000/health` (config sanity — confirms all
required env vars are set) and `http://localhost:8000/health/monday`
(confirms the API key + board IDs actually pull real data).

### 6. Run the frontend

Open `frontend/index.html` directly in a browser, or serve it with any
static file server. Set the "API endpoint" field in the sidebar to your
backend URL if it isn't `http://localhost:8000`.

### 7. Try it

```
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"How many work orders are there?"}]}'
```

```
curl -X POST http://localhost:8000/leadership-report \
  -H "Content-Type: application/json" \
  -d '{}'
```

Example questions to test with:

- "How's our pipeline looking for the mining sector right now?"
- "Which work orders are stuck or need a status update?"
- "What's our total amount receivable across all clients?"
- "Compare won deal value by sector"
- "Generate a leadership update focused on pipeline risk"

---

## Deploying (for the hosted-link deliverable)

### Backend — Render

- **Root Directory:** `backend`
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- **Environment variables:** same keys as `.env` above
  (`MONDAY_API_KEY`, `MONDAY_WORK_ORDERS_BOARD_ID`, `MONDAY_DEALS_BOARD_ID`,
  `GROQ_API_KEY`, `GROQ_MODEL`, `DATA_CACHE_TTL_SECONDS`, `ALLOWED_ORIGINS`)

Render's free tier sleeps after ~15 minutes idle, so the first request
after a period of inactivity can take 30–60 seconds. Railway and Fly.io
have similar free-tier behavior.

### Frontend

`frontend/index.html` is static — deploy as-is on GitHub Pages, Netlify, or
Vercel. Update the "API endpoint" field (or hardcode `apiBase()` in the
file) to point at the deployed backend URL, and set `ALLOWED_ORIGINS` on
the backend to that frontend's exact origin once you're past local testing.

This project uses **GitHub Pages (frontend) + Render (backend)** as two
separate URLs, rather than having FastAPI serve the static frontend from
`/`. That's a normal and valid split for a static frontend + API backend,
and the "Live Application" section above should point evaluators at the
**GitHub Pages URL**, since that's the actual user-facing app. Don't
switch to a single-URL architecture (FastAPI serving the frontend) this
close to a deadline without re-testing the whole flow.

---

## API

- `GET /health` — configuration sanity check (env vars present).
- `GET /health/monday` — confirms monday.com connectivity and returns row
  counts for both boards.
- `POST /chat` — `{"messages": [{"role": "user", "content": "..."}]}` →
  `{"reply": "...", "tool_trace": [...]}`. Send the full running
  conversation each time; the backend is stateless.
- `POST /leadership-report` — `{"focus": "optional extra instruction"}` →
  `{"report_markdown": "...", "tool_trace": [...]}`.

## Analysis tools available to the agent

- **`get_schema`** — column names, inferred types, and example values for
  `work_orders` and/or `deals`. Called when the model isn't sure of an
  exact column name.
- **`run_analysis`** — structured JSON operation (`count`, `sum`, `mean`,
  `group_sum`, `group_count`, `filter`, `top`, `distinct`) with optional
  filters, grouping, ordering, and limits. No code is executed — arguments
  are schema-validated and interpreted deterministically.
- **`get_data_quality_notes`** — missing-value percentages, dropped
  duplicate/header rows, and parsing failures, so the agent can caveat
  answers instead of presenting incomplete data as exact.
- **`leadership_summary`** — one deterministic calculation covering
  pipeline value, won value, active work orders, billed/collected values,
  sector performance, pipeline stage distribution, execution risks,
  receivables, and data coverage — used by `/leadership-report` instead of
  chaining several separate `run_analysis` calls.

## Grounding rules (enforced via the system prompt)

- Every number, date, status, sector, and stage in an answer must come
  from the user's message or a successful tool result — never invented.
- Exact dataset terminology is preserved (e.g. `Deal Status` is never
  silently renamed to "pipeline status"; `Open` is never renamed to
  "Active"). See `agent.py`'s `SYSTEM_PROMPT` for the full list of
  do-not-rename pairs.
- "Pipeline" defaults to `Deal Status = Open` unless the user says
  otherwise; Won and Dead deals are excluded from pipeline totals.
- No invented currency symbols, no invented reporting periods (no
  silently assuming "this quarter" means a specific FY unless the user
  said so or a tool result established it).
- The agent never claims to have written, updated, or deleted anything on
  monday.com — it is read-only.

---

## Known limitations

1. **Groq API quota.** A `429` / rate-limit response means the account's
   usage limit was hit, not an application bug. Swap `GROQ_MODEL` in
   `.env` if your account has access to a different model.
2. **Stateless conversation.** No database — refreshing the frontend loses
   the current thread. The full conversation is sent on every `/chat` call.
3. **Read-only prototype.** No board is ever modified by the running
   application; only the one-time `import_to_monday.py` script (and the
   separate `delete_old_boards.py` utility) write to monday.com.
4. **Fixed analysis vocabulary.** `run_analysis`'s operation set
   (count/sum/mean/group_sum/group_count/filter/top/distinct) covers the
   founder questions tested during development but isn't fully general —
   a question needing a genuinely novel aggregation would need a new
   operation added to the schema and `ToolRunner`.
5. **API rate limits.** monday.com rate-limits by account/plan; caching
   (`DATA_CACHE_TTL_SECONDS`) reduces repeated pulls, and
   `import_to_monday.py` paces its requests during the one-time import.

Full assumptions, trade-offs, and "what I'd do differently" are in
`DECISION_LOG.md`.
