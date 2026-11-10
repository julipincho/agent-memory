# Decisions

## Why lexical retrieval first
Postgres full-text search answers in ~30 ms with no extra services. Embeddings are added only if the
evaluation bench shows a real recall gain.

## Why history is immutable
Corrections are new revisions. Triggers block UPDATE and DELETE on history tables, even for the owner,
so an agent cannot silently rewrite what happened.

## GPU scheduling
Only one heavy GPU job runs at a time. A lock file marks a running batch; agents must not load models while it exists.
