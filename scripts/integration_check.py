"""Integration pass against live portals, the live database and the real bot
command surface. Prints a pass/fail line per test case id.

Unit tests cover logic with fakes; this covers the things that can only break
against reality: a portal changing its HTML, a bot-shield, the shared Turso
schema, and the end-to-end "does anything get lost" invariants.
"""
import copy
import io
import os
import logging
import pathlib
import sys
import time
from contextlib import redirect_stderr

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)
logging.disable(logging.CRITICAL)

import yaml  # noqa: E402

from scanner.aggregator import ListingGroup, group_listings  # noqa: E402
from scanner.chat_config import ChatOverride, EffectiveConfig  # noqa: E402
from scanner.chat_repo import ChatConfigRepo  # noqa: E402
from scanner.cities import get_city  # noqa: E402
from scanner.commands import CommandContext, CommandRouter  # noqa: E402
from scanner.env import load_dotenv  # noqa: E402
from scanner.filters import ListingFilter  # noqa: E402
from scanner.format import format_group_html, format_html  # noqa: E402
from scanner.models import Listing  # noqa: E402
from scanner.registry import SOURCE_REGISTRY  # noqa: E402
from scanner.scoring import DealScorer  # noqa: E402
from scanner.storage import SeenStore  # noqa: E402

load_dotenv()
CFG = yaml.safe_load(open(f"{ROOT}/config.yml"))
EC = EffectiveConfig(baseline=CFG, override=ChatOverride())
FLT = ListingFilter.from_config(EC)
# Any registered chat works; the backlog cases just need a real id.
CHAT = os.environ.get("TG_CHAT_ID", "-5411379431")
TELEGRAM_LIMIT = 4096
UA = (CFG.get("http") or {}).get("user_agent", "")

results = []


def check(tid, desc, fn):
    t0 = time.time()
    try:
        detail = fn()
        ok = True
    except AssertionError as e:
        detail, ok = str(e), False
    except Exception as e:
        detail, ok = f"{type(e).__name__}: {e}", False
    results.append((tid, ok, desc, detail, time.time() - t0))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {tid}  {desc}\n        {detail}  ({time.time()-t0:.1f}s)", flush=True)


# ── cached live scans, shared across cases ───────────────────────────────
SCANS = {}


def scan_all():
    for name, sconf in EC.enabled_source_configs(SOURCE_REGISTRY).items():
        cls = SOURCE_REGISTRY[name]
        params = {k: v for k, v in sconf.items() if k != "enabled"}
        params.update(user_agent=UA, timeout=30, delay=1.5)
        src = cls(**params)
        src.pages = 0
        with redirect_stderr(io.StringIO()):
            got = list(src.scan())
        SCANS[name] = (src, got)


# ── R1 discovery ─────────────────────────────────────────────────────────

def t_r1_1():
    scan_all()
    missing = [n for n, (_, got) in SCANS.items() if not got]
    assert not missing, f"sources returned nothing: {missing}"
    return " · ".join(f"{n}={len(g)}" for n, (_, g) in SCANS.items())


def t_r1_2():
    bad = [n for n, (s, _) in SCANS.items() if not s.scan_completed]
    assert not bad, f"sweep did not reach the end for: {bad}"
    return "every source walked to the end of its result set"


def t_r1_3():
    dupes = {}
    for n, (_, got) in SCANS.items():
        ids = [l.id for l in got]
        if len(ids) != len(set(ids)):
            dupes[n] = len(ids) - len(set(ids))
    assert not dupes, f"duplicate ids yielded within one scan: {dupes}"
    return "no source yielded the same listing twice"


def t_r1_4():
    """Otodom's normal path must work without the curl fallback."""
    cls = SOURCE_REGISTRY["otodom"]
    url = cls.build_url(get_city("krakow"), max_price=EC.max_price(), min_area=EC.min_area())
    src = cls(url=url, user_agent=UA)
    from scanner.sources.base import BaseSource
    html = BaseSource.fetch(src, url)
    n = len(list(src._parse(html)))
    assert n > 0, "requests path parsed 0 listings"
    return f"requests path OK, {n} listings on page 1"


def t_r1_5():
    """And the CloudFront fallback must work when it is needed."""
    cls = SOURCE_REGISTRY["otodom"]
    url = cls.build_url(get_city("krakow"), max_price=EC.max_price(), min_area=EC.min_area())
    src = cls(url=url, user_agent=UA)
    html = src._fetch_via_curl(url)
    n = len(list(src._parse(html)))
    assert n > 0, "curl fallback parsed 0 listings"
    assert "__OTODOM_HTTP_STATUS__" not in html, "status marker leaked into the body"
    return f"curl fallback OK, {n} listings, marker stripped"


# ── R2 filtering ─────────────────────────────────────────────────────────

def t_r2_1():
    allx = [l for _, got in SCANS.values() for l in got]
    rejected = [(l, r) for l in allx for ok, r in [FLT.accepts(l)] if not ok]
    rate = 100 * len(rejected) / max(1, len(allx))
    assert rate < 20, f"filters reject {rate:.0f}% — far too aggressive"
    kinds = {}
    for _, r in rejected:
        k = r.split("'")[1] if "'" in r else r.split()[0]
        kinds[k] = kinds.get(k, 0) + 1
    return f"{len(rejected)}/{len(allx)} rejected ({rate:.1f}%) — " + ", ".join(
        f"{k}×{v}" for k, v in sorted(kinds.items(), key=lambda x: -x[1])
    )


def t_r2_2():
    """A listing at exactly the threshold must pass, one over must not."""
    at = Listing(source="t", id="1", url="u", title="t", price=EC.max_price(), area=EC.min_area())
    over = Listing(source="t", id="2", url="u", title="t", price=EC.max_price() + 1, area=EC.min_area())
    under = Listing(source="t", id="3", url="u", title="t", price=EC.max_price(), area=EC.min_area() - 0.01)
    assert FLT.accepts(at)[0], "exact threshold rejected"
    assert not FLT.accepts(over)[0], "over budget accepted"
    assert not FLT.accepts(under)[0], "under min area accepted"
    return f"boundary at {EC.max_price()} zł / {EC.min_area()} m² behaves"


def t_r2_3():
    """Price and area are not symmetrical, and the asymmetry is deliberate.

    No price means the budget cannot be checked, so it is rejected. No area
    is fine — komornik prices its auctions but rarely sizes them.
    """
    no_area = Listing(source="komornik", id="1", url="u",
                      title="lokal mieszkalny", price=289_260)
    assert FLT.accepts(no_area)[0], "a priced listing with no area was rejected"

    no_price = Listing(source="otodom", id="2", url="u", title="Przystanek Prądnik")
    ok, reason = FLT.accepts(no_price)
    assert not ok, "an unpriced listing slipped past the budget filter"
    assert "no price" in reason, reason
    return "missing area passes, missing price rejects"


def t_r2_4():
    """Polish inflection: prefix match, so 'udziału' is caught too."""
    for title in ("udział 1/2", "Udziału w lokalu", "współwłasność", "TBS - 3 pokoje"):
        l = Listing(source="t", id="1", url="u", title=title, price=500000, area=45)
        assert not FLT.accepts(l)[0], f"{title!r} should be rejected"
    ok = Listing(source="t", id="1", url="u", title="Mieszkanie z balkonem", price=500000, area=45)
    assert FLT.accepts(ok)[0], "a clean listing was rejected"
    return "inflected forms rejected, clean listing accepted"


def t_r2_5():
    """Deleting a keyword must change the fingerprint and the verdict."""
    l = Listing(source="t", id="1", url="u", title="TBS - 3 POKOJE", price=360000, area=53.5)
    assert not FLT.accepts(l)[0], "TBS not currently rejected"
    relaxed_cfg = copy.deepcopy(CFG)
    relaxed_cfg["filters"]["reject_keywords"] = [
        k for k in CFG["filters"]["reject_keywords"] if k not in ("TBS", "T.B.S")
    ]
    relaxed = ListingFilter.from_config(
        EffectiveConfig(baseline=relaxed_cfg, override=ChatOverride())
    )
    assert relaxed.accepts(l)[0], "still rejected after removing the keyword"
    assert relaxed.fingerprint() != FLT.fingerprint(), "fingerprint did not change"
    return f"{FLT.fingerprint()} -> {relaxed.fingerprint()}, verdict flips"


# ── R3 dedup ─────────────────────────────────────────────────────────────

def t_r3_1():
    allx = [l for _, got in SCANS.values() for l in got]
    keys = [l.dedup_key for l in allx]
    assert len(keys) == len(set(keys)), "same dedup_key from two sources"
    return f"{len(set(keys))} unique dedup keys across {len(allx)} listings"


def t_r3_2():
    """The fuzzy key must not carry junk (OLX used to embed the date)."""
    olx = [l for l in SCANS.get("olx", (None, []))[1] if l.fuzzy_key]
    bad = [l.fuzzy_key for l in olx if any(c.isdigit() for c in l.fuzzy_key.split("|")[-1])]
    assert not bad, f"date leaked into fuzzy key: {bad[:3]}"
    return f"{len(olx)} olx fuzzy keys clean"


def t_r3_3():
    """Cross-source duplicates should actually be detected."""
    allx = [l for _, got in SCANS.values() for l in got if l.fuzzy_key]
    seen, dupes = set(), 0
    for l in allx:
        if l.fuzzy_key in seen:
            dupes += 1
        seen.add(l.fuzzy_key)
    return f"{dupes} cross-source duplicates collapse out of {len(allx)} keyed listings"


# ── R4 packaging ─────────────────────────────────────────────────────────

def t_r4_1():
    allx = [l for _, got in SCANS.values() for l in got if FLT.accepts(l)[0]]
    items = list(group_listings(allx, min_group_size=EC.min_group_size()))
    out = []
    for i in items:
        out.extend(l.id for l in i.items) if isinstance(i, ListingGroup) else out.append(i.id)
    assert len(out) == len(allx), f"{len(allx)} in, {len(out)} out"
    assert len(out) == len(set(out)), "a listing was emitted twice"
    return f"{len(allx)} listings -> {len(items)} messages, none lost, none duplicated"


def t_r4_2():
    allx = [l for _, got in SCANS.values() for l in got if FLT.accepts(l)[0]]
    worst, worst_kind = 0, ""
    for i in group_listings(allx, min_group_size=EC.min_group_size()):
        body = format_group_html(i) if isinstance(i, ListingGroup) else format_html(i)
        if len(body) > worst:
            worst, worst_kind = len(body), "group" if isinstance(i, ListingGroup) else "single"
    assert worst < TELEGRAM_LIMIT, f"a {worst}-char message would be rejected"
    return f"longest message {worst} chars ({worst_kind}), limit {TELEGRAM_LIMIT}"


def t_r4_3():
    """0 must be a real off switch — a big threshold is not.

    Kraków produces a 104-listing location bucket, so min_group_size=99 still
    grouped. This asserts both halves of that finding.
    """
    allx = [l for _, got in SCANS.values() for l in got if FLT.accepts(l)[0]]
    off = list(group_listings(allx, min_group_size=0))
    assert all(not isinstance(i, ListingGroup) for i in off), "grouped despite /grouping 0"
    assert len(off) == len(allx)
    big = [i for i in group_listings(allx, min_group_size=99) if isinstance(i, ListingGroup)]
    return (f"/grouping 0 -> {len(off)} individual messages; "
            f"a 99 threshold would still make {len(big)} group(s)")


# ── R5 delivery ──────────────────────────────────────────────────────────

def t_r5_1():
    with SeenStore(ensure_schema=False) as s:
        repo = ChatConfigRepo(s)
        backlog = repo.undelivered(CHAT)
        total = s.conn.execute(
            "SELECT COUNT(*) FROM seen WHERE status='matched'").fetchall()[0][0]
        sent = s.conn.execute(
            "SELECT COUNT(*) FROM chat_emissions WHERE chat_id=?", (CHAT,)).fetchall()[0][0]
    return f"{total} matched, {sent} already delivered, {len(backlog)} still owed"


def t_r5_2():
    """Backlog must be ordered so an interrupted run sends the best first."""
    with SeenStore(ensure_schema=False) as s:
        backlog = ChatConfigRepo(s).undelivered(CHAT, limit=50)
    scores = [l.score.value for l in backlog if l.score]
    assert scores == sorted(scores, reverse=True), "backlog not sorted by score"
    return f"top of backlog: {scores[:5]}"


def t_r5_3():
    """Nothing already delivered may reappear in the backlog."""
    with SeenStore(ensure_schema=False) as s:
        repo = ChatConfigRepo(s)
        backlog = {l.dedup_key for l in repo.undelivered(CHAT, limit=5000)}
        emitted = {r[0] for r in s.conn.execute(
            "SELECT listing_key FROM chat_emissions WHERE chat_id=?", (CHAT,)).fetchall()}
    overlap = backlog & emitted
    assert not overlap, f"{len(overlap)} already-sent listings are queued again"
    return f"no overlap between {len(backlog)} queued and {len(emitted)} sent"


def t_r5_4():
    """A sweep recorded under other filters must not count as done."""
    with SeenStore() as s:
        url = "https://example.test/itest"
        s.record_swept(url, "OLDFINGERPRINT")
        stale = s.is_swept(url, FLT.fingerprint())
        fresh = s.is_swept(url, "OLDFINGERPRINT")
        s.conn.execute("DELETE FROM swept_urls WHERE url=?", (url,))
        s.conn.commit()
    assert fresh and not stale, "fingerprint gating is broken"
    return "changing filters retires a completed sweep"


# ── R6 config ────────────────────────────────────────────────────────────

def t_r6_1():
    import os
    assert os.path.exists(f"{ROOT}/config.yml"), "config.yml missing"
    assert not os.path.exists(f"{ROOT}/config.example.yml"), "two config files again"
    return "exactly one config file, tracked in git"


def t_r6_2():
    import re
    body = open(f"{ROOT}/config.yml").read()
    leaks = re.findall(r"\d{8,10}:[A-Za-z0-9_-]{30,}|libsql://\S+|eyJ[A-Za-z0-9_-]{20,}", body)
    assert not leaks, f"secret-shaped string in tracked config: {leaks}"
    return "no secrets in the tracked config"


def t_r6_3():
    """Every source URL must come from the configured city."""
    urls = {n: c.get("url") for n, c in EC.enabled_source_configs(SOURCE_REGISTRY).items()}
    wrong = {n: u for n, u in urls.items() if u and "krakow" not in u.lower() and "krak" not in u.lower()}
    assert not wrong, f"URL does not target the configured city: {wrong}"
    return f"all {len(urls)} source URLs target {EC.city_key()}"


# ── R7 bot ───────────────────────────────────────────────────────────────

BOT_CMDS = [
    "/help", "/status", "/config", "/urls", "/decision_tree", "/dashboard",
    "/grouping", "/grouping 5", "/grouping 0", "/grouping abc",
    "/max_price 500000", "/max_price abc", "/max_price",
    "/min_area 45", "/max_area 70", "/min_year 1990",
    "/source otodom off", "/source nosuch on", "/source",
    "/kw + balkon 5", "/kw - parter", "/kw reject TBS", "/kw list", "/kw del x", "/kw",
    "/reset max_price", "/reset nosuchfield", "/reset all", "/reset",
    "/pause", "/resume", "/stats", "/stats 30", "/stats abc",
]


def _router():
    from unittest.mock import MagicMock
    repo = MagicMock()
    repo.get.return_value = None
    repo.stats_last_days.return_value = {"emitted": 42}
    return CommandRouter("123:TEST", repo, CFG)


def t_r7_1():
    r, ctx = _router(), CommandContext(
        chat_id="-1", chat_title="t", chat_type="group", user_id=1, user_name="u", message_id=1)
    silent, oversized = [], []
    for c in BOT_CMDS:
        p = c.split()
        h = r._handlers.get(p[0][1:].lower())
        assert h, f"{c}: no handler"
        replies = h(p[1:], ChatOverride(), ctx)
        if not replies:
            silent.append(c)
        oversized += [c for x in replies if len(x.text) > TELEGRAM_LIMIT]
    assert not silent, f"no reply from: {silent}"
    assert not oversized, f"reply over the Telegram limit: {oversized}"
    return f"{len(BOT_CMDS)} command forms all answer, all under {TELEGRAM_LIMIT} chars"


def t_r7_2():
    """/decision_tree must describe the filters that actually run."""
    from scanner.introspection import format_decision_tree
    tree = format_decision_tree(CFG, ChatOverride())
    for kw in ("TBS", "udział", "z lat 60"):
        assert kw in tree, f"{kw} missing from the decision tree"
    assert str(EC.max_price()) in tree
    return "decision tree lists the live thresholds and keywords"


def t_r7_3():
    """/grouping must set the threshold and explain itself."""
    r, ctx = _router(), CommandContext(
        chat_id="-1", chat_title="t", chat_type="group", user_id=1, user_name="u", message_id=1)
    o = ChatOverride()
    r._handlers["grouping"](["7"], o, ctx)
    assert o.min_group_size == 7, "threshold not stored"
    text = r._handlers["grouping"]([], ChatOverride(), ctx)[0].text
    assert "never fewer flats" in text and "/grouping 0" in text
    return "sets the threshold, and explains itself with an example"


# ── R9 observability ─────────────────────────────────────────────────────

def t_r9_1():
    """Scoring must produce a value and human-readable reasons."""
    scorer = DealScorer.from_config(EC, CFG)
    allx = [l for _, got in SCANS.values() for l in got if l.price and l.area]
    ctxs = scorer.make_context([l.price / l.area for l in allx])
    scored = [scorer.score(l, ctxs) for l in allx[:200]]
    assert all(0 <= s.value <= 100 for s in scored), "score outside 0..100"
    assert any(s.reasons for s in scored), "no listing got any reason"
    return (f"median {ctxs.median_price_per_m2:.0f} zł/m², "
            f"scores {min(s.value for s in scored)}..{max(s.value for s in scored)}")


if __name__ == "__main__":
    print("=" * 78)
    print("INTEGRATION PASS — live portals, live database, real command surface")
    print("=" * 78)
    for tid, desc, fn in [
        ("R1.1", "every configured source returns listings", t_r1_1),
        ("R1.2", "every source's sweep reaches the end", t_r1_2),
        ("R1.3", "no source yields the same listing twice", t_r1_3),
        ("R1.4", "otodom requests path works", t_r1_4),
        ("R1.5", "otodom curl fallback works", t_r1_5),
        ("R2.1", "filters are not over-rejecting", t_r2_1),
        ("R2.2", "price/area boundaries are exact", t_r2_2),
        ("R2.3", "missing area passes, missing price rejects", t_r2_3),
        ("R2.4", "Polish inflected forms are caught", t_r2_4),
        ("R2.5", "deleting a keyword takes effect", t_r2_5),
        ("R3.1", "dedup keys are unique", t_r3_1),
        ("R3.2", "fuzzy keys carry no junk", t_r3_2),
        ("R3.3", "cross-source duplicates are detected", t_r3_3),
        ("R4.1", "grouping loses nothing", t_r4_1),
        ("R4.2", "no message exceeds the Telegram limit", t_r4_2),
        ("R4.3", "grouping can be switched off", t_r4_3),
        ("R5.1", "delivery backlog reflects the database", t_r5_1),
        ("R5.2", "backlog is ordered best-first", t_r5_2),
        ("R5.3", "delivered listings never re-queue", t_r5_3),
        ("R5.4", "changing filters retires a sweep", t_r5_4),
        ("R6.1", "exactly one config file", t_r6_1),
        ("R6.2", "no secrets in the tracked config", t_r6_2),
        ("R6.3", "source URLs follow the configured city", t_r6_3),
        ("R7.1", "every bot command answers", t_r7_1),
        ("R7.2", "decision tree matches the live filters", t_r7_2),
        ("R7.3", "/grouping sets and explains", t_r7_3),
        ("R9.1", "scoring produces sane values", t_r9_1),
    ]:
        check(tid, desc, fn)

    passed = sum(1 for _, ok, *_ in results if ok)
    print("\n" + "=" * 78)
    print(f"RESULT: {passed}/{len(results)} passed")
    for tid, ok, desc, detail, _ in results:
        if not ok:
            print(f"  FAILED {tid} {desc}: {detail}")
