# Decision Log

## Key assumptions

- **"Read only" means the running BI agent never writes back to monday.com.**
  Data is fetched from monday.com, normalized in memory, and analyzed without
  modifying the source boards. The one-time import script creates/populates
  the boards, but the deployed application only reads them.

- **The two datasets are treated as related business datasets, not as a strict
  relational join.** Work Orders and Deals do not expose a guaranteed shared
  foreign key in the sample data. Questions involving both datasets are
  answered by reasoning across the two datasets rather than inventing a join
  key.

- **"Pipeline" means open deals by default.** Unless the user specifies
  otherwise, pipeline calculations use `Deal Status = Open`. Won and Dead
  deals are not silently included in open-pipeline totals.

- **Ambiguous dates are not invented.** The agent does not silently create a
  reporting period, quarter, fiscal year, or "current" date that is not
  supported by the user's question or a tool result. If the requested
  analysis genuinely requires a missing date definition, the agent states the
  limitation rather than manufacturing a period.

- **Dataset terminology is preserved.** Column names and categorical values
  are treated as source data. For example, `Deal Status = Open` is not
  silently renamed to "Active", and `Execution Status` is not treated as
  equivalent to `Billing Status`.

- **Missing and non-numeric values are excluded from numeric aggregates.**
  Aggregations operate on values that can be parsed numerically. Material
  missing-data limitations are surfaced to the user.

- **monday.com is the live source of truth.** The application does not
  hardcode the sample Excel values as its analytical source. Board data is
  queried dynamically, with a short cache to reduce unnecessary API calls.

---

## Trade-offs chosen, and why

| Decision | Chose | Over | Why |
|---|---|---|---|
| monday.com integration | Direct GraphQL API from the backend | MCP | The assignment requires a usable hosted application. Direct API access keeps the architecture simple and independently deployable. MCP would add another host/broker layer without a clear benefit for this purpose-built web UI. |
| Query engine | Structured `run_analysis` tool with a fixed JSON operation vocabulary | Model-generated pandas/code execution | An earlier version allowed the model to generate pandas expressions. The final version uses schema-validated JSON operations such as `count`, `sum`, `mean`, `group_sum`, `group_count`, `filter`, `top`, and `distinct`. `ToolRunner` interprets these deterministically, so model-generated code is never executed. |
| Tool vocabulary | Small set of deterministic analytical operations | Large collection of specialized tools | Common founder-level BI questions can be expressed through combinations of a small set of operations. This reduces tool-selection complexity and token usage while covering the tested questions. |
| Data cleaning strategy | Normalize on read + short cache | Clean once during import | monday.com remains the source of truth. Cleaning at read time keeps newly fetched data consistent, while caching avoids repeated board downloads during active use. |
| Header-duplicate detection | Data-driven heuristic | Manual row-index exclusion | The sample data contains header-like rows inside the datasets. Detecting rows where multiple cells match their own column names is more robust than permanently excluding a particular row number. |
| Missing-value handling | Parse numeric fields and exclude invalid values from numeric aggregates | Treat every raw value as numeric | Business data contains blanks and malformed numbers. Treating invalid values as zero would silently distort totals, so numeric parsing distinguishes valid values from missing/non-numeric values. |
| Terminology handling | Preserve source column names and categorical values | Invented business synonyms | Matching source terminology makes answers auditable and prevents transformations such as calling `Open` "Active" or changing `Pause / struck` into another status. |
| Agent grounding | Tool results are the source of factual numbers | Model-only reasoning | The model decides which tools to call and explains results, while factual numbers, dates, statuses, sectors, and stages must come from the user input or successful tool results. |
| Tool-call loop | Track and reject duplicate tool calls within a turn | Unlimited retries | Models can request identical calls repeatedly. Tracking attempted calls prevents wasted API quota and reduces the chance of exhausting the tool-call budget. |
| Leadership report | One deterministic `leadership_summary` calculation | Several independent model-driven analysis calls | A leadership report needs stable numbers across sections. One deterministic calculation reduces Groq token usage and prevents inconsistent totals between sections. |
| Leadership formatting | Fixed markdown structure generated from computed results | Completely free-form report | Executives need a predictable, skimmable artifact. The fixed structure covers headline metrics, sector performance, pipeline health, execution/collections watch, and data caveats. |
| Stack | Python + FastAPI + pandas + vanilla HTML/JS | Node/Next.js full-stack | pandas is well suited to messy tabular data. FastAPI provides a lightweight API and a static HTML/JS frontend requires no build pipeline. |
| Frontend hosting | GitHub Pages | Serving frontend through FastAPI | The frontend is static and needs no server-side rendering. GitHub Pages provides simple static hosting while Render handles the Python backend. |
| Backend hosting | Render | Self-managed VM | Render provides straightforward deployment from GitHub with environment variables and HTTPS, which is sufficient for the hosted-link requirement. |
| API model | Groq with `openai/gpt-oss-20b` | Anthropic Claude | The final implementation uses Groq's OpenAI-compatible API and function calling, matching the deployed configuration. |

---

## How the final analysis architecture works

```text
User question
     |
     v
FastAPI /chat
     |
     v
Agent
     |
     v
Groq function calling
     |
     +----------------------+
     |                      |
     v                      v
get_schema            run_analysis
     |                      |
     |                      v
     |                ToolRunner
     |                      |
     |                      v
     |                  DataStore
     |                      |
     +----------+-----------+
                |
                v
        normalized DataFrame
                |
                v
        deterministic result
                |
                v
             Groq
                |
                v
        grounded markdown
```

The model does not write pandas code. It produces structured tool arguments,
and `ToolRunner` validates and interprets those arguments against the
normalized DataFrames.

This gives the model analytical flexibility without allowing model-generated
code to execute.

---

## Leadership report architecture

The leadership endpoint deliberately differs from the conversational agent.

`POST /leadership-report` calls a deterministic `leadership_summary` tool that
calculates the complete leadership dataset in one operation.

The summary covers:

- open pipeline value
- won deal value
- active work orders
- billed value
- collected value
- receivable value
- sector-level pipeline and won values
- active work orders by sector
- open-deal stage distribution
- highest-value open deals
- execution attention
- billing attention
- receivables
- data-quality coverage

The model is used for presentation of the already-computed results rather than
being responsible for independently discovering every metric.

This reduces token consumption, prevents unnecessary repeated tool calls, and
keeps related numbers internally consistent.

---

## Data-quality strategy

The source data contains real-world inconsistencies such as:

- missing values
- non-numeric money values
- inconsistent categorical values
- incomplete status fields
- missing dates
- header-like rows embedded in the data
- incomplete deal values
- incomplete billing information

The application separates:

1. **Raw source data** from monday.com.
2. **Normalized data** used for analysis.
3. **Data-quality metadata** describing missing or rejected values.
4. **Computed analytical results** returned by the tools.
5. **Natural-language interpretation** produced by Groq.

This prevents the language model from having to infer whether a blank or
malformed business value should be treated as zero.

---

## Grounding and terminology rules

The final agent follows these principles:

- Never invent numerical values.
- Never invent dates.
- Never invent reporting periods.
- Never invent sectors or deal stages.
- Never invent status categories.
- Never silently rename source terminology.
- Do not treat `Open` as `Active` unless the context explicitly supports it.
- Do not include Won or Dead deals in an open-pipeline calculation unless the
  user explicitly requests them.
- Do not claim a cross-board relationship unless the source data establishes it.
- Do not present a calculation as exact when important source values are
  missing or non-numeric.
- If a tool fails, do not manufacture a result.
- If the available data cannot answer a question reliably, state the limitation.

These rules are implemented through the agent system prompt, structured tool
schemas, deterministic `ToolRunner` behavior, and data-quality metadata.

---

## Why the agent sometimes asks for schema information

`get_schema` provides:

- dataset names
- column names
- inferred data types
- representative values

This allows the model to resolve exact source terminology when necessary
without placing the entire dataset schema into every model prompt.

For common questions where the required column names are already known, the
agent can directly call `run_analysis`.

---

## Read-only design

The deployed application is intentionally read-only.

The running agent can:

- fetch monday.com board data
- normalize it
- analyze it
- calculate metrics
- generate explanations and reports

The running agent cannot:

- edit a work order
- change a deal
- update a status
- modify a billing field
- delete a record
- write analytical results back to monday.com

The separate import/maintenance scripts are outside the normal conversational
runtime and are used for project setup and board management.

---

## What I'd do differently / next, with more time

1. **Persist conversations.** The current backend is stateless. A database-backed
   conversation store would support multi-day threads, auditing, and user-specific
   history.

2. **Add automated regression tests.** Maintain known founder questions with
   expected tool operations and important output invariants to catch behavioral
   drift when changing the Groq model or system prompt.

3. **Add stronger analytical coverage.** The current operation vocabulary covers
   the assignment's tested BI questions, but genuinely novel aggregations may
   require a new operation in `run_analysis`.

4. **Introduce a validated cross-board relationship.** A shared Deal/Work Order
   identifier would enable precise joins and cross-board questions.

5. **Improve date-aware analysis.** A dedicated date-filter operation could
   support explicit periods such as January 2026 or Q2 2026 while making the
   selected date field and period transparent.

6. **Stream long responses.** Streaming would improve perceived responsiveness
   for multi-step questions.

7. **Improve production hardening.** Add request timeouts, payload limits,
   authentication, monitoring, structured logs, and stronger API-rate-limit
   handling.

8. **Tighten CORS for production.** `ALLOWED_ORIGINS=*` is convenient for the
   assignment. Production should restrict it to the actual frontend origin.

9. **Add richer frontend visualization.** Sector comparisons, pipeline stages,
   receivables, and execution risks could be displayed as interactive charts.

---

## Final implementation summary

The final project intentionally favors **grounded, deterministic analytics over
maximum model freedom**.

The model decides:

> "What analysis should I request?"

The tools decide:

> "What does that analysis actually calculate?"

The data layer decides:

> "Which cleaned source records are available?"

The model then decides:

> "How should the verified result be explained?"

This separation keeps the system relatively simple while reducing hallucinated
numbers, unsupported terminology, repeated tool calls, and unnecessary Groq
token consumption.
