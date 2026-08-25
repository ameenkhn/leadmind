# Ingestion — how it works and why

Phase 1 is entirely deterministic Python. No LLM is involved at any point, and none should be:
every decision here is one that code makes better, faster, and reproducibly.

```
Outbound_Leads.xlsx
  │
  ├─ ExcelLeadReader          per-sheet column maps; unknown column ⇒ hard failure
  ├─ normalize_record         7 field normalizers, each returning (value, confidence, method)
  ├─ validate_record          record-level rules; nothing is ever dropped
  ├─ deduplicate              union-find on exact identity keys; resemblance ⇒ review queue
  ├─ merge_cluster            confidence-ranked scalars, accumulated observations
  ├─ resolve_companies        owned-domain keyed, franchise-aware
  ├─ score_lead               config-driven rubric, every reason persisted
  └─ _persist                 idempotent upserts against natural keys
```

Run it:

```bash
make db-up && make migrate
make ingest-dry     # process everything, write nothing
make ingest         # persist
```

---

## 1. Reader — the schema is declared, never guessed

`backend/app/ingestion/readers/column_map.py` holds one `SheetSpec` per worksheet. A column the
spec has not heard of raises `SchemaMismatchError` rather than being ignored, because an
unannounced column almost always means the upstream export changed and continuing would either
drop the new field or mis-map it.

The mapping that justifies the whole design:

| Sheet | Column | Canonical field |
|---|---|---|
| Day_1 | `Niche` | `niche` — five curated verticals |
| Day_3 | `Niche` | **`fb_category`** — 131 Facebook page categories |

74 of Day_3's `Niche` values appear verbatim in Day_1's `FB_Category`. Mapping the two columns
together on the strength of a shared header would corrupt every vertical feature downstream.
`test_day3_niche_is_mapped_to_fb_category` exists so this can never silently regress.

Day_3 also has no `Source` column. It is filled in as `Meta Ad Library` and marked
`source_is_inferred`, so an inference never reads back as an observation.

---

## 2. Normalizers — value, confidence, and method

Every normalizer is a pure function returning `NormalizationResult`: the value, the raw input,
a confidence, the method that produced it, and any issues raised. None of them raises on bad
input, because "this phone number is unusable" is information to keep, not an exception to
swallow.

| Field | What it actually does | Measured on this dataset |
|---|---|---|
| `email` | lowercase, trim, correct known typo domains, classify mailbox | 0 invalid syntax; 51.8% freemail; 30.8% role; 5 typo domains corrected |
| `phone` | libphonenumber → E.164, line type | 100% parse, all `+91`, all mobile-range |
| `url` | canonical host+path, tracking params stripped, **owned-domain test** | 2 070 present → 1 995 owned; 75 are aggregator/social/messaging links |
| `followers` | K/M/B parser, `log1p` precomputed, exactness flagged | 2 314 present, 0 failures |
| `name` | fold case/punctuation, strip legal suffixes, entity-kind guess | 6 placeholders; 1 101 business / 85 person / 1 334 unknown |
| `city` | gazetteer + address-fragment stoplist | 87.7% resolved; 219 rejected as fragments or non-places |
| `category` | alias map → 15 controlled verticals | 98.6% of rows with a category mapped |

Three of these deserve their reasoning spelled out.

**`url.is_owned_domain`.** The useful predicate is not "is a URL present" but "is this a domain
the lead controls and that we can crawl for evidence". 26 rows point at `linktr.ee`, 19 at
`threads.com`, 11 at `wa.me`. Counting those as websites would inflate every coverage statistic
and send the Phase 3 crawler at pages that say nothing about the business.

**`city` does no fuzzy matching.** Edit distance would map `Kunj` to `Kanpur` and invent a
location the source never contained. Address fragments (`Nagar` ×24, `Road` ×9, `Colony` ×7) and
non-places (`PNB`, `Dental Hospital`, `quality`) are rejected outright; unknown values stay
unresolved with the raw text preserved in `leads.location_raw`.

**`followers` is stored exactly but scored logarithmically.** The column spans 0 to 12 000 000.
On a linear scale one outlier would dominate every weight in the rubric.

---

## 3. Validators — nothing is ever dropped

Record-level rules judge what field-level checks cannot: is there any way to contact this lead,
does the email domain agree with the website, is there anything here to research at all.

Not one rule rejects a row. A record that fails every check still becomes a lead; the evidence
of its weakness lands in `validation_issues` where it can be counted, filtered, and shown in the
UI as an explicit gap rather than an absence. This is what makes the reconciliation identity
possible, and reconciliation is the only proof that ingestion worked.

`thin_record` (256 leads: no owned website, no social profile, under 100 followers) reduces
**data confidence**, not lead quality. The two are different numbers and are kept apart
deliberately.

---

## 4. Deduplication — two tiers, and the boundary is the design

**Auto-merge — exact identity keys only.** Normalised email, E.164 phone, canonical Facebook
URL. Union-find over these collapses 2 520 rows into 2 351 leads: 169 duplicate pairs, every one
of them spanning Day_1 ∩ Day_3 (Day_2 is disjoint from both).

**Review queue — resemblance, not identity.** Shared website host and high name similarity go to
`duplicate_candidates` with a confidence and a `pending` status. They are never merged.

The case that settles it:

```
Pumo Technovation Kanchipuram      pumotechkanchipuram@gmail.com
Pumo Technovation Malumichampatti  pumotechnovationmalumichampatt@gmail.com
Pumo Technovation Tirupati         pumotechnovationtirupati@gmail.com
Pumo Technovation Bommasandra      pumotechnovationbommasandra@gmail.com
Pumo Technovation Poonamallee      pumotechpoonamallee@gmail.com
```

Five franchise branches on one corporate domain, each with its own inbox, phone and city — five
real prospects. Merging on shared website deletes four of them and looks correct while doing it.
So a shared host creates a **company relationship** plus a review-queue row, and nothing else.

Result: 1 826 companies, 28 of them multi-branch, 73 pairs queued (48 shared-website, 25 fuzzy).
The fuzzy threshold (`token_set_ratio ≥ 92`) is tuned against the 169 known duplicate pairs as
labelled data rather than picked by feel.

---

## 5. Merge — the duplicates disagree with themselves

Across the 169 pairs: Facebook URL and city agree 99.4%, name 98.8%, website 89.3%, phone
86.3% — but **follower count only 61.9%** and **matched query only 36.9%**.

So the merge is not "take the newest row and discard the rest":

- **Scalars** take the highest-confidence value, tie-broken by earliest sheet.
- **Follower counts are never overwritten.** Each source row contributes its own row in
  `metric_observations`. 168 leads carry two observations; 63 of those disagree. That
  disagreement is growth data — the only longitudinal signal in the file — and collapsing it to
  one column would destroy it.
- **Matched queries accumulate** into `lead_source_queries`. The same business found by two
  different searches tells you two things about it, not one thing twice.
- **Identifiers accumulate.** A row missing a LinkedIn URL does not erase one another row has.

`observed_at` stays **NULL**. The workbook records no scrape dates, and inventing one so a growth
*rate* could be computed would fabricate the very number it was meant to measure. Fill real dates
into `config/sources.yaml` and the feature switches on.

---

## 6. Data quality score — a rubric, not a judgement

`config/quality.yaml`, version-stamped. Nine weighted factors summing to 100, then capped
penalties. Every factor's value, weight, contribution and human-readable reason is persisted in
`data_quality_scores.factors`, so the "Why 87?" panel reads stored facts instead of recomputing —
and a score stays interpretable after the rubric changes.

Observed distribution: mean 69.4, median 72.8, range 7.8–100.

**This is not a lead score.** It answers "how much do we reliably know about this record". A
small local astrologer with a complete, verifiable profile scores high and may still be a
terrible prospect. `test_score_measures_completeness_not_desirability` asserts the two are
independently distributed, because if a rich profile always meant a good prospect the system
would quietly be scoring popularity.

---

## 7. Persistence — idempotent by construction

Running the same workbook twice produces the same database, not two copies of it. Leads are
matched to existing rows through their exact identity identifiers rather than insertion order;
every child table has a natural uniqueness key and is upserted against it.

Verified: after a second run every table count is byte-identical, `leads_created` is 0 and
`leads_updated` is 2 351. Only `ingest_runs` grows, which is correct — both runs happened.

Everything already in the database is loaded once into `_Store` and matched in memory. A lookup
per lead per child table would be roughly 15 000 round trips; this keeps the run linear in rows
rather than in rows × tables. Full ingest: ~23 s.

---

## 8. Reconciliation

```
rows_read == leads_total + rows_merged
2520      == 2351        + 169
```

The CLI exits non-zero if this fails, and `test_full_dataset_reconciles` asserts it against the
real file. Every source row is also traceable: `lead_source_records` holds all 2 520 raw payloads
as JSONB with a content hash, none orphaned, and all 2 351 leads have at least one.

---

## 9. What is deliberately not done yet

| Not done | Why | Phase |
|---|---|---|
| Email deliverability (MX/SMTP) | Network check; `deliverability` is recorded as `unverified` rather than assumed | 1b |
| Website liveness | Same; `liveness` is `unverified` | 1b |
| Follower growth rate | Needs real scrape dates; see §5 | 1b |
| Firmographics | The source contains none at all | 3 |
| Human-verified eval labels | 900 weak labels are seeded and tagged; ~200 need hand-verification | 8 |
