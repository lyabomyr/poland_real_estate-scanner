# Test plan

## What this system is judged on

One thing above all others: **a flat you would have wanted must not fail to
reach you.** Everything else — tidy messages, a pretty dashboard, fast runs —
is secondary, because a missed listing is invisible. Nobody notices the flat
they never saw.

That framing decides the whole plan. The interesting bugs here are not crashes
(a crash is loud and gets fixed). They are the ones where the system reports
success while quietly dropping data. Every case below asks: *if this broke,
would anything look wrong?* If the answer is no, it gets a test.

Four real examples, all found and fixed:

| what happened | what it looked like |
|---|---|
| a 77-listing group rendered to 10 853 chars; Telegram rejects >4096 | 220 of 1004 listings unreachable, logs said nothing |
| delivery driven by "what this scan saw"; the run was killed mid-send | 647 listings matched but never sent, re-stranded every run |
| a keyword deleted from the config, but the stored row still said `rejected` | listing passed the filter, then vanished |
| `/grouping` used a wrong attribute; the router swallows handler errors | the bot simply never replied |

## Scope

**In scope.** Discovery (4 live portals), filtering, three dedup layers,
message packaging, delivery and its resumability, config propagation, the
Telegram command surface, the Streamlit dashboard, and observability.

**Out of scope.** Telegram's own delivery guarantees, portal uptime, Turso
availability, and the accuracy of the scoring *model* (its arithmetic is
tested; whether 74/100 is "correct" for a flat is a judgement call, not a
test).

## Test levels

| level | what it proves | cost | where |
|---|---|---|---|
| unit | logic is right given inputs, including inputs reality rarely produces | ~0.4 s | `make test` |
| integration | it still works against the real portals, the real database and the real command surface | ~3 min | `scripts/` runner, below |
| manual | it looks right to a human | minutes | dashboard + a real chat |

Unit tests use fakes so they can cover the awkward cases on demand — a portal
that ignores `?page=`, a 404 past the last page, a 500-listing group. The
integration pass covers what fakes cannot: HTML that changed overnight, a
bot-shield, the shared schema.

---

## Test cases

Ids are grouped by risk area. **U** = covered by unit tests, **I** = by the
integration pass, **M** = manual.

### R1 — Discovery: are we even seeing the market?

| id | case | expected | level |
|---|---|---|---|
| R1.1 | every configured source is scanned | all 4 return listings; none silently empty | I |
| R1.2 | a full sweep reaches the end of the result set | `scan_completed` is true for every source | I |
| R1.3 | one scan never yields the same listing twice | unique ids within a scan | I, U |
| R1.4 | Otodom over `requests` | HTTP 200, page parses | I |
| R1.5 | Otodom over the curl fallback | 200 with TLS pinned to 1.2, status marker stripped | I |
| R1.6 | a portal that ignores `?page=` | stop after 2 barren pages, don't refetch 100× | U |
| R1.7 | one barren page mid-walk | keep going — Otodom's tail is erratic | U |
| R1.8 | 404 on the page after the last | treat as a complete walk, not a failure | U |
| R1.9 | 404 on page 1 | that's a broken URL — a failure | U |
| R1.10 | transient 5xx | retry 3× with backoff; never retry a 403 | U |
| R1.11 | an interrupted sweep | URL not marked swept; retried next run | U |
| R1.12 | unlimited sweep on a portal that never ends | stop at `MAX_PAGES` | U |

### R2 — Filtering: are we throwing away good flats?

| id | case | expected | level |
|---|---|---|---|
| R2.1 | reject rate against the live market | single digits, with a per-reason breakdown | I |
| R2.2 | price/area boundaries | exactly at threshold passes, one over does not | I |
| R2.3 | unknown price or area | never rejects — komornik rarely publishes m² | I, U |
| R2.4 | Polish inflection | `udział` catches `udziału`; a clean listing survives | I, U |
| R2.5 | deleting a keyword from the config | verdict flips **and** the filter fingerprint changes | I |
| R2.6 | a stored `rejected` row that now passes | promoted to `matched`, reason cleared | U |
| R2.7 | promoting a row that is already matched | no-op — never demote | U |
| R2.8 | `max_area` | enforced by the filter itself, and described by `describe()` | U |
| R2.9 | a placeholder price ("1 zł") | rejected — it is a missing price, not a bargain | U |
| R2.10 | no price at all | still passes; unknown is not the same as absurd | U |

### R3 — Dedup: same flat twice, or two flats collapsed into one?

| id | case | expected | level |
|---|---|---|---|
| R3.1 | same-source keys | unique across the whole run | I |
| R3.2 | fuzzy keys carry no junk | no timestamp inside an OLX key | I, U |
| R3.3 | cross-source duplicates | the same flat on two portals collapses | I |
| R3.5 | dedup is applied to what is **actually sent** | the backlog, not the discarded scan results | U |
| R3.6 | dedup is not over-eager | distinct flats all survive; no fuzzy key means no collapsing | U |
| R3.4 | a listing with no price/area/location | passes through without fuzzy dedup rather than colliding | U |

### R4 — Packaging: does a message ever swallow a listing?

| id | case | expected | level |
|---|---|---|---|
| R4.1 | grouping over the live market | every listing emitted exactly once | I, U |
| R4.2 | longest rendered message | under 4096 chars, groups and singles alike | I, U |
| R4.3 | `min_group_size: 0` | grouping off; one message per flat | I, U |
| R4.4 | a group larger than 20 | split into `(1/N)` parts, nothing dropped | U |
| R4.5 | two portals, same address | two groups — never mixed | U |
| R4.6 | below the threshold | individual messages | U |

### R5 — Delivery: does everything matched eventually arrive?

| id | case | expected | level |
|---|---|---|---|
| R5.1 | the backlog reflects the database | matched minus already-delivered, per chat | I, U |
| R5.2 | backlog ordering | best score first, so an interrupted run sends the good ones | I, U |
| R5.3 | an already-delivered listing | never re-queued | I, U |
| R5.4 | filters changed since the sweep | sweep retired; next run re-walks every page | I, U |
| R5.5 | rejected rows | never in the backlog | U |
| R5.6 | a second chat | owed the same listings independently | U |
| R5.7 | a short 429 | wait it out, message lands | U |
| R5.8 | a long 429 | give up; it stays in the backlog for the next run | U |
| R5.9 | persistent 429 | attempt cap — one message cannot monopolise a run | U |
| R5.10 | no bot token | logged as a failure, never as silent success | U |
| R5.11 | a price change on a delivered listing | re-notified with the delta | U |

### R6 — Config: does editing the YAML do what it says?

| id | case | expected | level |
|---|---|---|---|
| R6.1 | one config file | `config.yml`, tracked; no second copy to drift | I |
| R6.2 | no secrets in it | it is in git, so this is not optional | I |
| R6.3 | `city` drives every URL | all source URLs target the configured city | I |
| R6.4 | env overrides | `TG_BOT_TOKEN` / `TURSO_*` / `DASHBOARD_URL` win over the file | U |
| R6.5 | per-chat override | baseline + delta, delta wins, null falls through | U |

### R7 — Bot: does every command answer?

| id | case | expected | level |
|---|---|---|---|
| R7.1 | all command forms, including error paths | every one answers, all under the limit | I, U |
| R7.2 | `/decision_tree` | describes the filters that actually run | I, U |
| R7.3 | `/grouping` | explains itself, and sets the threshold | I, U |
| R7.4 | every command appears in `/help` | undiscoverable ≈ non-existent | U |
| R7.5 | a duplicate `update_id` | executed exactly once | U |
| R7.6 | a long reply | chunked, never truncated | U |

### R8 — Dashboard

| id | case | expected | level |
|---|---|---|---|
| R8.1 | boots from `requirements.txt` alone | all three pages return 200 | I |
| R8.2 | every module imports in an isolated venv | no missing transitive dependency | I |
| R8.3 | reads the same baseline the scanner runs | `config.yml`, not a local copy | I |
| R8.4 | no credentials configured | says "not connected", never renders an empty market | M |
| R8.5 | editing a chat config | round-trips to `chat_configs` | M |

### R9 — Observability: could a silent failure stay silent?

| id | case | expected | level |
|---|---|---|---|
| R9.1 | scoring | 0..100 with human-readable reasons | I |
| R9.2 | a source returning zero | `WARNING` — that is what a broken parser looks like | code |
| R9.3 | rejects | one line per source with a per-reason breakdown | code |
| R9.4 | undelivered messages | `ERROR` plus a `send_failed` counter | code |
| R9.5 | which URL was scanned | logged, so a run can be reproduced in a browser | code |

---

## Running it

```bash
make test                     # unit, ~0.4s
make lint                     # unused imports, undefined names
make check-dashboard-deps     # imports in a venv built from requirements.txt only
make boot-check               # actually boots the dashboard, hits every page
make dry                      # full scan against live portals, sends nothing
make integration              # the 27 cases marked I below, ~3 min
```

`make integration` hits live portals and the shared database, so it is
deliberately not part of `make test` and not run in CI — it needs credentials
and it is slow. Run it after touching a parser, a filter, or delivery.

## Last run

77 unit tests green, pyflakes clean, dashboard serves all three pages 200,
and 27/27 integration cases passed against the live market:

```
R1.1  otodom=361 · olx=28 · morizon=648 · komornik=20
R1.2  every source walked to the end of its result set
R1.4  requests path OK, 37 listings on page 1
R1.5  curl fallback OK, 37 listings, marker stripped
R2.1  56/1057 rejected (5.3%) — price×24, TBS×16, udział×9,
      wielkiej płyty×3, area×2, z lat 60×1, suterena×1
R2.5  fingerprint e8d2a7370511 -> 0dd9fc6777d9, verdict flips
R3.1  1057 unique dedup keys across 1057 listings
R3.3  125 cross-source duplicates collapse out of 800 keyed listings
R4.1  1001 listings -> 620 messages, none lost, none duplicated
R4.2  longest message 3209 chars, limit 4096
R4.3  /grouping 0 -> 1001 individual messages
R5.1  1004 matched, 357 delivered, 647 still owed
R5.2  top of backlog: [83, 82, 81, 80, 80]
R5.3  no overlap between 647 queued and 357 sent
R7.1  34 command forms all answer, all under 4096 chars
R9.1  median 13504 zł/m², scores 31..83
```

Two defects were found by this pass rather than by review:

- **R4.3** — `/grouping 99` was documented as "off" and is not. Kraków
  produces a 104-listing location bucket, so 99 still grouped. Fixed by
  making `0` a real off switch; the case now asserts both halves.
- **R7.3** — caught a stale assertion in the test itself, not in the code.
  Worth recording: a test that drifts from the behaviour it guards is a
  false sense of safety, which is the failure mode this whole plan is about.

## Entry and exit criteria

**Entry.** Lint clean; unit tests green; credentials present for the
integration pass.

**Exit.** Every case above passes, or a failure is written down with its
consequence. A skipped case is a hole, not a pass.

## Known limitations

These are accepted, not defects — but they are exactly what a future
regression will hide behind:

- **The grouping key is only as good as the portal's address.** Morizon
  sometimes omits the street, so a "group" can be district-wide. Nothing is
  lost; the label is just coarser than it looks.
- **KRZ is not scanned.** Incapsula blocks headless browsers; documented
  rather than pretended.
- **`komornik.pl` has no working pagination.** We stop after detecting the
  repeat. Its whole Kraków inventory is ~20 listings, so nothing is missed.
- **Rate limiting is Telegram's, not ours.** There is no fixed pacing delay.
  A large backlog drains over several runs by design.
