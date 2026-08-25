# Verification — turning "unverified" into a measurement

Phase 1 deliberately refused to guess. Every email carried `deliverability: unverified` and every
website carried `liveness: unverified`, because syntactic validity is not reachability and
recording a guess as a fact is how a lead database stops being trustworthy.

Phase 1b replaces those placeholders with measurements.

```bash
make verify-emails                  # MX lookup per domain
make verify-websites                # HTTP liveness per owned domain
make rescore                        # recompute quality scores with the new evidence
leadmind verify status              # what is verified, and what has gone stale
```

---

## 1. The distinction the whole layer is built around

```
VERIFIED      the domain publishes MX  →  a measurement
UNREACHABLE   NXDOMAIN, or no MX       →  a measurement
UNKNOWN       resolver timed out       →  the ABSENCE of a measurement
SKIPPED       we declined to look      →  the absence of a measurement
```

`UNREACHABLE` and `UNKNOWN` must never collapse into each other. The first is evidence; the
second is a network blip. Fold them together and one bad afternoon on your resolver silently
marks thousands of good leads undeliverable — and because the result gets cached, it stays wrong.

That distinction propagates all the way to the score: an `UNKNOWN` mailbox leaves the
`mailbox_verified` factor **unmeasured**, which drops it out of the rubric entirely rather than
scoring it zero. `test_resolver_failure_is_treated_as_unmeasured_not_as_a_dead_domain` asserts
that a resolver failure produces exactly the same score as never having checked at all.

---

## 2. Email: MX, and deliberately not SMTP

### What runs

An MX lookup per domain, async, through a `MxResolver` protocol so tests inject a deterministic
stub instead of depending on the internet.

### Why there is no SMTP callout

The obvious next step — connect to the MX on port 25 and issue `RCPT TO` — is **not implemented**,
and that is a decision rather than an omission:

- **It usually cannot run.** Outbound port 25 is blocked by essentially every cloud provider and
  most residential ISPs — including both environments this project was built in. A feature that
  silently degrades to `UNKNOWN` everywhere is worse than no feature.
- **It damages the sender.** Repeated callouts from one IP get it greylisted, then blacklisted.
  The cost lands on the mail reputation of whoever runs the check.
- **It is frequently wrong anyway.** Catch-all domains accept every address, so a positive result
  means nothing. Gmail and Microsoft accept at RCPT and bounce later, so a positive result means
  nothing there either.

MX presence plus the Phase 1 classification (freemail, role account, disposable, typo domain)
captures most of the usable signal at none of the risk. If mailbox-level verification is ever
genuinely needed, the right answer is a specialist provider with a warmed IP pool sitting behind
the same `DomainVerification` interface — not a socket in this repository.

### Why per domain, not per address

2,352 addresses share **1,103 domains**, and `gmail.com` alone accounts for 1,203 of them.
Checking per address would be 2,352 queries for 1,103 distinct answers.

| | |
|---|---:|
| Distinct domains | 1,103 |
| Addresses covered | 2,352 |
| **Queries saved by domain-level dedup** | **1,249** |
| First run | 22s |
| Second run (all cached) | **0.2s** |

### Measured results

| Outcome | Domains | Addresses |
|---|---:|---:|
| `VERIFIED` — publishes MX | 1,044 | 2,293 |
| `UNREACHABLE` — NXDOMAIN or no MX | 34 | 34 |
| `UNKNOWN` — resolver failed | 25 | 25 |

**34 leads cannot receive email.** Roughly half are NXDOMAIN (the domain was abandoned since
scraping), half publish no MX at all. That is the actionable output: 34 rows that would have
consumed outreach effort and produced bounces.

Provider mix, by addresses:

| Provider | Addresses | Domains |
|---|---:|---:|
| Google — free `gmail.com` | 1,203 | 1 |
| Google Workspace — own domain, paid | 594 | 584 |
| Hostinger | 115 | 115 |
| Microsoft 365 | 111 | 102 |
| Self-hosted | 82 | 82 |
| Zoho | 67 | 67 |
| GoDaddy | 38 | 38 |
| Other / Rediff / Mimecast | 83 | 55 |

**906 domains (921 addresses, ~39% of leads) run managed business email.** That is not trivia —
it is a digital-maturity signal, measured rather than assumed, and it is exactly the kind of
input the Phase 5 ICP engine needs given this dataset contains no firmographics at all.

### A measured tuning decision

The first run used concurrency 32 and produced **122 `UNKNOWN`s**. Re-running at concurrency 8
produced **20**, in the same wall-clock time — the resolver was the bottleneck either way, and the
extra parallelism bought failures rather than speed. The default is now 8, and the reasoning is a
comment in the code rather than folklore.

---

## 3. Websites: liveness, safely

### SSRF is the default assumption, not an edge case

Every URL here came out of a scraped spreadsheet. Handing such a URL to an HTTP client inside your
own network is the textbook SSRF setup:

```
http://169.254.169.254/    cloud instance metadata — credentials
http://127.0.0.1:5432/     your own database
http://10.0.0.1/           whatever is on the LAN
```

`resolve_public_address` refuses anything resolving to a loopback, private, link-local, multicast
or reserved address — **after** DNS resolution, because a public hostname is free to point at
`127.0.0.1`. Ports outside `{80, 443, 8080, 8443}` are refused on sight, so a crafted URL cannot
turn the verifier into a port scanner.

The guard is not decorative. The first run of the website test suite failed ten tests because the
local server had bound an ephemeral port and the guard correctly refused it. The fixture now binds
8080; the guard was right.

`allow_private=True` exists solely for those tests, as an explicit constructor argument rather
than an environment flag, precisely so that enabling it is visible at the call site.

### Politeness

These are small businesses' hosting, frequently shared. A burst of parallel requests at one host
is indistinguishable from an attack, so `HostLimiter` caps concurrency **per host** and enforces a
minimum gap between requests to the same host, independently of the global budget. A descriptive
`User-Agent` names the bot and points at the repository.

### Behaviours worth having

| Case | Handling | Why |
|---|---|---|
| HEAD returns 403/405 | fall back to GET | Small-business hosting often rejects HEAD while serving GET fine; treating that as dead writes off live sites |
| 404 | `VERIFIED`, `is_live=false` | The host answered — that is a measurement, just not a good one |
| 500 | `UNKNOWN` | The host is up and broken today; that may not last |
| Read timeout | `UNKNOWN` | The handshake succeeded; slow is not gone |
| Connect refused | `UNREACHABLE` | Nothing is listening |
| Redirect loop | `UNREACHABLE` | Capped at 5 hops |
| Parked page | `is_parked=true`, not live | A registrar's for-sale page returns 200 and contains no evidence about anyone |
| Response body | capped at 2 MB | Liveness does not need the whole site |

### Where it runs

Both sandboxes this project was built in have **DNS but no arbitrary HTTP egress**, so the real
website run happens on your machine:

```bash
make verify-websites
```

The code is tested against a real local HTTP server rather than a mocked transport — actual
sockets, actual redirects, actual timeouts — because a mock would only prove the mock returns what
the mock was told to return.

---

## 4. Caching and staleness

Both tables are caches keyed on the thing checked, with an explicit `expires_at`. A verification
is a statement about a moment; without a TTL the system keeps presenting a year-old DNS answer as
current fact.

| Outcome | TTL | Reasoning |
|---|---|---|
| `VERIFIED` | 30 days | MX records are stable |
| `UNREACHABLE` | 7 days | Worth re-checking in case it was transient |
| `UNKNOWN` | 6 hours | Not a measurement — retry soon rather than freeze a non-answer |
| `SKIPPED` | 30 days | A policy refusal; it will not change on its own |

`leadmind verify status` reports fresh vs stale counts. `--force` re-checks inside the TTL.

---

## 5. Rubric v1.1

The Phase 1 design decision to stamp every score with its rubric version paid off here: v1.0 and
v1.1 scores coexist in `data_quality_scores` and are directly comparable.

Two new factors, `mailbox_verified` (10) and `website_live` (10), with the other weights reduced
proportionally to keep the total at 100. Two new penalties: `mailbox_domain_dead` (15) and
`website_dead` (8).

**Unmeasured is not zero.** A factor that cannot be evaluated returns `None` and drops out of both
the numerator and the denominator. Scoring it zero would punish leads for work the operator has
not done yet; scoring it 0.5 would invent a measurement. Each score records how many factors were
actually evaluated, so a partially-measured 80 is visibly different from a fully-measured one.

Measured effect of adding MX evidence (websites not yet verified, so `website_live` is still
unmeasured for everyone):

| Rubric | Leads | Mean | Median |
|---|---:|---:|---:|
| v1.0 | 2,351 | 69.37 | 72.80 |
| v1.1 | 2,351 | 71.97 | 75.68 |

Factors actually measured per lead under v1.1: **10 of 11** for 2,326 leads, **9 of 11** for the
25 whose mailbox domain came back `UNKNOWN`.

---

## 6. Schema

```
domain_verifications   domain (unique) · status · has_mx · provider · is_freemail
                       · is_disposable · address_count · latency_ms · details(JSONB)
                       · error · checked_at · expires_at

url_verifications      url (unique) · host · status · status_code · is_live · is_parked
                       · final_url · redirect_count · title · latency_ms · details(JSONB)
                       · error · checked_at · expires_at
```

`address_count` on the domain row makes the cache's leverage visible in the data rather than only
in a log line.

---

## 7. Tests

| Area | What is asserted |
|---|---|
| SSRF guard | loopback, RFC1918, link-local, metadata, multicast, IPv6 ULA all refused; refusal happens after DNS; non-web ports refused before any connection |
| Host limiter | per-host concurrency capped; minimum interval enforced; different hosts do not block each other |
| Retry | backoff retries listed exceptions, re-raises the last one, never retries unlisted ones |
| MX verification | MX present → verified; no MX → unreachable; NXDOMAIN → unreachable; resolver failure → **unknown**; provider classification; ordering normalised by the caller |
| Website liveness | real local HTTP server: live pages, redirects, HEAD→GET fallback, 404, 500, timeouts, redirect loops, parked pages |
| Runner | one query per domain not per address; TTL cache serves the second run; `--force` overrides |
| Rubric | unmeasured factors drop out; verified mailbox raises the score; dead domain penalises and names the reason; a resolver blip changes nothing |

**198 tests**, all passing.

---

## 8. Still not done, stated plainly

- **Website liveness has not been run against the real 1,995 domains** — no HTTP egress from
  either build sandbox. The code and its tests are complete; the run is one command on a machine
  with internet.
- **Mailbox-level verification is out of scope**, by the reasoning in §2. Domain-level is what is
  claimed and domain-level is what is measured.
- **25 domains remain `UNKNOWN`.** They expire in 6 hours and will be retried automatically.
- **Catch-all detection is not implemented.** It requires SMTP, and §2 applies.
