# LeadMind

**AI Lead Intelligence & Qualification Engine** — an evidence-grounded system that turns raw
scraped leads into ranked, explained, actionable prospects.

This document is the complete guide to the project: what it is for, the reasoning behind every
significant design decision, how each part works, and how the whole thing behaves when you run
it.

---

## Contents

1. [The problem](#1-the-problem)
2. [The dataset, and what it forces](#2-the-dataset-and-what-it-forces)
3. [Design doctrine](#3-design-doctrine)
4. [Architecture](#4-architecture)
5. [Ingestion](#5-ingestion)
6. [Deduplication and company resolution](#6-deduplication-and-company-resolution)
7. [Data quality scoring](#7-data-quality-scoring)
8. [Verification](#8-verification)
9. [The API](#9-the-api)
10. [Knowledge layer: crawl, chunk, index](#10-knowledge-layer-crawl-chunk-index)
11. [Grounded RAG](#11-grounded-rag)
12. [Lead scoring: ICP, intent, pain](#12-lead-scoring-icp-intent-pain)
13. [The research agent](#13-the-research-agent)
14. [Outreach](#14-outreach)
15. [Evaluation](#15-evaluation)
16. [Dashboard](#16-dashboard)
17. [Production concerns](#17-production-concerns)
18. [The model stack](#18-the-model-stack)
19. [Security](#19-security)
20. [Observability](#20-observability)
21. [Configuration](#21-configuration)
22. [Running it](#22-running-it)
23. [Testing philosophy](#23-testing-philosophy)
24. [Repository layout](#24-repository-layout)
25. [Documentation](#25-documentation)

---

## 1. The problem

A scraped lead list is not a lead list. It is a spreadsheet where 7% of the rows are the same
business twice, a quarter of the "cities" are fragments of street addresses, and a tenth of the
"websites" are WhatsApp links. Any scoring model built on top of that is scoring noise, and any
LLM asked "is this a good lead?" will produce a confident paragraph with nothing behind it.

LeadMind's premise is that **the interesting work happens before the model**: establish what is
actually known about each lead, how reliably it is known, and what evidence supports it — then
score, explain and act.

That ordering is the whole thesis. It is also why the first third of this system contains no AI
at all: email validation, phone parsing, deduplication and completeness scoring are things
deterministic code does faster, cheaper, reproducibly and explainably. Models arrive where the
task is genuinely semantic, and not one step earlier.

---

## 2. The dataset, and what it forces

`Outbound_Leads.xlsx` — 2 520 Indian SMB advertisers harvested from the Meta Ad Library: coaches,
clinics, astrologers, training institutes, financial advisors. Three worksheets with three
different schemas.

| Sheet | Rows | Columns | Notes |
|---|---:|---:|---|
| `Day_1` | 900 | 17 | Richest. Has `Relevance`, `WhatsApp`, a curated `Niche`, `Matched_Query` |
| `Day_2` | 1 000 | 13 | No `Niche`, no `Matched_Query` |
| `Day_3` | 620 | 13 | No `Source`, no `FB_Category` |

Four properties of this file shape every decision downstream.

**There are no firmographics.** No employee count, revenue, funding, tech stack, job title, or
person name exists anywhere in it. Any ICP model that assumes them is modelling data that does
not exist. The ICP therefore has to be built on *digital-maturity* signals — which is what the
data actually supports.

**Every lead is a paying advertiser.** Presence in the Meta Ad Library is a hard, dated buying
signal, and `Matched_Query` says what they were advertising for. That is the most valuable thing
in the file and it costs nothing.

**`Name` is a Facebook Page name, not a person.** About 35% carry a business token (`institute`,
`clinic`, `academy`); the rest are personal brands. The lead↔company split is therefore *derived*
from evidence, never given.

**The same column name means two different things.** `Day_1.Niche` holds five curated verticals.
`Day_3.Niche` holds 131 Facebook page categories, 74 of which appear verbatim in
`Day_1.FB_Category`. Mapping them together on a shared header would corrupt every vertical
feature in the system. Hence explicit per-sheet column maps, and a test that fails if the mapping
ever regresses.

Full profile: **[`docs/01-dataset-analysis.md`](docs/01-dataset-analysis.md)**.

---

## 3. Design doctrine

Seven rules. Every section below is an application of one of them.

### 3.1 Data quality is not lead quality

Two numbers that get conflated everywhere and must not be.

*Data quality* answers **how much do we reliably know about this record** — completeness,
reachability, verifiability. *Lead quality* answers **is this worth contacting** — fit, intent,
timing. A tiny local astrologer with a complete, verifiable profile scores high on the first and
may be a terrible prospect. A perfect-fit business with one Gmail address and nothing else scores
low on the first and may be excellent.

They are computed by different engines, from different inputs, and stored in different tables. A
test asserts they are independently distributed, because if a rich profile always meant a good
prospect, the system would quietly be ranking popularity.

### 3.2 Unmeasured is not zero, and unknown is not unreachable

A rubric factor that cannot be evaluated drops out of **both numerator and denominator** rather
than scoring zero. Scoring it zero punishes leads for work the operator has not done yet; scoring
it 0.5 invents a measurement. Every score records how many factors were actually evaluated, so a
partially-measured 80 is visibly a different claim from a fully-measured 80.

The same distinction runs through verification. An `UNREACHABLE` domain is a measurement: it
resolved and publishes no mail exchanger. An `UNKNOWN` domain is a *resolver timeout*. Collapsing
them lets one bad afternoon on your DNS silently mark thousands of good leads dead — and the
result gets cached, so it stays wrong.

### 3.3 Nothing is ever dropped

Not one validation rule rejects a row. Records with unusable fields still become leads; the
reasons land in `validation_issues` where they can be counted, filtered, and shown as explicit
gaps. This is what makes reconciliation possible, and reconciliation is the only proof that
ingestion worked:

```
rows_read == leads_total + rows_merged
```

The ingest CLI exits non-zero if that identity fails. A golden test asserts it over the real
file. `GET /api/v1/stats` reports it live.

### 3.4 Resemblance is not identity

Only *exact* identity keys — email, phone, Facebook URL — auto-merge. Shared websites and similar
names go to a review queue with a confidence and a `pending` status, for a human. Section 6
explains why in detail; the short version is that automatic merging on a shared website deletes
real prospects and looks correct doing it.

### 3.5 Every claim carries its provenance and its confidence

A city is not a string; it is a string, a gazetteer match, and the confidence of that match. A
normalised value traces back to the exact spreadsheet cell it came from. A score traces back to
the rubric version and git SHA that produced it. A generated sentence traces back to the page
that supports it.

### 3.6 Deterministic first, model second

Routing rule for every task: deterministic Python if it can do the job; a small local model if it
cannot; a larger model only when the small one's output fails schema validation or confidence is
low. This is a cost decision and an explainability decision at the same time.

### 3.7 Build the boundary before you need the queue

Modular monolith. No Celery, no Redis, no microservices — they would be over-engineering for a
2 520-row batch job. But the module boundaries are drawn so they can be added when a queue is
genuinely needed, rather than requiring a rewrite.

---

## 4. Architecture

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

  → verification            MX per domain · website liveness · SSRF-guarded, cached, TTL'd
  → rescore                 rubric v1.1, with unmeasured factors dropping out

  → HTTP API                filtered reads · explainable scores · human review queue
  → crawler                 owned domains only, robots-aware, same SSRF guard
  → chunking + embedding    semantic chunks, local embeddings, pgvector + BM25
  → hybrid retrieval        dense + sparse fusion, cross-encoder rerank
  → grounded RAG            citations, confidence, claim↔evidence mapping
  → scoring engines         ICP fit · intent · pain points → explained lead score
  → research agent          bounded state machine, every tool call logged
  → outreach                personalised drafts behind an approval workflow
  → evaluation              retrieval · groundedness · classification · ranking metrics
  → dashboard               Next.js over the same API
```

Everything is a **modular monolith** in one Python package, with a PostgreSQL 16 + pgvector
database and a Next.js frontend.

---

## 5. Ingestion

### 5.1 Sheet-aware reading

Each worksheet gets an explicit `SHEET_COLUMN_MAP`. An unknown column is a hard failure, not a
warning: silently ignoring a new column is how a dataset quietly loses a field between one export
and the next. `leadmind check-schema` runs this check alone.

### 5.2 Normalizers

One pure function per field, each returning `(value, confidence, method)`:

| Field | What it does |
|---|---|
| **email** | Lowercase, strip, typo-domain correction (`gamil.com` → `gmail.com`), freemail and role-account classification |
| **phone** | Parse to E.164 `+91XXXXXXXXXX` |
| **url** | Canonical host + path; a denylist of social, messaging, shortener, aggregator and marketplace hosts decides `is_owned_domain` |
| **followers** | `1.4K` / `2.3M` → integer |
| **city** | Gazetteer match with an address-fragment stoplist (`Nagar`, `Road`, `Vihar`), producing a confidence |
| **category** | `FB_Category` (240 distinct values) → a controlled vertical taxonomy via an alias map |
| **name** | Normalised form, plus `is_placeholder` for anonymised `Advertiser 13887200` names |

Pure functions, so each is unit-testable against the real edge cases the dataset actually
contains rather than invented ones.

### 5.3 Validators

Syntax → structural → semantic. Every failure is recorded as a `validation_issue` row with a
code, a severity and the offending value. `ERROR` severity never means "drop the row" — it means
the field is unusable and the reason is on the record.

### 5.4 Idempotence

Every raw row is stored as JSONB with a content hash. Re-ingesting the same workbook recognises
unchanged rows and updates rather than duplicates, so running ingest twice produces identical
database state. This is what makes the pipeline safe to re-run and safe to test.

Details: **[`docs/03-ingestion.md`](docs/03-ingestion.md)**.

---

## 6. Deduplication and company resolution

### 6.1 Two tiers, and the line between them

**Auto-merged** — union-find over exact identity keys: same email, same phone, same Facebook URL.
These are the same business by definition, and merging them is safe.

**Queued for a human** — shared website host, and fuzzy name similarity (`rapidfuzz
token_set_ratio`, threshold tuned against the known duplicate pairs as labelled data rather than
picked by feel). These land in `duplicate_candidates` with a confidence and a `pending` status,
and are **never merged automatically**.

### 6.2 Why a shared website is a relationship, not an identity

```
Pumo Technovation Kanchipuram      pumotechkanchipuram@gmail.com
Pumo Technovation Malumichampatti  pumotechnovationmalumichampatt@gmail.com
Pumo Technovation Tirupati         pumotechnovationtirupati@gmail.com
Pumo Technovation Bommasandra      pumotechnovationbommasandra@gmail.com
Pumo Technovation Poonamallee      pumotechpoonamallee@gmail.com
```

Five franchise branches on one domain, each with its own inbox, phone and city. Deduplicating on
website deletes four real prospects and looks correct doing it.

So the shared domain becomes a **company** with five leads attached. Nothing is lost, the
relationship is queryable, and the branches remain five separate prospects. A regression test
asserts they stay five.

### 6.3 The disagreement between duplicates is data

Across the cross-sheet duplicate pairs, follower counts agree only 61.9% of the time and
`Matched_Query` agrees only 36.9% of the time — the two scrapes happened days apart. That is
growth and discovery signal, not dirt.

So follower counts are stored as dated observations in `metric_observations` rather than a column
that gets overwritten on merge, and `Matched_Query` is a child table because it is genuinely
many-to-one.

`observed_at` stays **NULL** throughout, because the workbook carries no scrape dates. Inventing
one so a growth *rate* could be computed would fabricate the number it was meant to measure.
Supplying real dates in `config/sources.yaml` fills it in and switches the growth feature on.

### 6.4 Resolving the review queue

The API's review queue (§9.3) is where a person resolves what the pipeline refused to guess at.
Confirming a duplicate sets a pointer — `leads.merged_into_id` — rather than deleting a row, so
the decision is reversible and the reconciliation identity still holds.

The decisions are also **labelled data**: confirm rate per detection method is that detector's
observed precision, and it is what the fuzzy-name threshold should be tuned against.

---

## 7. Data quality scoring

A 0–100 score, defined entirely in `config/quality.yaml`, computed as a weighted sum of factors
in [0, 1], rescaled, then reduced by capped penalties.

**Factors** — identity present, contact reachable, mailbox verified, owned website, website live,
channel breadth, audience evidence, location resolved, category known, identity verifiable,
provenance observed.

**Penalties** — dead mailbox domain, dead website, email/website domain mismatch, thin record,
placeholder name, zero followers, corrected email typo, unusable city. Capped in total, so a
single bad record cannot go arbitrarily negative.

Three properties are worth naming.

**Every factor's value, weight, contribution and reason is persisted.** "Why is this an 87?" is
answerable by reading a row, without recomputation. Recomputing would answer a subtly different
question — what the *current* rubric would say — and would make old scores unexplainable the day
the rubric changes.

**Unmeasurable factors drop out.** Before verification has run, nothing is known about mailbox or
website reachability. Those factors return `null` and leave both numerator and denominator, and
the count of evaluated factors is stored alongside the score.

**Scores are versioned.** Changing the rubric requires bumping its version. Scores are stored
with the version that produced them, so v1.0 and v1.1 scores are comparable rather than silently
mixed. `ingest_runs` additionally stores the git SHA of the code, so any stored score traces to
the exact rules *and* the exact code that produced it.

**Audience is log-scaled, not linear.** The follower column spans 0 to 12 000 000; a linear scale
would make every SMB indistinguishable from every other.

---

## 8. Verification

Turning Phase 1's "unverified" placeholders into measurements.

### 8.1 Email domains, by MX lookup

Checked **per domain, not per address**. 2 520 addresses share far fewer domains, and `gmail.com`
alone accounts for over a thousand of them; deduplicating before dispatch turns thousands of DNS
queries into hundreds, and the cache means a second run costs nothing.

The MX hostnames also reveal *who runs the mailbox* — Google Workspace, Microsoft 365, Zoho,
GoDaddy. A business paying for managed email is a mild but real digital-maturity signal, which
matters a great deal in a dataset with no firmographics at all. Free providers are excluded from
that count: `gmail.com`'s MX is Google's, and counting it would turn every personal address into
a buying signal.

### 8.2 There is deliberately no SMTP callout

Port 25 is blocked from essentially every cloud provider and most ISPs; callouts get the sending
IP blacklisted; and catch-all domains plus Gmail's accept-then-bounce make a positive result
meaningless anyway. MX presence plus the local-part classification captures most of the signal at
none of the risk.

Verification is therefore **domain-level, not mailbox-level**: a verified domain accepts mail; it
does not prove that specific address exists. The API says so rather than implying otherwise.

### 8.3 Website liveness

An HTTP request per owned domain, distinguishing live, parked and dead. Concurrency is bounded
globally *and per host*, because the targets are small businesses' web hosting and a burst of
parallel requests is indistinguishable from an attack.

### 8.4 TTLs, and why they differ by outcome

A verification is a statement about a moment. Without expiry the system would keep presenting a
year-old DNS answer as current fact.

| Outcome | TTL | Why |
|---|---|---|
| `VERIFIED` | 30 days | A confirmed MX record is stable |
| `UNREACHABLE` | 7 days | Re-check sooner, in case it was a blip |
| `UNKNOWN` | 6 hours | This is an *absence* of measurement — an outage must not freeze into a cached non-answer |

Argument and design: **[`docs/04-verification.md`](docs/04-verification.md)**.

---

## 9. The API

FastAPI, mounted at `/api/v1`, with health probes deliberately outside the version prefix.

```
GET  /healthz  /readyz
GET  /api/v1/leads                        list · filter · sort · paginate
GET  /api/v1/leads/{id}                   evidence and gaps
GET  /api/v1/leads/{id}/quality           why this lead scored what it scored
GET  /api/v1/leads/{id}/provenance        the spreadsheet rows it came from
GET  /api/v1/companies  /companies/{id}   the franchise view
GET  /api/v1/duplicates                   the review queue
POST /api/v1/duplicates/{id}/decision     confirm · reject · undo
GET  /api/v1/stats  /quality  /verification  /review
GET  /api/v1/meta/categories  /meta/locations
```

### 9.1 The API does not smooth anything over

Locations carry match confidence. Mailboxes carry the three-way distinction between measured,
never-measured, and nothing-to-measure. Scores carry how many factors were evaluated and against
which rubric version. Each of those could have been flattened into a friendlier scalar, and each
would have made the API a more confident liar than the pipeline behind it.

### 9.2 Reads are cheap and deterministic

Every ordering ends in a tiebreak on `id`. Without it, two leads with the same quality score can
swap places between page 1 and page 2, and a reviewer paging through the list sees one twice and
the other never — silently, with no error anywhere.

A page costs a **constant** number of queries regardless of page size: the page is fetched first,
then each dependency (identifiers, observations, scores, verification, issue counts) is resolved
in one further query keyed on the page's ids. An integration test counts SQL statements and
requires a 100-row page to cost exactly as many as a 25-row page, because an N+1 regression
returns perfectly correct data and no other test would catch it.

### 9.3 The review queue is the only thing that writes

No endpoint edits a lead. Lead data comes from ingest, which is idempotent and reproducible from
the source workbook; letting an API mutate it would make the next re-ingest either destructive or
a merge conflict.

Each queue item embeds **both** leads and a pre-computed field-by-field diff, because a reviewer
who has to fetch two more URLs per row to see what they are deciding will not review anything,
and rendering two records and leaving the diff to the eye is how franchises get merged at 2am.

Confirming sets a pointer and touches nothing else — identifiers, observations, provenance and
validation issues all stay where they were. The lead vanishes from listings, stays fetchable at
its own URL, and returns under `?include_merged=true`. Setting a decided pair back to `pending`
reverses it completely. A review tool without an undo trains its reviewers to hesitate, which
produces worse decisions than a wrong one that can be corrected.

### 9.4 Errors are part of the interface

Every failure is RFC 9457 `application/problem+json`, carrying a `request_id` that also appears
in the server logs. A client that gets JSON for a 404, HTML for a 500 and a differently-shaped
JSON for a validation failure has to write three parsers and will write one.

An unhandled exception's message goes to the log; the client gets a stable code and the request
id. That text is as likely to contain a connection string as anything useful.

### 9.5 Liveness and readiness are two endpoints

`/healthz` says the process is running: if it fails, restart. `/readyz` says the process can
serve — database reachable **and** schema at the migration head this build expects: if it fails,
stop sending traffic but do *not* restart. A database blip is not a crash loop, and treating it
as one turns a two-minute outage into a rolling restart storm.

Full reference: **[`docs/05-api.md`](docs/05-api.md)**.

---

## 10. Knowledge layer: crawl, chunk, index

The point at which the system stops knowing only what the spreadsheet said.

**Crawl** — owned domains only, respecting `robots.txt`, rate-limited per host, inheriting the
same SSRF-guarded HTTP client the verification stage uses. A lead with no owned domain has
nothing to crawl and nothing to cite, which is precisely why `owned_website` is such a heavily
weighted quality factor.

**Chunk** — semantic chunking rather than fixed windows, preserving heading context so a
retrieved passage still knows what page and section it came from. Every chunk keeps its source
URL and position, because a citation that cannot be clicked is not a citation.

**Index** — two indexes over the same chunks. Dense vectors in pgvector for semantic similarity;
BM25 for lexical exactness. Neither alone is enough: dense retrieval misses exact product names
and phone numbers, sparse retrieval misses paraphrase.

**Retrieve** — hybrid fusion of both result sets, then a cross-encoder rerank of the top
candidates. The reranker is the expensive step and runs on a short list for exactly that reason.

---

## 11. Grounded RAG

Generation is constrained so that every claim maps to retrieved evidence.

- **Citations are mandatory.** A sentence without a supporting chunk is a failure of the
  generation step, not a stylistic preference.
- **Claim↔evidence mapping is explicit**, so the UI can show which passage supports which
  sentence rather than appending a list of links at the end.
- **Confidence is reported**, and low-confidence answers say so instead of hedging in prose.
- **Retrieved page content is data, never instruction.** The system prompt and the retrieved text
  are strictly separated: these pages are third-party HTML from the open web, and treating their
  content as instructions is prompt injection with extra steps.

---

## 12. Lead scoring: ICP, intent, pain

This is where "is this lead worth contacting" is answered — separately from, and never conflated
with, data quality.

**ICP fit.** Built on digital-maturity signals, because that is what the data supports:
currently-running Meta ads (a solvency proxy), owning a domain, multi-channel presence, an
established Facebook page with a vanity handle rather than a numeric ID, LinkedIn presence,
corporate rather than free email, and operational complexity such as multi-branch structure.
Negative signals — thin records, zero followers, placeholder advertiser names, numeric-only pages
— reduce **confidence** and, separately, some reduce **fit**. The two are never collapsed into
one number.

**Intent.** The strongest signal in the dataset is free: presence in the Meta Ad Library is a
dated buying signal, and `Matched_Query` says what they were advertising for. Crawled site
content adds hiring pages, new-service launches and expansion language.

**Pain points.** Derived from the knowledge layer with citations, so a claim about a prospect's
problem points at the page that supports it.

All weights live in versioned config, never inside a prompt. A score that changes because someone
edited a prompt string is a score nobody can reproduce.

---

## 13. The research agent

A bounded state machine, not an open-ended loop.

- A fixed set of states with explicit transitions and a hard step budget.
- Every tool call logged with its arguments, its result and its latency.
- Deterministic tools preferred; the model chooses *which* tool, not *what is true*.
- Failure is a state, not an exception: an agent that cannot finish reports what it established
  and what it could not, rather than guessing to fill the gap.

The reason for the state machine is auditability. An agent whose reasoning cannot be replayed
cannot be debugged, and an agent that cannot be debugged cannot be trusted with outbound
communication.

---

## 14. Outreach

Personalised drafts generated from the grounded knowledge layer, so every personalised sentence
traces to a citation rather than to a plausible-sounding invention.

Nothing sends automatically. Drafts enter an approval workflow, and the approving human sees the
evidence behind each claim next to the claim itself. This is a correctness control as much as a
courtesy one: a confidently wrong sentence about a prospect's business is worse than no outreach.

---

## 15. Evaluation

Metrics for every stage that can be wrong, measured rather than asserted:

| Stage | Metrics |
|---|---|
| Retrieval | Recall@k, MRR, nDCG |
| RAG | Groundedness, citation precision, answer relevance |
| Classification | Precision, recall, F1 per class |
| Ranking | nDCG, calibration of the lead score against outcomes |

The label set carries its provenance. The `Relevance` column that ships with `Day_1` is **weak
supervision** of unknown origin; `eval_labels.label_source` keeps it permanently separate from
hand-verified gold labels so the two can never be silently averaged into one metric. Weak labels
are useful for seeding and for regression detection; they are not ground truth, and a metric that
mixes them is a metric that flatters itself.

---

## 16. Dashboard

Next.js, over exactly the same public API — no private backdoor endpoints, so anything the UI can
show is something a client can fetch.

The interface is built around explanation rather than a leaderboard: a lead's score is shown with
its factors, its evidence and its gaps; the review queue is a first-class screen; and the filter
UI is driven by `/meta/categories` and `/meta/locations` so it always reflects the live taxonomy.

---

## 17. Production concerns

Docker packaging, background job execution, caching, authentication, rate limiting and CI/CD land
together, because they are one story rather than several. This is also the point at which a queue
is genuinely needed — the crawl and the embedding build are the first workloads that outlive a
request — and the module boundaries were drawn from the beginning so that adding one is a change
of transport, not a rewrite.

Authentication deliberately does not arrive earlier. A token check bolted onto a service that
binds to `127.0.0.1` is security theatre; the real control until then is the bind address, and
that is stated plainly rather than left to be discovered.

---

## 18. The model stack

Fully local, open weights, running on Apple Silicon via Metal.

| Role | Model | Why |
|---|---|---|
| Embeddings | `bge-m3` | Multilingual — real, given Hinglish and Devanagari content on these sites |
| Reranker | `bge-reranker-v2-m3` | Local cross-encoder, deterministic, no per-query cost |
| Extraction | `qwen2.5:7b-instruct` | Structured field extraction with JSON-constrained output |
| Hard reasoning | `qwen2.5:14b-instruct` | Only for the subset the 7B model cannot handle |

Everything sits behind `LLMProvider` and `EmbeddingProvider` protocols, so swapping to a hosted
API is a config change rather than a rewrite.

One caveat stated rather than hidden: a 7B local model is meaningfully worse at structured
extraction than a frontier API model. The evaluation harness measures that difference per model
instead of asserting it away.

---

## 19. Security

**No secrets in the repository.** `.env` is git-ignored and `alembic.ini` carries no connection
string — the URL comes from settings, so there is one source of truth and a migration can never
run against a database the application is not configured for.

**Scraped URLs are untrusted input.** Every URL in this dataset came from a spreadsheet somebody
else produced. `169.254.169.254` is cloud instance metadata; `127.0.0.1:5432` is your own
database. The SSRF guard refuses any host resolving to a non-public address — *after* DNS,
because a public hostname is free to point at loopback — and refuses ports outside
`{80, 443, 8080, 8443}` on sight. Redirects are capped and response bodies size-limited. The
crawler inherits the same client.

**Parsing does not touch the network.** The URL normalizer uses `tldextract`'s bundled
public-suffix snapshot and makes no network call at import or at runtime. A pipeline that
silently reaches the internet to parse a string is a pipeline that breaks in CI.

**Retrieved content is data, not instruction.** Strict separation of system prompt from retrieved
page text, throughout the RAG and agent layers.

**Native PostgreSQL enums.** Adding a value needs a migration — noisier than free text, but an
invalid value becomes impossible to write. For a system whose whole value is trustworthy data,
that trade is worth making.

---

## 20. Observability

`structlog` with JSON output, and a correlation id on every record:

- **`run_id`** is bound to every log line inside a pipeline run, so one ingest can be
  reconstructed from logs alone.
- **`request_id`** is bound to every log line inside an HTTP request, echoed as `X-Request-ID`,
  and included in every error body. An inbound header is honoured so a trace survives a proxy.

`ingest_runs` stores the git SHA, the rubric version and the full statistics of each run, so any
stored score can be traced to the exact code and rules that produced it. Logs go to stderr so
stdout stays a clean channel for machine-readable CLI output — mixing the two is how a pipeline
breaks a shell script.

---

## 21. Configuration

Two kinds of configuration, kept apart on purpose.

**Deployment** lives in the environment, prefixed `LEADMIND_`:

| Variable | Default |
|---|---|
| `LEADMIND_DATABASE_URL` | `postgresql+psycopg://leadmind:leadmind@127.0.0.1:5432/leadmind` |
| `LEADMIND_LOG_LEVEL` / `LEADMIND_LOG_JSON` | `INFO` / `true` |
| `LEADMIND_FUZZY_NAME_THRESHOLD` | `92` |
| `LEADMIND_API_PREFIX` | `/api/v1` |
| `LEADMIND_API_DOCS_ENABLED` | `true` |
| `LEADMIND_API_CORS_ORIGINS` | *(empty — CORS off)* |
| `LEADMIND_API_DEFAULT_PAGE_SIZE` / `_MAX_PAGE_SIZE` | `25` / `200` |

**Policy** lives in versioned YAML under `config/`, because it describes what the system believes
rather than where it runs:

| File | |
|---|---|
| `quality.yaml` | The data quality rubric — factors, weights, penalties, version |
| `taxonomy.yaml` | Controlled verticals and the alias map from raw Facebook categories |
| `gazetteer.yaml` | Locations, alternate spellings, address-fragment stoplist |
| `sources.yaml` | Source metadata, including optional real scrape dates |

Nothing that affects a score ever lives inside a prompt.

---

## 22. Running it

Requires Python 3.11+, Docker, and [uv](https://github.com/astral-sh/uv).

```bash
make install          # venv + dependencies
make db-up            # PostgreSQL 16 + pgvector
make migrate          # build the schema
make ingest-dry       # process everything, write nothing
make ingest           # persist
make verify-emails    # MX lookup per domain (cached; second run is instant)
make verify-websites  # HTTP liveness per owned domain
make rescore          # recompute quality scores with the new evidence
make serve            # API on http://127.0.0.1:8000 — docs at /docs
make check            # lint + type-check + full test suite
```

```
leadmind ingest data/raw/Outbound_Leads.xlsx [--dry-run] [--json]
leadmind check-schema data/raw/Outbound_Leads.xlsx
leadmind verify emails|websites|status
leadmind rescore data/raw/Outbound_Leads.xlsx
leadmind serve [--host 127.0.0.1] [--port 8000] [--reload]
leadmind config
```

`make ingest` prints a reconciliation report: rows in, rows merged, leads out, review queue,
validation issues by code, and the quality distribution. It exits non-zero if the reconciliation
identity fails.

`leadmind serve` binds to localhost. There is no authentication until the production phase, so
binding to `0.0.0.0` publishes the corpus to the network.

---

## 23. Testing philosophy

**Test cases come from the dataset, not from imagination.** `1.4K`, `gamil.com`,
`http://www.bellsoverseas/`, `Advertiser 13887200`, `Nagar`, `wa.me/...` — every one of those is
a real value from the real file.

**Unit tests need no database.** Normalizers are pure functions. Query construction is tested by
compiling SQL and inspecting it: that merged leads are excluded by default, that every sort ends
in the id tiebreak, that a hostile `sort` value falls back instead of reaching SQL, that search
text arrives as a bound parameter. All of that is decided at compile time, so it runs in seconds.

**Integration tests use a real PostgreSQL**, in a dedicated `*_test` database that is created and
migrated on demand, inside transactions that are rolled back. Website verification is tested
against a real local HTTP server rather than a mocked transport — actual sockets, actual
redirects, actual timeouts — because a mock only proves the mock returns what it was told to.

**Network-dependent results are seeded, not measured.** A test must not depend on DNS, but a
filter reading `domain_verifications` cannot be tested against a table nobody wrote to: it would
pass by returning nothing, which is exactly the bug it exists to catch.

**Some tests exist to catch things no other test can.** The golden test asserts the reconciliation
totals over all 2 520 real rows. A regression test asserts the five franchise branches survive as
five leads. A test asserts data quality and lead quality are independently distributed. A query
counter asserts a 100-row page costs exactly as many SQL statements as a 25-row page — because an
N+1 regression returns perfectly correct data, slowly, and nothing else would notice.

```bash
make test-unit      # no database
make test           # full suite
```

---

## 24. Repository layout

```
leadmind/
├── backend/app/
│   ├── core/           settings, structlog with a correlation id, typed errors
│   ├── db/             engine, session, alembic migrations
│   ├── models/         SQLAlchemy 2.0 typed models
│   ├── schemas/        Pydantic v2 request and response models
│   ├── services/       query building, serialisation, decisions — no FastAPI imports
│   ├── api/            routers, dependencies, error handlers, middleware
│   ├── verification/   SSRF-safe HTTP client, async DNS, MX and liveness checks
│   └── ingestion/
│       ├── readers/        sheet-aware Excel reader + per-sheet column maps
│       ├── normalizers/    one pure function per field
│       ├── validators/     record-level rules
│       ├── dedup/          union-find, candidate detection
│       ├── resolution/     cluster merge, company resolution
│       ├── quality/        the scoring rubric
│       └── pipeline.py     orchestration and persistence
├── backend/tests/      unit (no DB) + integration (real PostgreSQL)
├── config/             quality · taxonomy · gazetteer · sources
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
- **`lead_source_queries` is a child table**, because `Matched_Query` is genuinely many-to-one.
- **`metric_observations` stores dated measurements**, so a merge accumulates history instead of
  overwriting it.
- **`leads.merged_into_id` is a pointer, not a deletion.** A reviewer's decision is reversible and
  the reconciliation identity survives it.
- **`eval_labels.label_source`** separates weak supervision from hand-verified gold labels so
  they can never be averaged together.

### The services/api split

Routers do HTTP: parse, validate, serialise, choose a status code. Everything that decides what
is *true* lives in `services`, where it can be called from a test, a CLI command or a background
job without a request object in sight. Schemas are kept separate from the ORM models for a
related reason: a schema generated from the ORM leaks every column the database happens to have —
surrogate keys, internal flags, anything added by tomorrow's migration — into a public interface
that then cannot change without breaking clients.

---

## 25. Documentation

- [`docs/01-dataset-analysis.md`](docs/01-dataset-analysis.md) — the full dataset profile, every number measured
- [`docs/02-phase1-plan.md`](docs/02-phase1-plan.md) — the ingestion plan and the constraints behind it
- [`docs/03-ingestion.md`](docs/03-ingestion.md) — how ingestion works, normaliser by normaliser
- [`docs/04-verification.md`](docs/04-verification.md) — verification design, the SMTP decision, TTL policy
- [`docs/05-api.md`](docs/05-api.md) — the HTTP interface, the review queue, error contract, operations
- [`docs/openapi.json`](docs/openapi.json) — the generated OpenAPI schema (`make openapi`)
