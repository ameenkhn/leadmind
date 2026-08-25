# LeadMind

**AI Lead Intelligence & Qualification Engine** — an evidence-grounded system that turns raw
scraped leads into ranked, explained, actionable prospects.

> **Status: Phase 1 of 10 complete.** The ingestion pipeline is built, tested and running.
> Everything below describes what exists today; the roadmap at the end says what does not.
> Every number on this page was measured, not estimated. Nothing here is a benchmark claim.

---

## The problem

A scraped lead list is not a lead list. It is a spreadsheet where 7% of the rows are the same
business twice, a quarter of the "cities" are fragments of street addresses, and a tenth of the
"websites" are WhatsApp links. Any scoring model built on top of that is scoring noise, and any
LLM asked "is this a good lead?" will produce a confident paragraph with nothing behind it.

LeadMind's premise is that the interesting work happens before the model: establish what is
actually known about each lead, how reliably it is known, and what evidence supports it — then
score, explain and act.

## The dataset

`Outbound_Leads.xlsx` — 2 520 Indian SMB advertisers harvested from the Meta Ad Library:
coaches, clinics, astrologers, training institutes, financial advisors. Three worksheets with
three different schemas.

Two things about it shape the whole system:

1. **There are no firmographics.** No employee count, revenue, funding, tech stack, job title, or
   person name. Any ICP model that assumes them is modelling data that does not exist.
2. **Every lead is a paying advertiser.** Presence in the Meta Ad Library is a hard, dated buying
   signal, and `Matched_Query` says what they were advertising for. That is the most valuable
   thing in the file and it costs nothing.

Full profile: **[`docs/01-dataset-analysis.md`](docs/01-dataset-analysis.md)**.

---

## What Phase 1 does

```
Outbound_Leads.xlsx
  → sheet-aware reader      per-sheet column maps; an unknown column is a hard failure
  → normalizers             email · phone · url · followers · name · city · category
  → validators              record-level rules; nothing is ever dropped
  → deduplication           union-find on identity keys; resemblance goes to a review queue
  → merge                   confidence-ranked scalars, accumulated observations
  → company resolution      owned-domain keyed, franchise-aware
  → data quality score      config-driven rubric, every reason persisted
  → PostgreSQL              idempotent upserts against natural keys
```

Measured output over the real file:

| | |
|---|---:|
| Rows read | 2 520 |
| Exact duplicates merged | 169 |
| Leads | 2 351 |
| Companies (28 multi-branch) | 1 826 |
| Identifiers | 11 287 |
| Follower observations | 2 314 |
| Pairs queued for human review | 73 |
| Weak eval labels seeded | 900 |
| Data quality — mean / median | 69.4 / 72.8 |
| Full ingest | ~23 s |
| Tests | 123 passing |

```
rows_read == leads_total + rows_merged
2520      == 2351        + 169          ✓
```

The CLI exits non-zero if that identity fails.

---

## Engineering decisions worth defending

### The same column name meant two different things

`Day_1.Niche` holds five curated verticals. `Day_3.Niche` holds 131 Facebook page categories, 74
of which appear verbatim in `Day_1.FB_Category`. Mapping them together on a shared header would
have corrupted every vertical feature in the system. Hence explicit per-sheet column maps, and a
test that fails if the mapping ever regresses.

### Shared website is a relationship, not an identity

```
Pumo Technovation Kanchipuram      pumotechkanchipuram@gmail.com
Pumo Technovation Malumichampatti  pumotechnovationmalumichampatt@gmail.com
Pumo Technovation Tirupati         pumotechnovationtirupati@gmail.com
Pumo Technovation Bommasandra      pumotechnovationbommasandra@gmail.com
Pumo Technovation Poonamallee      pumotechpoonamallee@gmail.com
```

Five franchise branches on one domain, each with its own inbox, phone and city. Deduplicating on
website deletes four real prospects and looks correct doing it. So only exact identity keys
(email, phone, Facebook URL) auto-merge; shared hosts and fuzzy names go to `duplicate_candidates`
with a confidence and a `pending` status, for a human. The fuzzy threshold is tuned against the
169 known duplicate pairs as labelled data, not picked by feel.

### The duplicates disagree, and the disagreement is the data

Across the 169 duplicate pairs, follower counts agree only 61.9% of the time — the two scrapes
happened days apart. That is a growth signal, not dirt. Follower counts are stored as dated
observations in `metric_observations` rather than a column that gets overwritten on merge. 168
leads carry two observations; 63 of those differ.

`observed_at` stays **NULL**, because the workbook has no scrape dates and inventing one so a
growth *rate* could be computed would fabricate the number it was meant to measure.

### Data quality ≠ lead quality

Two numbers that get conflated everywhere and must not be. The Phase 1 rubric answers *how much
do we reliably know about this record* — completeness, reachability, verifiability. Whether the
lead is worth contacting is a later, separate judgement with different inputs. A test asserts the
two are independently distributed, because if a rich profile always meant a good prospect the
system would quietly be ranking popularity.

### Nothing is ever dropped

Not one validation rule rejects a row. Records with unusable fields still become leads; the
reasons land in `validation_issues` where they can be counted, filtered, and shown as explicit
gaps. This is what makes reconciliation possible, and reconciliation is the only proof that
ingestion worked.

### No LLM in Phase 1, on purpose

Nothing here needs one. Email validation, phone parsing, deduplication and completeness scoring
are all things deterministic code does faster, cheaper, reproducibly and explainably. The models
arrive in Phase 4 where the task is actually semantic.

---

## Architecture

Modular monolith. No Celery, no Redis, no microservices — they would be over-engineering for a
2 520-row batch job, and the module boundaries are drawn so they can be added when a queue is
genuinely needed in Phase 3.

```
leadmind/
├── backend/app/
│   ├── core/           settings, structlog with a run_id on every record, typed errors
│   ├── db/             engine, session, alembic migrations
│   ├── models/         SQLAlchemy 2.0 typed models
│   └── ingestion/
│       ├── readers/        sheet-aware Excel reader + per-sheet column maps
│       ├── normalizers/    one pure function per field
│       ├── validators/     record-level rules
│       ├── dedup/          union-find, candidate detection
│       ├── resolution/     cluster merge, company resolution
│       ├── quality/        the scoring rubric
│       └── pipeline.py     orchestration and persistence
├── backend/tests/      unit (no DB) + integration (real PostgreSQL)
├── config/             quality.yaml · taxonomy.yaml · gazetteer.yaml · sources.yaml
├── data/raw|processed|evaluation/
├── docs/
└── scripts/profiling/  the scripts behind docs/01-dataset-analysis.md
```

### Schema notes

- **`lead_identifiers` is a table, not eight sparse columns.** The source fills them between 12%
  and 100%; deduplication queries across all of them; a ninth channel should be a row, not a
  migration.
- **`lead_source_records` keeps every raw row as JSONB** with a content hash. Re-ingestion is
  idempotent and every normalised value traces back to the cell it came from.
- **`lead_source_queries` is a child table** because `Matched_Query` is genuinely many-to-one.
- **`eval_labels.label_source`** separates the 900 shipped `Relevance` values (weak, provenance
  unknown) from future hand-verified gold labels, so they can never be averaged together.
- **Native PostgreSQL enums.** Adding a value needs a migration — noisier than free text, but an
  invalid value becomes impossible to write. For a pipeline whose whole value is trustworthy
  data, that trade is worth making.

---

## Running it

Requires Python 3.11+, Docker, and [uv](https://github.com/astral-sh/uv).

```bash
make install        # venv + dependencies
make db-up          # PostgreSQL 16 + pgvector
make migrate        # build the schema
make ingest-dry     # process everything, write nothing
make ingest         # persist
make check          # lint + type-check + full test suite
```

```
leadmind ingest data/raw/Outbound_Leads.xlsx [--dry-run] [--json]
leadmind check-schema data/raw/Outbound_Leads.xlsx
leadmind config
```

`make ingest` prints a reconciliation report: rows in, rows merged, leads out, review queue,
validation issues by code, and the quality distribution.

### Testing

123 tests. Unit tests need no database; integration tests create and migrate a dedicated
`leadmind_test` database and run inside transactions that are rolled back.

```bash
make test-unit      # ~7s, no database
make test           # ~2min, full suite
```

The test cases are drawn from the dataset, not invented: `1.4K`, `gamil.com`,
`http://www.bellsoverseas/`, `Advertiser 13887200`, `Nagar`, `wa.me/...`.

---

## Security

- No secrets in the repository; `.env` is git-ignored and `alembic.ini` carries no connection
  string — the URL comes from settings so there is one source of truth.
- The URL normalizer parses entirely offline: `tldextract` uses its bundled public-suffix
  snapshot and makes no network call at import or at runtime. A pipeline that silently reaches
  the internet to parse a string is a pipeline that breaks in CI.
- Phase 1 makes no outbound requests at all. When the crawler arrives in Phase 3, scraped content
  is treated as untrusted data: SSRF protections on fetch, and strict separation of system
  instructions from retrieved text so a page cannot rewrite the agent's policy.

## Observability

`structlog` JSON output, with a `run_id` bound to every record inside a pipeline run so one
ingest can be reconstructed from logs alone. `ingest_runs` stores the git SHA and rubric version
with each run's full stats, so any stored score can be traced to the exact code and rules that
produced it.

---

## Roadmap

| Phase | | |
|---|---|---|
| 1 | Ingestion, dedup, data quality | **done** |
| 1b | Async MX/SMTP and website liveness checks | next |
| 2 | FastAPI: leads, filtering, pagination, review queue | |
| 3 | Crawler, semantic chunking, pgvector + BM25, hybrid retrieval, reranking | |
| 4 | Grounded RAG with citations, confidence, claim↔evidence mapping | |
| 5 | ICP · intent · pain-point engines; lead scoring; explainability | |
| 6 | Bounded research agent (state machine, logged tool calls) | |
| 7 | Personalised outreach with an approval workflow | |
| 8 | Evaluation harness: retrieval, RAG groundedness, classification, ranking | |
| 9 | Next.js dashboard | |
| 10 | Docker, background jobs, caching, auth, rate limiting, CI/CD | |

### Known limitations, stated plainly

- Email deliverability and website liveness are **unverified** — recorded as such rather than
  assumed. Syntactic validity is not reachability.
- Follower growth rate is computable but **disabled**, because the source has no scrape dates.
- The 900 seeded evaluation labels are **weak supervision**, not ground truth. About 200 need
  hand-verification before any metric derived from them means anything.
- 16 city values (0.8%) remain unresolved. They are small towns absent from the gazetteer, kept
  verbatim and marked unresolved rather than fuzzy-matched into a plausible wrong answer.
- Entity kind is `unknown` for 1 334 leads. A two-token personal-looking name is a guess, and
  guesses are not recorded as observations.

---

## Documentation

- [`docs/01-dataset-analysis.md`](docs/01-dataset-analysis.md) — the full dataset profile
- [`docs/02-phase1-plan.md`](docs/02-phase1-plan.md) — plan and constraints
- [`docs/03-ingestion.md`](docs/03-ingestion.md) — how ingestion works and why
