# Decision Log

## Key assumptions

- **"Read only" means the agent never writes back to monday.com**, including
  not auto-fixing bad data at the source — cleaning happens in-memory on
  read, so the boards stay an honest record of what was actually imported.
- **The two datasets are meant to be joined loosely, not on a hard key.**
  Work Orders and Deals don't share a clean foreign key in the sample data
  (`Deal name masked` vs `Deal Name` are similar but not guaranteed to
  match 1:1, and client identifiers differ in format). I treat "query
  across both boards" as *reasoning* across both datasets in the same
  answer (e.g. "won deal value" from Deals next to "billed value" from Work
  Orders for context) rather than a strict SQL-style join, and I say so
  when an answer draws from both.
- **"This quarter" / "recently" type language is ambiguous** — the agent
  states its assumption (e.g. which date field it used, calendar vs fiscal)
  in the answer itself rather than blocking every vague question on a
  clarifying round-trip, per the assignment's own guidance to document
  assumptions and proceed. It does ask a clarifying question when a
  question is genuinely unanswerable without one.
- **monday.com's free-tier column types were assumed available** (status,
  dropdown, date, numbers, text) for the import script; a paid-plan-only
  column type would need a fallback to text.

## Trade-offs chosen, and why

| Decision | Chose | Over | Why |
|---|---|---|---|
| monday.com integration | Direct GraphQL API from the backend | MCP | The assignment allows either. A hosted, standalone web app needs its own UI and needs to be "testable without local setup" — that's a natural fit for a backend calling the API directly. MCP shines when *Claude itself* (in an MCP-aware host like Claude Desktop) is the interface; here the interface is a purpose-built chat UI, so MCP would add a host/broker layer without buying anything back. |
| Query engine | One constrained `run_analysis` tool (model writes a short pandas expression against cleaned DataFrames, executed in a restricted namespace — no imports, no I/O) | A fixed menu of pre-built aggregation tools | Founder questions are genuinely open-ended, and a fixed menu can't anticipate all of them without becoming enormous. The trade-off is real (arbitrary-ish code execution) and is called out in README's Limitations — acceptable for a read-only internal prototype, not for a multi-tenant or production system without further sandboxing (e.g. subprocess isolation, resource limits). |
| Data cleaning strategy | Normalize on every read, cache briefly (2 min TTL) | Clean once and cache indefinitely / clean at import time | monday.com is the live source of truth per the assignment ("must query monday.com dynamically", "do not hardcode CSV data") — someone could edit a board between agent calls. Normalizing on read (with a short cache to avoid hammering the API on every chat turn) keeps answers current without re-fetching on every single tool call within one conversation turn. |
| Header-duplicate detection | Heuristic: drop a row if ≥2 of its cells literally equal their own column name | Manual row-index exclusion | The actual sample data has header rows pasted mid-sheet (e.g. a cell containing the literal text "Deal Stage" in the Deal Stage column) — a generic, data-driven rule generalizes better than hardcoding "skip row 47." |
| Stack | Python (FastAPI) + vanilla JS/HTML frontend | Node/Next.js full-stack | pandas is the right tool for the actual hard part of this assignment (messy tabular data cleaning + ad hoc aggregation), and it lets the "tool" the agent calls be genuinely expressive (real dataframe ops) rather than a reimplemented aggregation DSL. A single static HTML file for the frontend needs no build step or hosting complexity, which matters for a 5–6 hour timebox. |

## How I interpreted "leadership updates" (optional requirement)

I read this as: leadership wants a **recurring, structured artifact**, not
another free-form chat answer — so `POST /leadership-report` runs the same
tool-equipped agent under a system prompt that enforces a fixed markdown
shape (headline metrics → sector table → pipeline health → execution/
collections risk → data caveats), so it can be pasted straight into a deck
or email. It reuses the live tools rather than a hardcoded template so the
numbers never drift from what the chat agent would say if asked the same
questions individually. An explicit "focus" field lets a user nudge it
("focus on Renewables this month") without changing the output shape.

## What I'd do differently / next, with more time

1. **Persist conversations** (currently stateless — history lives only in
   the browser session) so multi-day threads and audit trails are possible.
2. **Harden `run_analysis`** — move execution to an isolated subprocess or
   a proper code sandbox with CPU/memory/time limits, and add a
   regression suite of "known good" founder questions to catch silent
   drift if the model changes how it queries.
3. **A real join key between boards.** Right now the agent reasons about
   Work Orders and Deals side-by-side rather than joining them; adding a
   shared, validated client/deal ID (and surfacing *mismatch* as a data
   quality issue in its own right) would unlock more precise cross-board
   answers like "which won deals haven't produced a work order yet."
4. **Streaming responses** in the chat UI — right now the whole tool-use
   loop runs before anything renders, which feels slow on multi-step
   questions.
5. **Write-adjacent features** (still read-only from monday.com, but
   generating downloadable exports — e.g. a CSV of flagged receivables) —
   deliberately scoped out to stay inside the read-only integration
   requirement and the time budget.
6. **Better date-ambiguity handling** — a small clarifying-question UI
   affordance (quick-reply buttons for "this fiscal quarter" vs "this
   calendar quarter") instead of the agent just picking one and stating it.
