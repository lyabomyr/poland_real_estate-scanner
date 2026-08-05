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


class _Store:
    """Minimal SeenStore stand-in for the sweep bookkeeping."""

    def __init__(self, swept=()):
        self.swept = set(swept)

    def is_swept(self, url):
        return url in self.swept

    def record_swept(self, url):
        self.swept.add(url)

    # everything the pipeline touches during a scan
    def has(self, key):
        return False

    def add(self, *a, **kw):
        pass

    def stored_price(self, key):
        return None


class _Repo:
    def emitted_price(self, chat_id, key):
        return None

    def has_emitted(self, chat_id, key):
        return False


class FirstSweepTests(unittest.TestCase):
    def _ctx(self, src):
        from scanner.filters import ListingFilter
        from scanner.pipeline import ChatContext

        return ChatContext(
            chat_id="-1", title="t", filter=ListingFilter(min_area=0, max_price=10**9),
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
