# Design decisions, rejected alternatives and findings

Written while building the memory for DreamFactory (November 2026). The "Context" column describes the original
deployment: the service shared a PostgreSQL 18 instance (with an existing, verified daily backup) with an earlier
project, inside a WSL2 VM on Windows. The standalone `compose.yaml` in this repo ships its own Postgres.

## Starting point (verified facts, not assumptions)

- Agent memory lived in Markdown files (per-agent progress notes, task summaries, a generated status file) and in
  chat history.
- The earlier factory attempt kept a single continuity file of 132 KB (~38,000 tokens). That is the problem this
  solves.
- WSL shuts itself down when idle (measured). A cold start costs ~6 s plus PostgreSQL startup.

## Decisions

| # | Decision | Why | Rejected alternatives |
|---|---|---|---|
| M1 | A dedicated `memoria` schema with its own roles (owner without login, migrator, app without DDL or DELETE) | Isolation by schema and role; the existing daily backup covered it without touching root scripts; `pg_dump -n memoria` makes it portable | A dedicated database (no backup unless root scripts changed); tables in `public` (mixed with another migration chain) |
| M2 | Its own Alembic chain (`memoria.alembic_version_memoria`) | Doesn't branch or interfere with the other project's migrations | Adding migrations to the other backend |
| M3 | Its own FastAPI service in Docker, localhost only | It is the only holder of the DB password; identity is resolved server-side from the token; agents on Windows use plain HTTP with the standard library | Publishing PostgreSQL (recreating a production container and opening a port); CLI inside WSL hitting the container IP (needs root to resolve it) |
| M4 | Atomic commands + idempotency + audit in the same transaction | Retries are safe (a lost ACK doesn't duplicate), and nothing is "confirmed" before COMMIT | Loose writes per endpoint |
| M5 | Immutable history via triggers, no DELETE/TRUNCATE | "Nothing gets deleted": a correction is a new revision, and that protects even against the owner role | Trusting application code alone |
| M6 | Local outbox in the client | Never fake persistence: an explicit PENDING state and idempotent replay when the service returns | Failing and losing the write; using files as the source of truth |
| M7 | Bounded WSL keep-alive (`sleep` process, hidden, time-limited) | Keeps the DB up while agents work, without changing `.wslconfig` or Windows scheduled tasks | Changing WSL or Windows configuration |
| M8 | Context packs with per-agent / per-operation budgets | Fixed priority order, shown in the README. Critical items are shortened to titles, never dropped, and flagged `excedido_por_criticos` | Loading everything; summarizing with an LLM on every start (spends tokens and adds latency) |
| M9 | Lexical RAG (Spanish full-text, OR query, excerpts cited as `path:lines@sha`) | No heavy components, 31 ms/query, recall@3 = 0.90 on the bench | pgvector (not in the image; would have meant recreating the production Postgres); local embeddings without measurement |
| M10 | Claude Code hooks at user level | They apply to every registered project without writing into each repo; outside a registered root they do nothing | A `.claude/settings.json` per repo |

## Bugs found during validation (root-cause fixes, not patches)

- **The automatic end-of-session checkpoint hid the model's semantic checkpoint.** "Resume" now uses the latest
  *semantic* checkpoint and appends later automatic ones as "After that…".
- **Growing global rules pushed the current task's memory out of the pack.** Task memory is now critical and goes
  first. Regression test: `test_memoria_de_la_tarea_no_la_desplazan_las_reglas_globales`.
- **4 packs went over budget without warning**, because the "on demand" footer wasn't counted. It is counted from the
  start now, and an invariant test checks that no pack exceeds its budget without the flag.
- **Console windows popping up on Windows.** `DETACHED_PROCESS` made each console child (`wsl.exe`, `git`) open its
  own window. Children now use `CREATE_NO_WINDOW`, and background flushes use `pythonw.exe`.

## Measurements (2026-11-03, real data)

See the README table. Method: estimated tokens (~3.5 chars/token), "load everything" = all memories + checkpoints +
indexed corpus for that project.

- Cold start after a hard WSL power-off: 12 s to a confirmed pack, with identical row counts before and after.
- Real Claude Code test: a fresh process with no tools answered where to resume (file and line, pending signal,
  pending test) using only what the hook injected. In a second project it identified the project and its
  constraints.
- RAG bench (20 questions): recall@1 0.50 · recall@3 0.90 · recall@5 0.90 · MRR 0.675 · 31 ms/query · ~720 tokens
  of excerpts. A document-name boost didn't help and was reverted. The 2 misses are vocabulary/semantic gaps.

## Embeddings: when to turn them on

Only if (a) a small local embedding model can run without competing for the GPU with generation jobs, and
(b) `rag_eval.py` shows a gain in recall@3 or MRR. The server already fuses lexical and cosine results with
reciprocal-rank fusion (`fragmentos.embedding real[]`, command `fragmentos_embedding`). pgvector only becomes worth it
at tens of thousands of chunks; today's 1,910 fit in an in-memory cosine pass.
