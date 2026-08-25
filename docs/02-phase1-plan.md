# LeadMind — Phase 1 Plan (for approval)

Follows `docs/01-dataset-analysis.md`. Nothing in this document is implemented yet.

---

## 0. Decisions taken

| Decision | Choice | Consequence |
|---|---|---|
| Offer | An AI/SaaS product sold **to** these SMBs | ICP is built on **digital-maturity** signals, not firmographics — which is what the data actually supports |
| Enrichment | Full crawl of the ~2 001 owned domains | Knowledge layer is real; crawler is a first-class component, not a script |
| Models | Fully local / open weights | Zero API cost, provider interface still pluggable, quality ceiling accepted |

### The ICP this implies

Selling AI/SaaS to a coach, clinic or training institute means the buyer must be *reachable,
solvent, and digitally mature enough to adopt software*. Every one of those is measurable from
this dataset without a single LLM call:

- **Solvency / spend** — currently running Meta ads (100% of Day_1+Day_2), follower base as a proxy for scale
- **Digital maturity** — owns a domain (79.4%), multi-channel presence, established FB page (vanity handle, 64%), LinkedIn presence (22.3%)
- **Reachability** — corporate vs freemail (48.2% / 51.8%), role-based vs personal inbox (30.8%)
- **Vertical fit** — controlled taxonomy derived from `FB_Category` + `Day_1.Niche`
- **Operational complexity** — multi-branch/franchise structure (the Pumo pattern), service breadth from the site

Negative signals are equally measurable: 260 thin leads, 42 zero-follower pages, 6 placeholder
advertiser names, 912 numeric-ID pages. These reduce **confidence**, and separately some reduce
**fit** — the two must never be collapsed into one number.

All of this lands in `config/icp.yaml`, versioned, never inside a prompt.

---

## 1. ⚠ Environment constraint you need to know about

I probed both sandboxes I can run code in. Neither can reach the open web:

| Capability | Cloud sandbox (mine) | Linux VM on your Mac (`device_bash`) | Your actual macOS |
|---|---|---|---|
| PyPI / npm | ✅ | ✅ | ✅ |
| Arbitrary websites (the crawl) | ❌ blocked | ❌ blocked | ✅ |
| huggingface.co (model weights) | ❌ blocked | ❌ blocked | ✅ |
| ollama.com | ❌ blocked | ❌ blocked | ✅ |
| Docker Hub | ❌ blocked | no docker | ✅ |
| Postgres 16 + pgvector | ✅ via apt | ❌ | ✅ via Docker |

Also worth stating plainly: `device_bash` is **not** your Mac's shell — it's an isolated Linux VM
with your folders mounted. It has no Docker, ~4 GB free disk, and no arbitrary web egress. So I
cannot install Ollama or Postgres on your machine, and I cannot run the crawl or download model
weights anywhere.

**How I'll work around it, honestly:**

- **I write the code into `~/ai_engineering/leadmind`** — it persists on your Mac, it's yours.
- **I test everything testable in my sandbox**, against a real Postgres 16 + pgvector installed
  there via apt. Ingest, normalisation, dedup, DQ scoring, schema/migrations, BM25, hybrid fusion,
  the scoring engine, the API — all of that gets genuinely executed and tested, not hand-waved.
- **Network-bound stages ship as commands you run**: `make crawl`, `make embed`, `make research`.
  I write them, test them against recorded HTTP fixtures (VCR-style cassettes), and you run them
  live on your Mac where the network and Ollama actually exist.
- **The Firecrawl connector in this session** is my only live web access. I'll use it to fetch a
  small sample of real lead sites (~20) to build the fixtures, so chunking and extraction are
  developed against genuine messy Indian SMB HTML rather than something I invented.

If you'd rather I ran the crawl end-to-end myself, the only route is Firecrawl for all ~10 000
pages, which will hit its limits and cost money. Running it locally is both cheaper and faster.

---

## 2. Local model stack (runs on your Mac)

M-series Air, so everything below is MPS/Metal-accelerated and modest in size:

| Role | Model | Size | Why |
|---|---|---|---|
| Embeddings | `bge-m3` (or `bge-small-en-v1.5` if RAM is tight) | 2.2 GB / 130 MB | bge-m3 is multilingual — real, given Hinglish/Devanagari content on these sites |
| Reranker | `bge-reranker-v2-m3` cross-encoder | 2.2 GB | Local, deterministic, no per-query cost |
| Extraction LLM | `qwen2.5:7b-instruct` via Ollama | ~4.7 GB | Structured field extraction, JSON-constrained |
| Hard-reasoning LLM | `qwen2.5:14b-instruct` (optional) | ~9 GB | Only for the small subset that needs it |

Routing rule, per §26: deterministic Python first, 7B for extraction, 14B only when the 7B output
fails schema validation or confidence is low. Everything behind `LLMProvider` / `EmbeddingProvider`
protocols so swapping to an API provider later is a config change, not a rewrite.

**One caveat I won't hide:** a 7B local model is meaningfully worse at structured extraction than a
frontier API model. I'll measure that rather than assert it — the §23 eval harness will report
extraction accuracy per model, and that comparison is itself a good interview artefact.

---

## 3. Phase 1 scope — deterministic ingest, zero LLM

```
leadmind/
├── backend/app/
│   ├── core/          config (pydantic-settings), logging (structlog), errors
│   ├── db/            engine, session, alembic/
│   ├── models/        SQLAlchemy 2.0 typed models
│   ├── schemas/       Pydantic v2
│   └── ingestion/
│       ├── readers/       sheet-aware Excel reader + per-sheet column maps
│       ├── normalizers/   email, phone, url, followers, name, city, category
│       ├── validators/    syntax → structural → semantic
│       ├── dedup/         exact keys, fuzzy, cluster builder, review queue
│       ├── resolution/    company resolution (franchise-aware)
│       └── quality/       DQ rubric v1
├── backend/tests/     unit + integration (real Postgres)
├── config/            icp.yaml, scoring.yaml, taxonomy.yaml, gazetteer.yaml
├── data/raw|processed|evaluation/
├── scripts/           profiling/ (done), ingest CLI
└── docs/
```

### Work items

**1.1 Config & foundations** — `pydantic-settings`, `.env.example`, `structlog` JSON logging with
a `run_id` on every record, typed exceptions. No secrets in git.

**1.2 Alembic + schema v1** — the tables from analysis §7. Notable: `lead_identifiers` (replaces 8
sparse columns), `metric_observations` (preserves the follower time series), `lead_source_queries`
(many-to-one), `lead_source_records` (raw JSONB, content-hashed, idempotent re-ingest),
`duplicate_candidates` (review queue), `eval_labels` with `label_source`.

**1.3 Sheet-aware reader** — explicit `SHEET_COLUMN_MAP` per sheet. `Day_3.Niche → fb_category`
is asserted in a test so it can never silently regress. Unknown columns fail loudly.

**1.4 Normalizers** — each a pure function returning `(value, confidence, method)`:
- email → lowercase, strip, typo-domain correction (`gamil.com`), freemail/role classification
- phone → E.164 `+91XXXXXXXXXX`
- url → canonical host+path, `SOCIAL_OR_SHORTENER_HOSTS` denylist → `is_owned_domain`
- followers → K/M parser (validated: 2 314/2 314 parse)
- city → gazetteer match, address-fragment stoplist (`Nagar`, `Road`, `Vihar`…), confidence score
- category → `FB_Category` (240 values) → controlled vertical taxonomy via alias map
- name → normalised form + `is_placeholder` (`Advertiser \d+`)

**1.5 Validators** — every failure recorded as a `validation_issue` row. Nothing is silently
dropped; §35's "do not hide uncertainty".

**1.6 Deduplication** — union-find over exact keys (email, phone, facebook) → auto-merge.
Website-host and fuzzy-name (rapidfuzz) → `duplicate_candidates` with confidence, **never merged**.
Regression test asserts the 5 Pumo branches stay 5 leads under 1 company. Expected outcome:
2 520 rows → ~2 352 leads, ~168 auto-merges, ~180 candidates queued for review.

**1.7 Company resolution** — owned-domain host → `companies`; branches attach as sibling leads.

**1.8 Data quality score v1** — rubric in `config/quality.yaml`, per-factor reasons persisted,
`rubric_version` stored so scores stay reproducible across rubric changes.

**1.9 Ingest CLI** — `python -m leadmind.ingest data/raw/Outbound_Leads.xlsx --dry-run`, idempotent,
prints a reconciliation report (rows in → leads out → merged → queued → rejected, with reasons).

**1.10 Tests** — unit tests per normaliser with the real edge cases found in §3 (`1.4K`,
`gamil.com`, `wa.me/...`, `Nagar`, `Advertiser 13887200`); integration tests against real Postgres;
one golden test that ingests the actual 2 520 rows and asserts the reconciliation totals.

### Explicitly out of scope for Phase 1
No FastAPI, no embeddings, no crawler, no LLM, no frontend. Phase 1 ends when 2 520 rows land in
Postgres with measured, reproducible quality scores and a green test suite.

---

## 4. Definition of done

- [ ] `alembic upgrade head` builds schema from empty
- [ ] Ingest is idempotent — running twice produces identical DB state
- [ ] Reconciliation report accounts for all 2 520 source rows
- [ ] 168 cross-sheet duplicates auto-merged; Pumo's 5 branches preserved
- [ ] Follower history retained for the 168 double-scraped leads
- [ ] Every lead has a DQ score with persisted per-factor reasons
- [ ] `pytest` green; `ruff` + `mypy --strict` clean on `backend/app`
- [ ] `docs/03-ingestion.md` documents each normaliser and its measured edge cases

---

## 5. Assumptions I'm proceeding on unless you object

1. Python 3.11+, SQLAlchemy 2.0 typed ORM, Pydantic v2, Alembic, `uv` for dependency management.
2. Postgres 16 + pgvector 0.6+ (Docker Compose for you locally; apt-installed in my sandbox).
3. Modular monolith. No Celery/Redis until Phase 3 actually needs a queue — adding them now would
   be the over-engineering §35 warns about.
4. Fuzzy-name dedup threshold starts at rapidfuzz `token_set_ratio ≥ 92`, tuned against the 168
   known-duplicate pairs as labelled data rather than picked by feel.
5. Original Excel is never modified — read-only input, all output to new tables/files.
