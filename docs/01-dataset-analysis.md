# LeadMind — Phase 0: Dataset Analysis

**Source file:** `Outbound_Leads.xlsx` (462 KB)
**Analysed:** 2026-08-25 · every number below is measured, not estimated.
**Profiling scripts:** `scripts/profiling/` (reproducible)

---

## 1. Structure

Three sheets with **three different schemas**. This is the first real finding: there is no single
table here, and treating it as one is where a naive ingest would go wrong.

| Sheet | Rows | Cols | Notes |
|---|---:|---:|---|
| `Day_1` | 900 | 17 | Richest. Has `Relevance`, `WhatsApp`, curated `Niche`, `Matched_Query` |
| `Day_2` | 1000 | 13 | No `Niche`, no `Matched_Query` |
| `Day_3` | 620 | 13 | No `Source`, no `FB_Category` |
| **Total** | **2520** | 17 (union) | |

### Column availability matrix

| Column | Day_1 | Day_2 | Day_3 |
|---|:--:|:--:|:--:|
| S.No / S.No. | ✓ | ✓ (renamed) | ✓ (renamed) |
| Name, Email, Phone, Facebook | ✓ | ✓ | ✓ |
| Website, Instagram, YouTube, LinkedIn, City, Followers | ✓ | ✓ | ✓ |
| FB_Category | ✓ | ✓ | — |
| Niche | ✓ (curated) | — | ✓ (**different meaning**) |
| Matched_Query | ✓ | — | ✓ |
| Source | ✓ | ✓ | — |
| Relevance | ✓ | — | — |
| WhatsApp | ✓ (6 rows) | — | — |

### ⚠ Schema collision: `Niche` means two different things

- `Day_1.Niche` — 5 curated business verticals:
  `Health & Wellness` 318, `Life Coaching` 265, `Finance` 154, `Occult Healing` 153, `Disease Reversal` 10.
- `Day_3.Niche` — 131 distinct values that are **Facebook page categories**
  (`Education website`, `Educational consultant`, `Nutritionist`, `Yoga studio`, `App Page`…).
  74 of them appear verbatim in `Day_1.FB_Category` and 80 in `Day_2.FB_Category`.

**Consequence:** `Day_3.Niche` must be ingested as `fb_category`, **not** as `niche`. Merging these
two columns on name alone would poison every downstream ICP/vertical feature. This is exactly the
kind of thing "don't assume the schema" was meant to catch.

---

## 2. What the data actually is

Not B2B SaaS contacts. This is **Indian SMB / creator-economy advertisers** — coaches, clinics,
astrologers, training institutes, financial advisors — harvested from the **Meta Ad Library**
(`Source = "Meta Ad Library"` for all 1900 rows that carry the column).

Three consequences that shape the whole product:

1. **`Name` is a Facebook Page name, not a person.** ~35% contain a business token
   (`institute`, `clinic`, `pvt`, `academy`…); the rest are personal brands
   (`Amulya Kamarapu`, `geet india`). There is **no separate person name, no job title**.
   The lead↔company split in the spec's schema is therefore *derived*, not given.
2. **Zero firmographics.** No employee count, revenue, funding, tech stack, industry code, or
   founded date exist anywhere in the file. Every one of those must come from enrichment or be
   dropped from the ICP model. Do not design a scoring formula that assumes them.
3. **Every lead is a paying advertiser.** Presence in the Meta Ad Library is itself a hard,
   dated, verifiable buying-intent signal — the single most valuable thing in this dataset and
   free of charge. `Matched_Query` tells you what they were advertising *for*.

---

## 3. Field-by-field quality

### Email — clean syntax, weak deliverability
- 2520/2520 present, **0 syntactically invalid**.
- 2352 unique → **168 duplicate groups covering 336 rows**.
- **51.8% freemail** (gmail.com alone: 1269). Corporate-domain leads are the minority.
- **30.8% role-based** local parts (`info@`, `contact@`, `support@`, `sales@`…) — reachable but not
  a decision maker, and materially worse for personalised outreach.
- Lookalike/typo domains found: `gamil.com` (3), `gmail.om` (2). Real, silent bounces.
- **Not verified:** no MX/SMTP check has been run. Syntax validity ≠ deliverability.

### Phone — uniform, low information
- 2520/2520 present, **100% `+91 XXXXXXXXXX`, all exactly 12 digits.** No format cleanup needed
  beyond E.164 normalisation.
- 146 duplicate groups / 292 rows.
- Because it is 100% present and 100% uniform it carries **no discriminative scoring signal** —
  it is a dedup key and a contact channel, nothing more.

### Website — 82% present, but 3% aren't websites
- 2070/2520 present (82.1%).
- **69 are not owned domains**: `linktr.ee` (26), `threads.com` (19), `wa.me` (11), `t.me` (3),
  `share.google` (3), `youtu.be` (3), `amazon.in` (3), plus a few social/shortener hosts.
- **→ 2001 rows (79.4%) have a scrapable owned domain.** That is the size of the RAG corpus.
- 156 host duplicate groups / 375 rows — but see §4, most are *not* duplicates.

### Facebook — 100% present, half of it anonymous
- 2520/2520 present, 2352 unique.
- **912 rows (36%) are numeric-ID pages** (`facebook.com/61585557833996`) with no vanity handle —
  typically brand-new pages. Weak entity identity, and a mild spam/low-maturity signal.

### Followers — usable after parsing, wildly skewed
- 2314 present (91.8%), 206 missing. Mixed types: raw ints **and** strings with `K`/`M` suffixes
  (`1.4K`, `173K`). All 2314 parse cleanly with a K/M parser — 0 failures.
- Distribution: min 0 · p25 92 · **median 1 000** · p75 7 500 · p95 129 000 · max 12 000 000.
- **25.6% under 100 followers; 8.9% under 10; 42 rows at exactly 0.**
- Must be log-scaled before use as a feature; raw values would let one 12M outlier dominate.

### City — present but unreliable
- 1917 present (76.1%), **417 distinct values, 259 of them appearing exactly once.**
- Clearly extracted from a free-text address, not a geo field. Contains address fragments as
  "cities": `Nagar` (24), `Vihar`, `Road`, `Colony`, `Marg`, `Block`, `Floor`, `Puram`, `Enclave`,
  `extension` — and outright non-places: `Tamilnadu`, `PNB`, `Dental Hospital`, `Diagnostics`,
  `Center`, `Farm`.
- Head of distribution is sound (Mumbai 186, Delhi 124, Bangalore 118, Hyderabad 87, Pune 81…).
- **Needs a gazetteer-backed resolver with a confidence score**, not a `.strip().title()`.

### Category taxonomy
- `FB_Category`: 240 distinct across sheets (Education 154, Educational consultant 142,
  Medical & health 83, Astrologist 57…). Facebook's own taxonomy — noisy, long-tailed, but real.
- Needs mapping to a **controlled internal vertical taxonomy**; the 5 `Day_1.Niche` values are a
  usable seed for that mapping.

### Matched_Query — provenance, and it is many-to-one
- 1520 rows (Day_1 + Day_3), 171 unique, 86% suffixed `India`
  (`career counsellor India` 74, `astrologer India` 73, `nutritionist India` 65…).
- **Critically: it is not a property of the lead.** In the 168 rows that appear in both Day_1 and
  Day_3, `Matched_Query` matches only **36.9%** of the time — the same business was found by two
  different queries. It belongs in a **`lead_source_queries` child table**, not a column on `leads`.

### Relevance — a free labelled seed set 🎁
- Day_1 only: **High 573 / Medium 290 / Low 37.**
- Provenance unknown (hand-labelled? heuristic?), so it cannot be trusted as ground truth —
  **but 900 pre-labelled rows are an excellent starting point for the §23 evaluation set.**
  Plan: treat it as a weak label, hand-verify a stratified sample of ~200, and keep
  `label_source` on every eval row so weak and gold labels never get mixed.

### Name
- 2352 unique / 2520. 170 normalised-name duplicate groups (340 rows).
- **6 placeholder names**: `Advertiser 13887200` etc. — Meta anonymised advertisers. Flag, don't drop.

---

## 4. Duplicates — and the trap inside them

Union-find over four keys (`email` ∪ `phone` ∪ `facebook` ∪ owned-website-host):

```
2520 rows → 2320 clusters  (200 rows collapsible, 7.9%)
cluster sizes:  1 × 2130   2 × 182   3 × 7   5 × 1
```

**Every one of the 168 cross-sheet duplicates is Day_1 ∩ Day_3.** Day_2 is fully disjoint from both.

Field agreement inside those 168 pairs — this is the interesting part:

| Field | Identical |
|---|---:|
| Facebook | 99.4% |
| City | 99.4% |
| Name | 98.8% |
| Website | 89.3% |
| Phone | 86.3% |
| **Followers** | **61.9%** |
| **Matched_Query** | **36.9%** |

Followers disagree because the two scrapes happened on different days. That is not dirty data —
**it is a time series**, and it is the only longitudinal signal in the file. Overwriting it on
merge destroys a real growth-rate feature. Store follower counts as dated observations.

### ⚠ The website-host key is unsafe on its own

The single 5-row cluster is:

```
Pumo Technovation Kanchipuram      pumotechkanchipuram@gmail.com     pumotechnovation.com
Pumo Technovation Malumichampatti  pumotechnovationmalumichampatt@…  pumotechnovation.com
Pumo Technovation Tirupati         pumotechnovationtirupati@gmail…   pumotechnovation.com
Pumo Technovation Bommasandra      pumotechnovationbommasandra@…     pumotechnovation.com
Pumo Technovation Poonamallee      pumotechpoonamallee@gmail.com     pumotechnovation.com
```

Five **distinct franchise branches**, each its own lead, sharing one corporate site. Merging them
loses four real prospects. Shared website is a **company relationship**, not identity — it should
create one `companies` row with five `leads` attached. Of the 375 rows sharing a website host,
this pattern accounts for a meaningful share.

**Therefore:** auto-merge on `email` / `phone` / `facebook_url` only. Website-host and fuzzy-name
matches go to a `duplicate_candidates` review queue with a confidence score, never a silent merge.
This directly satisfies §5's "do NOT automatically merge uncertain records".

---

## 5. Completeness & junk

Across 11 substantive fields:

| Fields present | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| Rows | 6 | 65 | 291 | 539 | 751 | 310 | 361 | 197 |

Mean 8.11 / 11.

- **260 "thin" leads** — no owned website, no LinkedIn, under 100 followers. Almost nothing to
  research and nothing to ground a RAG answer in. These should score low on *data confidence*
  (§17) rather than low on *lead quality* (§13) — the distinction the spec insists on, and this
  dataset makes it concrete.
- 583 rows have **both** an owned website and a LinkedIn URL — the highest-yield enrichment tier.
- Suspicious/spam: 6 placeholder advertiser names, 42 zero-follower pages, 5 typo-domain emails,
  912 numeric-ID Facebook pages. Nothing looks maliciously injected; the noise is scraper artefacts.

---

## 6. Feature inventory

### Available today, deterministic (no LLM, no network)

| Feature | Basis | Coverage |
|---|---|---:|
| `log_followers` | Followers, K/M-parsed, log1p | 91.8% |
| `follower_growth_rate` | multi-date observations (Day_1↔Day_3) | 6.7% now, grows |
| `has_owned_website` | host not in social/shortener denylist | 79.4% |
| `channel_breadth` | count of IG/YT/LI/site present | 100% |
| `has_linkedin` | LinkedIn present | 22.3% |
| `email_is_corporate` | domain ∉ freemail list | 48.2% |
| `email_is_role_based` | local part ∈ role list | 30.8% |
| `email_domain_matches_website` | domain == site host | computable |
| `fb_page_is_established` | vanity handle vs numeric ID | 64% |
| `city_resolved` + `city_confidence` | gazetteer match | 76.1% raw |
| `category_vertical` | FB_Category → controlled taxonomy | 90.4% |
| `is_active_advertiser` | Source = Meta Ad Library | 75.4% |
| `query_intent_class` | Matched_Query → intent bucket | 60.3% |
| `completeness` | 11-field fill ratio | 100% |
| `dup_cluster_size` | union-find | 100% |

### Requires enrichment (network)
Employee count, revenue, funding, tech stack, decision-maker name/title, email deliverability
(MX/SMTP), website liveness, ad creative + ad run dates from Meta Ad Library, recent news,
job postings.

### RAG corpus sources, in order of value
1. **Website pages** — home / about / services / pricing / contact — 2001 sites. The backbone.
2. **Meta Ad Library ad copy & creatives** — what they *say they sell*, dated, and a direct intent
   and pain-point source. Highest signal-to-effort ratio in this dataset.
3. LinkedIn company about (583 rows).
4. YouTube channel/video titles + descriptions (~788 rows) — service language in their own words.
5. Instagram bio (~1003 rows) — short but dense.

Row metadata (name, category, city, followers) is **structured** — it belongs in Postgres and in
SQL/metadata filters, **not** embedded as a pseudo-document. Embedding a spreadsheet row is the
anti-pattern §8 explicitly forbids.

---

## 7. Recommended schema (grounded in what's actually here)

```
companies              — resolved by owned-website host; 1 : N leads (franchise-safe)
leads                  — one per Facebook page / advertiser identity
lead_identifiers       — (lead_id, kind ∈ email|phone|facebook|instagram|youtube|linkedin|website,
                          value_raw, value_normalized, is_primary)   ← replaces 8 sparse columns
lead_source_records    — one row per (sheet, S.No) originally ingested; raw JSONB payload kept
lead_source_queries    — (lead_id, matched_query, source, observed_at)  ← many-to-one, see §3
metric_observations    — (lead_id, metric='followers', value, observed_at)  ← the time series
categories             — controlled vertical taxonomy + fb_category alias map
locations              — gazetteer; leads.location_id + location_confidence
data_quality_scores    — score + per-factor JSONB breakdown + rubric_version
duplicate_candidates   — (lead_a, lead_b, method, confidence, status)  ← review queue, no auto-merge
documents / chunks     — chunk embeddings (pgvector) + tsvector (BM25) + metadata JSONB
evidence, lead_scores, research_runs, outreach, feedback   — per spec §7
eval_labels            — (lead_id, label, label_source ∈ weak_relevance|human_gold, labeller, at)
```

Design notes worth defending in an interview:

- **`lead_identifiers` as a table, not columns.** Eight sparse URL columns become one indexed table;
  dedup becomes a join instead of eight `OR`s; adding a channel is a row, not a migration.
- **`metric_observations`, not `leads.followers`.** Preserves the 61.9% disagreement as growth data.
- **`lead_source_records` keeps raw JSONB.** Re-ingestion is idempotent and every normalised value
  stays traceable to its original cell — the provenance §35 demands.
- **`companies` keyed on website host, with N leads.** The Pumo case is designed for, not patched.

---

## 8. Recommended ingestion pipeline

```
Outbound_Leads.xlsx
   ↓  sheet-aware reader (per-sheet column map; Day_3.Niche → fb_category)
   ↓  raw landing → lead_source_records (JSONB, content-hashed, idempotent)
   ↓  normalisers: email · phone(E.164) · URL(host+path canonical, denylist) ·
                   followers(K/M) · name · city(gazetteer) · category(alias map)
   ↓  validators: syntax → structural → semantic; every failure recorded, none silently dropped
   ↓  dedup: exact keys auto-merge · fuzzy/host → duplicate_candidates queue
   ↓  company resolution (host-based, franchise-aware)
   ↓  data quality score (rubric-versioned, per-factor reasons persisted)
   ↓  Postgres
```

Everything above is **deterministic Python** — no LLM anywhere in Phase 1. Per §35, an LLM has no
business validating an email address.

---

## 9. Assumptions made (flag anything you disagree with)

1. `Day_3.Niche` is FB-category data → ingested as `fb_category`. (Evidence: 74/131 exact overlap.)
2. Day_3's missing `Source` is also `Meta Ad Library` — recorded as `inferred`, not `observed`.
3. `Relevance` is a **weak** label, unusable as ground truth until human-verified.
4. All leads are India / `+91` → geography is not a discriminating ICP feature for this dataset.
5. `S.No` restarts per sheet and is not a stable ID → surrogate UUIDs; `(sheet, S.No)` kept for trace.
6. Website liveness and email deliverability are **unknown** — both need async network checks in
   Phase 1b, and neither should be assumed true in scoring until measured.

---

## 10. Open questions that change the architecture

These materially affect the data model and scoring, so they are worth answering before Phase 2:

1. **What is being sold to these leads?** The ICP engine, pain-point engine and outreach are
   all defined relative to an offer. The spec's example ICP (SaaS, 50–500 employees, HubSpot)
   does not describe this dataset at all.
2. **Is outbound web scraping in scope?** ~2000 sites × ~5 pages. Without it the knowledge layer
   has almost nothing to retrieve and the RAG half of the project is decorative.
3. **Which LLM + embedding provider, and what monthly budget ceiling?** Determines model routing,
   caching aggressiveness, and whether reranking is cross-encoder or LLM-based.
