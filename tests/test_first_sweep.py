"""First-sweep behaviour: the one run that must not be page-capped.

Portals sort newest-first, so the steady-state `pages: 2` cap is right — new
listings always land on page 1. It is wrong exactly once, on the first run
against a URL, when the entire existing market sits on pages 3..20.
"""

from __future__ import annotations

import unittest

from scanner.models import Listing
from scanner.sources.base import BaseSource


class _FakeSource(BaseSource):
    """Yields `per_page` listings for `total_pages`, then empty pages."""

    name = "fake"

    def __init__(self, total_pages: int, per_page: int = 3, **kw):
        super().__init__(url="https://example.test/search", delay=0, **kw)
        self.total_pages = total_pages
        self.per_page = per_page
        self.fetched: list[int] = []

    def fetch(self, url: str) -> str:
        page = 1
        if "page=" in url:
            page = int(url.rsplit("page=", 1)[1])
        self.fetched.append(page)
        return str(page)

    def _parse(self, html: str):
        page = int(html)
        if page > self.total_pages:
            return
        for i in range(self.per_page):
            yield Listing(
                source=self.name,
                id=f"p{page}-{i}",
                url=f"https://example.test/{page}/{i}",
                title=f"flat {page}-{i}",
                price=500_000,
                area=45.0,
            )


class PageCapTests(unittest.TestCase):
    def test_configured_cap_stops_early(self) -> None:
        src = _FakeSource(total_pages=20)
        src.pages = 2
        self.assertEqual(6, len(list(src.scan())))
        self.assertEqual([1, 2], src.fetched)

    def test_pages_zero_walks_until_a_page_comes_back_empty(self) -> None:
        src = _FakeSource(total_pages=7)
        src.pages = 0
        listings = list(src.scan())
        self.assertEqual(21, len(listings))
        # 7 full pages plus the empty 8th that ends the walk.
        self.assertEqual(list(range(1, 9)), src.fetched)

    def test_unlimited_sweep_is_bounded_by_max_pages(self) -> None:
        """A portal that never returns an empty page must not loop forever."""
        src = _FakeSource(total_pages=10_000)
        src.pages = 0
        src.MAX_PAGES = 5
        list(src.scan())
        self.assertEqual([1, 2, 3, 4, 5], src.fetched)


class PaginationEndTests(unittest.TestCase):
    """The two ways a walk ends that are NOT failures."""

    def test_404_past_the_last_page_counts_as_a_complete_walk(self) -> None:
        """Morizon answers the page after the last one with a 404.

        Treating that as a fetch failure would leave the sweep unmarked, so
        the full walk would repeat on every single run, forever.
        """
        import requests

        src = _FakeSource(total_pages=3)
        src.pages = 0
        original = src.fetch

        def fetch(url):
            page = int(url.rsplit("page=", 1)[1]) if "page=" in url else 1
            if page > 3:
                response = requests.Response()
                response.status_code = 404
                raise requests.HTTPError(response=response)
            return original(url)

        src.fetch = fetch
        self.assertEqual(9, len(list(src.scan())))
        self.assertTrue(src.scan_completed)

    def test_a_404_on_the_very_first_page_is_still_a_failure(self) -> None:
        """Page 1 missing means the URL is broken, not that we ran out."""
        import requests

        src = _FakeSource(total_pages=3)

        def fetch(url):
            response = requests.Response()
            response.status_code = 404
            raise requests.HTTPError(response=response)

        src.fetch = fetch
        self.assertEqual([], list(src.scan()))
        self.assertFalse(src.scan_completed)

    def test_a_source_that_ignores_page_stops_after_one_repeat(self) -> None:
        """Komornik serves page 1 no matter what ?page= says.

        Left unchecked, an unlimited sweep refetches the same rows MAX_PAGES
        times and reports them as a market's worth of listings.
        """
        src = _FakeSource(total_pages=1)
        src.pages = 0
        original = src.fetch

        def fetch(url):
            original(url)  # keep `fetched` tracking intact
            return "1"     # every page is page 1

        src.fetch = fetch

        listings = list(src.scan())
        self.assertEqual(3, len(listings), "duplicates must not be re-yielded")
        self.assertEqual(3, len(src.fetched), "stops after 2 barren pages in a row")
        self.assertTrue(src.scan_completed)

    def test_one_barren_page_does_not_end_a_walk_that_recovers(self) -> None:
        """Otodom's tail is erratic: 37, 22, then 1 repeat, then 16 more.

        Stopping at that single all-repeats page cost half the
        back-catalogue, so a lone barren page must not end the walk.
        """
        pages = {
            1: ["a1", "a2"],
            2: ["b1", "b2"],
            3: ["b1"],            # nothing new — but not the end
            4: ["c1", "c2"],      # recovers
            5: [],                # the real end
        }
        src = _FakeSource(total_pages=0)

        def parse(html):
            for i in pages.get(int(html), []):
                yield Listing(source="fake", id=i, url=f"u/{i}", title=i,
                              price=500_000, area=45.0)

        src._parse = parse
        src.pages = 0

        got = [l.id for l in src.scan()]
        self.assertEqual(["a1", "a2", "b1", "b2", "c1", "c2"], got)
        self.assertTrue(src.scan_completed)


class RetryPolicyTests(unittest.TestCase):
    """A transient 5xx must not cost us a whole source for the run."""

    def _retry(self):
        src = BaseSource(url="https://example.test/", user_agent="t")
        return src.session.get_adapter("https://example.test/").max_retries

    def test_transient_server_errors_are_retried(self) -> None:
        forced = self._retry().status_forcelist
        for status in (429, 500, 502, 503, 504):
            self.assertIn(status, forced)

    def test_403_is_never_retried(self) -> None:
        """That's Otodom's bot-shield; hammering it only looks more like a bot."""
        self.assertNotIn(403, self._retry().status_forcelist)

    def test_final_status_still_reaches_raise_for_status(self) -> None:
        """raise_on_status=False, so fetch() reports the real code, not MaxRetryError."""
        self.assertFalse(self._retry().raise_on_status)


class _Store:
    """Minimal SeenStore stand-in for the sweep bookkeeping."""

    def __init__(self, swept=()):
        # A sweep is recorded per (url, filter fingerprint) — changing the
        # filters retires it so the back-catalogue is re-judged.
        self.swept = set(swept)

    def is_swept(self, url, filters=""):
        return url in self.swept

    def record_swept(self, url, filters=""):
        self.swept.add(url)

    # everything the pipeline touches during a scan
    def has(self, key):
        return False

    def add(self, *a, **kw):
        pass

    def stored_price(self, key):
        return None

    def promote_rejected(self, key, fuzzy_key=None):
        return False


class _Repo:
    def emitted_price(self, chat_id, key):
        return None

    def has_emitted(self, chat_id, key):
        return False

    def undelivered(self, chat_id, city=None, limit=2000):
        return []


class FirstSweepTests(unittest.TestCase):
    def _ctx(self, src):
        from scanner.filters import ListingFilter
        from scanner.pipeline import ChatContext

        return ChatContext(
            chat_id="-1", title="t", city="krakow",
            filter=ListingFilter(min_area=0, max_price=10**9),
            scorer=None, sources=[src], min_group_size=99, notifier=None,
        )

    def _pipeline(self, ctx, store):
        from scanner.pipeline import MultiChatPipeline

        return MultiChatPipeline([ctx], store=store, repo=_Repo(), dry_run=True)

    def test_unswept_url_is_scanned_in_full_then_marked(self) -> None:
        src = _FakeSource(total_pages=6)
        src.pages = 2                       # would normally stop at page 2
        store = _Store()
        pipe = self._pipeline(self._ctx(src), store)
        swept: list[str] = []
        pipe._scan_and_filter(pipe.contexts[0], swept)

        self.assertEqual(list(range(1, 8)), src.fetched, "should ignore the cap")
        self.assertEqual([src.url], swept)

    def test_already_swept_url_honours_the_configured_cap(self) -> None:
        src = _FakeSource(total_pages=6)
        src.pages = 2
        store = _Store(swept=[src.url])
        pipe = self._pipeline(self._ctx(src), store)
        swept: list[str] = []
        pipe._scan_and_filter(pipe.contexts[0], swept)

        self.assertEqual([1, 2], src.fetched)
        self.assertEqual([], swept)

    def test_a_crash_mid_sweep_does_not_mark_the_url(self) -> None:
        """Otherwise the deep pages we never reached are stranded forever."""
        src = _FakeSource(total_pages=6)
        src.pages = 2

        def boom(url):
            raise RuntimeError("network died on page 3")

        original = src.fetch

        def fetch(url):
            if "page=3" in url:
                return boom(url)
            return original(url)

        src.fetch = fetch
        pipe = self._pipeline(self._ctx(src), _Store())
        swept: list[str] = []
        pipe._scan_and_filter(pipe.contexts[0], swept)

        self.assertEqual([], swept, "an interrupted sweep must be retried")

    def test_dry_run_never_records_a_sweep(self) -> None:
        src = _FakeSource(total_pages=2)
        store = _Store()
        pipe = self._pipeline(self._ctx(src), store)
        pipe.run()
        self.assertEqual(set(), store.swept)


if __name__ == "__main__":
    unittest.main()
