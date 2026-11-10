# Demo project

A tiny corpus so you can try indexing and retrieval in a minute.

## Conventions
- Every task is a written spec with an acceptance test that exists *before* an agent starts.
- No agent approves its own work: a different agent (or a human) reviews and accepts.
- Agents never write global state directly; they send typed commands to the memory API.
