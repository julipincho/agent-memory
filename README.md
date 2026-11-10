# agent-memory

**Persistent, auditable memory for a team of coding agents.** Claude Code, Codex CLI, OpenCode and local models
(Ollama) share one memory service. Each agent starts a session with a **600–1,500-token context pack** instead of
re-reading ~90k–127k tokens of docs and chat history, and it can resume exactly where it (or another agent) stopped.

I built this for **DreamFactory**, my local multi-agent production pipeline for games (a Godot 4 RPG and two other
projects). There, agents take task specs from a queue, run sandboxed, and get reviewed before anything is accepted.
This repo is the memory layer, extracted so it runs on its own.

```
$ python dfmem.py --proyecto demo contexto
<<MEMORIA DREAMFACTORY · dfmem-v1 · agente claude · proyecto demo · inicio>>
Identidad: claude — lead architect, reviewer and implementer · permisos: demo=curador, …
## Reanudar · checkpoint #1 de claude (modelo)
Haciendo: wiring the demo
Reanudar desde: README.md section Quickstart
Pendiente: write README
## Relevante
- [m1·decision·p4] No self-approval: A different agent reviews every task before it is accepted.
<<FIN MEMORIA>>
[250 tokens de ~1500]
```

## Why

Coding agents forget everything between sessions. The usual fixes break down as a project grows:

- **"Read AGENTS.md / CONTINUITY.md first"**: in my previous setup one continuity file alone was ~38k tokens, and
  the full project knowledge was 90k–127k tokens per agent start.
- **Markdown files as memory**: agents overwrite them. One of my agents replaced the whole project log with a
  `Write` call, and I had to recover it from snapshots.
- **Chat history as memory**: a crash or a compaction loses it, and other agents can't see it.

## What it does

| Capability | How |
|---|---|
| **Context packs on a budget** | Per-agent, per-operation token budgets (start, resume, compaction, task switch, dispatch). Priority order: identity → resume point (last semantic checkpoint + later automatic ones) → uncertain operations → current task and its memories → global rules → ranked relevant memories → RAG excerpts (≤ ¼ of the budget). Critical items are shortened, never dropped, and flagged if they push past the budget. |
| **Versioned memory** | Decisions, lessons, known errors and rules with scope (`global`, `project`, `task`, `agent`), priority 1–5, expiry and supersession. Every change is a new immutable revision. |
| **Tasks + checkpoints** | Tasks with acceptance criteria, owner and next action, updated with compare-and-swap. Checkpoints (doing / done / pending / files / tests / git state / resume-from) are append-only per session. |
| **Side-effect ledger** | `intent → confirmed / failed / uncertain → reconciled`. An agent that crashed mid-action sees the operation as *uncertain* on restart and must not blindly repeat it (CLI exit code 3). |
| **RAG with citations** | Postgres full-text search (Spanish config) over chunked project docs; every excerpt cites `path:lines@sha`. A newer document version supersedes the old one, which is kept but no longer retrieved. Optional embedding fusion (RRF) is implemented but not enabled until the eval says it helps. |
| **Identity & permissions** | One token per agent (only its SHA-256 is stored). Levels per project: none < read < write < curator. Only curators write global memory. |
| **Offline-safe client** | Standard library only. If the service is down, writes go to a durable local outbox (fsync'd JSONL) and are reported as **PENDING**, never as saved. They are replayed later with the same idempotency key, so nothing is applied twice. |
| **Secret redaction** | Tokens, keys and high-entropy strings are redacted on the client before anything is stored. |

### Guarantees enforced by the database, not just the code

- Composite foreign keys: a session or checkpoint cannot cross projects or agents.
- Triggers block `UPDATE`/`DELETE` on history tables, **even for the owner role**. Nothing is ever deleted;
  retiring something means changing its state.
- The app role has no DDL and no `DELETE`/`TRUNCATE` on any table. Migrations run under a separate role, and the
  schema owner cannot log in.
- Every command runs in one transaction together with its idempotency record and audit event: it is either
  fully committed or not at all.

## Results (measured on the real DreamFactory deployment, Nov 2026)

| Context pack | Tokens delivered | Budget | "Load everything" | Ratio |
|---|---|---|---|---|
| Game project, session start (Claude Code) | 1,449 | 1,500 | 115,808 | 1.25 % |
| Game project, task dispatch (Codex) | 645 | 700 | 115,703 | 0.56 % |
| Second game, session start | 1,422 | 1,500 | 90,115 | 1.58 % |
| Third project, task dispatch | 586 | 700 | 126,687 | 0.46 % |

Token counts are estimated at ~3.5 characters per token (no local tokenizer).

**Retrieval eval** (`evaluacion/rag_eval.py`, 20 real natural-language questions over 1,910 chunks from 5 projects):
recall@1 0.50 · **recall@3 0.90** · recall@5 0.90 · MRR 0.675 · 31 ms/query · ~720 tokens of excerpts.
Both misses are vocabulary gaps (e.g. "D&D class" vs. "Fighter"), which is the case that would justify adding
embeddings. A tweak that boosted document-name matches didn't improve anything, so I reverted it.

**Resilience:** after a hard power-off of the WSL VM, a confirmed context pack was back in 12 s with identical
row counts. In a test, a fresh Claude Code process with no tools answered where to resume (file, line, pending
signal, pending test) using only what the hook injected.

## Architecture

```
 Claude Code ──hooks──┐            Codex / OpenCode / local model ← "dispatch" pack in their prompt
 dfmem (CLI) ─────────┤            (the orchestrator requests it on their behalf)
 orchestrator ────────┤
                      ▼
          cliente.py (stdlib): token → identity · durable local outbox · idempotent replay
                      │ HTTP 127.0.0.1:8790 (localhost only)
                      ▼
          memoria (FastAPI, SQLAlchemy Core, Alembic) · Docker, read-only FS, 512 MB, no-new-privileges
                      │ internal Docker network (Postgres has no published port)
                      ▼
          PostgreSQL 18 · schema "memoria" · owner / migrator / app roles
```

| Layer | Files |
|---|---|
| Storage | `servidor/migraciones/versions/` (schema, constraints, immutability triggers, role grants) |
| Writes | `servidor/comandos.py`, `memorias.py`, `rag.py`: atomic commands with idempotency and audit |
| Retrieval | `servidor/contexto.py` (budgeted pack, ranking, dedup), `memorias.py`, `rag.py` |
| Identity | `servidor/permisos.py` |
| API | `servidor/api.py`, with stable errors: 401 / 403 / 404 / 409 (version or idempotency conflict) / 422 / 503 (nothing saved) |
| Client | `cliente.py`, `dfmem.py`, `indexar.py`, `fragmentar.py`, `redaccion.py` (standard library only) |
| Claude Code | `hooks_claude.py` |

Design decisions, rejected alternatives and the bugs found during validation: [docs/DECISIONS.md](docs/DECISIONS.md).

## Quickstart

Requirements: Docker with Compose, Python 3.11+.

```bash
bash scripts/setup.sh                    # secrets → build → migrate → seed → one token per agent
export DFMEM_DIR="$PWD/.dfmem"           # where the client keeps tokens, outbox and cache

python dfmem.py estado
python dfmem.py --agente orquestador --proyecto demo indexar
python dfmem.py --proyecto demo rag "why is history immutable"
python dfmem.py --proyecto demo recordar --tipo decision --clave review --prioridad 4 \
    --titulo "No self-approval" --contenido "A different agent reviews every task."
python dfmem.py --proyecto demo checkpoint --haciendo "wiring the demo" --pendiente "write README" \
    --reanudar "README.md, Quickstart"
python dfmem.py --proyecto demo contexto
```

Exit codes: `0` confirmed in PostgreSQL · `2` PENDING in the local outbox (not confirmed) · `3` operation that
must **not** run (already done or uncertain) · `1` error / denied.

Environment variables: `DFMEM_URL` (default `http://127.0.0.1:8790`), `DFMEM_DIR`, `DFMEM_TOKEN`, `DFMEM_AGENTE`,
`DFMEM_PORT` (host port for compose). On Windows with the stack inside WSL, `DFMEM_WSL_DISTRO` keeps the distro
awake with a bounded `sleep` while agents use it, because WSL shuts itself down when idle.

## Claude Code hooks

`hooks_claude.py` gives Claude Code memory without relying on the model to remember to save:

| Event | What it does |
|---|---|
| `SessionStart` (startup / resume / clear / compact) | Opens or reopens the session and injects the context pack as `additionalContext`. After a compaction it re-injects what matters. |
| `PostToolUse` (Edit / Write / …) | Records the touched file locally (no network). |
| `Stop` | Heartbeat with mechanical state (files, git) to the outbox, sent in the background. Never blocks. |
| `PreCompact` | Automatic checkpoint before context is lost. |
| `SessionEnd` | Automatic checkpoint if anything changed, then closes the session. |

The model saves the semantic part itself (`dfmem checkpoint`, `dfmem recordar`); the hooks guarantee the mechanical
part even when it doesn't. Conversations and model reasoning are never stored. A hook never breaks a session: on any
error it exits 0 and logs. Example config: [docs/claude-settings.example.json](docs/claude-settings.example.json).

## Tests

```bash
bash scripts/test_isolated.sh            # throwaway Postgres 18 on its own network: bootstrap ×2, migrations ×2,
                                         # role/privilege checks, 21 integration tests, integrity check
python pruebas_cliente/test_unitarias.py # 7 client unit tests (chunking, redaction, outbox)
python pruebas_cliente/test_cliente.py   # 6 end-to-end tests against the running service: simulated Claude Code
                                         # sessions, compaction, Postgres down → PENDING → replay without
                                         # duplicates, permissions over HTTP, uncertain operations, token budget
```

The integration suite includes invariant tests, for example: no context pack exceeds its budget without the
`excedido_por_criticos` flag, and the current task's memory is never pushed out by global rules.

## Status

- **Working and used daily:** schema and migrations, API, identities and permissions, versioned memory, tasks with
  CAS, checkpoints, uncertain operations, budgeted packs, lexical RAG, Claude Code hooks, outbox, recovery after
  power loss.
- **Implemented, not validated:** hybrid retrieval with embeddings (tested only with synthetic vectors). It stays
  off until a small local embedding model beats lexical search on `rag_eval.py`.
- **Note:** code identifiers, CLI commands and the context-pack text are in Spanish, because the system was built
  for my own Spanish-language workflow. Docs and API errors are documented here in English.

## License

MIT
