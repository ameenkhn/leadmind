# LeadMind — Handoff

**As of 25 August 2026.** Every number here was measured against the real
`data/raw/Outbound_Leads.xlsx` on a live PostgreSQL 16, not estimated.

For what the system *is* and why it is built the way it is, read
[`README.md`](README.md). This document is only about state: what exists, what does not, and what
the next person should pick up.

---

## 1. Status at a glance

| # | Phase | State | Evidence |
|---|---|---|---|
| 1 | Ingestion, normalisation, dedup, data quality | **Done** | 2 520 rows reconcile; 192 unit + 110 integration tests |
| 1b | MX verification, website liveness, rubric v1.1 | **Done** (one caveat, §3.1) | 1 103 domains measured; liveness code tested, not yet run at scale |
| 2 | HTTP API: leads, filtering, pagination, review queue | **Done** | 17 routes; 104 new tests; runs under uvicorn |
| 3 | Crawler, chunking, pgvector + BM25, hybrid retrieval, reranking | Not started | — |
| 4 | Grounded RAG with citations and confidence | Not started | — |
| 5 | ICP · intent · pain-point engines; lead scoring | Not started | `config/icp.yaml` and `scoring.yaml` do not exist yet |
| 6 | Bounded research agent | Not started | — |
| 7 | Personalised outreach with approval workflow | Not started | — |
| 8 | Evaluation harness | Not started | 900 weak labels seeded and ready |
| 9 | Next.js dashboard | Not started | API is complete enough to build against |
| 10 | Docker, jobs, caching, auth, rate limiting, CI/CD | Not started | — |

**Three of eleven phases complete.** The deterministic foundation — everything that establishes
what is *known* — is finished and tested. Nothing semantic has been built yet.

---

## 2. Phase 1 — ingestion (done)

`leadmind ingest data/raw/Outbound_Leads.xlsx`, ~6 s over the full file.

| | |
|---|---:|
| Rows read (`Day_1` 900 · `Day_2` 1 000 · `Day_3` 620) | 2 520 |
| Exact duplicates auto-merged | 169 |
| Leads | 2 351 |
| Companies | 1 826 |
| — multi-branch | 28 |
| Identifiers | 11 287 |
| Follower observations | 2 314 |
| Source queries | 1 457 |
| Weak eval labels | 900 |
| Validation issues recorded (nothing dropped) | 1 726 |
| Pairs queued for human review | 73 |

```
rows_read == leads_total + rows_merged
2520      == 2351        + 169          ✓
```

The CLI exits non-zero if that identity fails. `GET /api/v1/stats` reports it live from
`ingest_runs`.

**Review queue composition:** 48 `shared_website`, 25 `fuzzy_name`. Zero exact-key pairs, by
construction — those merge during ingest and never reach a human.

**Top validation issues:** `profile_numeric_id_only` 931, `thin_record` 256,
`email_domain_website_mismatch` 234, `city_address_fragment` 153, `city_not_a_place` 66,
`zero_followers` 42.

**Delivered:** schema v1 migration, sheet-aware reader, 8 normalizers, validators, union-find
dedup, franchise-aware company resolution, DQ rubric v1, ingest CLI with reconciliation report.

---

## 3. Phase 1b — verification (done, one caveat)

`leadmind verify emails`, 23.5 s for 1 103 domains at concurrency 16.

| | |
|---|---:|
| Distinct email domains | 1 103 |
| Verified (MX present) | 1 063 |
| Proven unreachable | 34 |
| `UNKNOWN` (resolver failure) | 6 |
| DNS queries saved by per-domain dedup | 1 249 |
| Leads affected | 2 311 |
| Leads on managed business email | 939 |

After `leadmind rescore`, rubric v1.1: **mean 71.98 · median 75.68**, with 2 345 leads scored on
10 factors and 6 on 9.

**Delivered:** schema v2 migration, async MX resolver, SSRF-guarded HTTP client, website liveness
checker with per-host rate limiting, TTL'd verification caches, rubric v1.1 with
`mailbox_verified` and `website_live` factors.

### 3.1 The caveat

**Website liveness has still not been run against the real 1 849 owned website URLs.** The code and
its tests are complete — tested against a real local HTTP server, actual sockets and redirects,
not a mocked transport — but the full run is a few thousand outbound HTTP requests to third-party
small-business hosting and has not been executed. Run it deliberately:

```bash
leadmind verify websites --concurrency 16 --per-host 2
leadmind rescore data/raw/Outbound_Leads.xlsx
```

Until then, `website_live` returns `null` for every lead and drops out of the score. That is
correct behaviour — unmeasured is not zero — but it means quality scores are currently computed
on 10 factors rather than 11, and `/api/v1/stats/verification` reports zero website records.

---

## 4. Phase 2 — the API (done)

FastAPI at `/api/v1`, 17 routes, ~3 400 lines across `api/`, `services/` and `schemas/`.

```
GET  /healthz  /readyz
GET  /api/v1/leads · /{id} · /{id}/quality · /{id}/provenance
GET  /api/v1/companies · /{id}
GET  /api/v1/duplicates · /{id}
POST /api/v1/duplicates/{id}/decision
GET  /api/v1/stats · /quality · /verification · /review
GET  /api/v1/meta/categories · /meta/locations
```

Verified end to end against the real corpus under uvicorn, not only under `TestClient`.

### 4.1 What was built

**Reads.** Twenty query parameters on the leads list, all conjunctive: free-text over names
*and* identifier values, controlled vertical, location, state, entity kind, channel presence and absence, quality
bounds, follower bounds, owned-website, mailbox and website verification status, mail provider,
placeholder names, multi-branch, validation issue code, company, and `include_merged`. Five sort
keys, each tiebroken on `id`.

**Explainability.** `/leads/{id}/quality` returns the stored per-factor breakdown — value,
weight, contribution, reason, and whether it was measurable — read rather than recomputed, so an
old score stays explainable after the rubric changes. `/leads/{id}/provenance` returns the raw
spreadsheet rows behind the lead.

**The review queue.** Both leads embedded per pair with a pre-computed field diff. Confirming
sets `leads.merged_into_id` — a pointer, never a deletion — with row locks, idempotent re-confirm,
409 on conflicting merges, chain-following to the merge root with a cycle guard, and full undo by
setting the pair back to `pending`.

**Statistics.** Corpus counts with the reconciliation identity, quality distribution with factor
coverage, verification coverage and staleness, and review-queue depth with per-method confirm
rate.

**Operations.** RFC 9457 `application/problem+json` for every failure; `X-Request-ID` on every
response and in every error body; structlog access logs; separate liveness and readiness probes,
where readiness also checks the schema is at the migration head this build expects.

### 4.2 Schema change

Migration `5f21ac9d1b64` — *human review and reversible merge*:

- `leads.merged_into_id` / `merged_at` / `merged_by`, self-referencing FK, index,
  `merged_into_id <> id` check constraint
- `duplicate_candidates.resolution_note`
- GIN trigram index on `leads.normalized_name` for `?q=` substring search

`alembic upgrade head` was applied from empty and all three migrations ran clean.

### 4.3 Dependencies added

`fastapi` and `uvicorn[standard]`. Nothing new was needed for tests — FastAPI's `TestClient`
uses the `httpx` the verification layer already depends on.

---

## 5. Tests

**302 passing.** 192 unit (no database, ~3 s) + 110 integration (real PostgreSQL, ~2 min).
104 of those are new in Phase 2.

`ruff check`, `ruff format --check` and `mypy --strict` are all clean over `backend/`.

Worth knowing about the new tests:

- **The API corpus fixture is module-scoped, not session-scoped.** It holds one long-lived
  uncommitted transaction containing the full ingest; a session-scoped version would still be
  open when the golden ingest tests run, and their writes would block on the same unique keys.
  The cost is one extra ingest per API test module.
- **Verification results are seeded, not measured** (`seed_verification` in
  `backend/tests/integration/conftest.py`). A test must not depend on DNS, but a filter reading
  `domain_verifications` cannot be tested against an empty table — it would pass by returning
  nothing, which is the bug it exists to catch.
- **A query counter guards against N+1** (`tests/integration/helpers.py`). It asserts a 100-row
  page costs exactly as many SQL statements as a 25-row page. An N+1 returns perfectly correct
  data, slowly, so no other test would notice the regression.

---

## 6. What is not built

Phases 3 through 10, in full. Specifically absent:

- **No crawler.** `backend/app/verification/net.py` has the SSRF-guarded client the crawler
  should inherit, but nothing fetches page bodies for content yet.
- **No embeddings, no chunks, no retrieval.** The `vector` extension is created by migration v1
  and unused. There is no `chunks` table.
- **No LLM anywhere.** No provider protocol, no Ollama wiring, no prompts.
- **No lead score.** `config/icp.yaml` and `config/scoring.yaml` do not exist. The only score in
  the system is *data* quality, which is explicitly not a lead score.
- **No agent, no outreach, no evaluation harness, no frontend.**
- **No auth, no rate limiting, no caching, no CI pipeline, no Dockerfile for the app** (the
  compose file covers only PostgreSQL).

---

## 7. Known limitations

Carried forward, plus what Phase 2 added.

1. **Website liveness has not been run at scale.** §3.1.
2. **Verification is domain-level, not mailbox-level.** A verified domain accepts mail; it does
   not prove that specific address exists. There is deliberately no SMTP callout —
   `docs/04-verification.md` §2 argues it.
3. **6 email domains returned `UNKNOWN`** on the last run — resolver failures, not dead domains.
   They expire in 6 hours and retry automatically.
4. **Follower growth rate is computable but disabled**, because the source has no scrape dates.
   `observed_at` stays NULL rather than being invented. Real dates in `config/sources.yaml` turn
   it on.
5. **The 900 seeded evaluation labels are weak supervision**, not ground truth. Roughly 200 need
   hand-verification before any metric derived from them means anything.
6. **16 city values remain unresolved** — small towns absent from the gazetteer, kept verbatim
   and marked `resolved: false` rather than fuzzy-matched into a plausible wrong answer.
7. **Entity kind is `unknown` for 1 222 of 2 351 leads.** A two-token personal-looking name is a
   guess, and guesses are not recorded as observations.
8. **Pagination is offset-based.** Correct and deterministic at 2 351 rows because every sort
   ends in an `id` tiebreak, but `OFFSET` starts scanning somewhere past a few hundred thousand
   rows. Keyset pagination is the migration path; it is not there because it would buy complexity
   against a problem this dataset does not have.
9. **The API has no authentication.** `leadmind serve` binds to `127.0.0.1` for exactly that
   reason. Do not bind it to `0.0.0.0` before Phase 10.
10. **The review queue has no optimistic concurrency token.** Decisions are row-locked and
    last-writer-wins is safe here because every decision is idempotent and reversible, but two
    reviewers deciding the same pair differently will not be told they disagreed.

---

## 8. Picking it up

### 8.1 Get running

```bash
make install && make db-up && make migrate
make ingest && make verify-emails && make rescore
make serve          # http://127.0.0.1:8000/docs
make check          # lint + types + 302 tests
```

`make db-up` needs Docker. If Docker is unavailable, any PostgreSQL 16 with `pgvector` and
`pg_trgm` will do — point `LEADMIND_DATABASE_URL` at it. Both extensions are required: v1's
migration creates them, and Phase 2's trigram index depends on `pg_trgm`.

### 8.2 The one thing to do first

Run `leadmind verify websites` on a machine with real outbound HTTP, then `leadmind rescore`.
That closes the last measurement gap in the deterministic foundation, moves every lead from 10
scored factors to 11, and makes `website_status` a measurement rather than an `unknown`
everywhere in the API.

### 8.3 Then Phase 3

The crawler is the next real component and the gateway to everything semantic — no crawl means no
chunks, no citations, no grounded claims. Three things are already in place for it:

- `backend/app/verification/net.py` — the SSRF-guarded, per-host rate-limited HTTP client it
  should reuse rather than reimplement.
- `companies.primary_domain` — 1 826 owned domains, already resolved and franchise-aware, so the
  crawl runs per company rather than per lead.
- The `vector` extension, created in migration v1 precisely so the Phase 3 migration does not
  need privileges the application role should not keep.

Read `docs/02-phase1-plan.md` §2 for the model stack decisions and the honest caveat about local
7B extraction quality.

### 8.4 If you are building the dashboard instead

The API is complete enough. `/meta/categories` and `/meta/locations` drive the filter UI from
live data, `/stats` and `/stats/quality` drive the overview, and `/duplicates` is a ready-made
review screen with the diff already computed. Set `LEADMIND_API_CORS_ORIGINS` to the frontend's
exact origin — the wildcard is deliberately not supported.

---

## 9. Where things live

| | |
|---|---|
| Dataset profile | `docs/01-dataset-analysis.md` |
| Ingestion design | `docs/03-ingestion.md` |
| Verification design | `docs/04-verification.md` |
| API reference | `docs/05-api.md` |
| Project guide | `README.md` |
| Migrations | `backend/app/db/migrations/versions/` — three, head `5f21ac9d1b64` |
| Policy config | `config/quality.yaml` · `taxonomy.yaml` · `gazetteer.yaml` · `sources.yaml` |
