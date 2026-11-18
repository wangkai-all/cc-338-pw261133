"""Catalog and content collection against the publisher site.

The collector is deliberately conservative: a *positive* sighting (the item is
listed, or its full article is readable) is always accepted, while a *negative*
sighting only counts when the listing was complete, healthy and region-matched.
Anything ambiguous is recorded as an anomaly and never drives deletion.
"""

from __future__ import annotations

import gzip
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterable

from .util import compact_ws, sha256_text

USER_AGENT = "CatalogLedger/1.0 (+local-collector; evidence-preserving)"
MAX_PAGES = 50
MAX_BODY_FETCHES = 400
MAX_BYTES = 4_000_000


@dataclass
class Response:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed_ms: int
    error: str = ""

    @property
    def text(self) -> str:
        charset = "utf-8"
        ctype = self.headers.get("content-type", "")
        match = re.search(r"charset=([\w-]+)", ctype, re.I)
        if match:
            charset = match.group(1)
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


class Http:
    """Tiny polite HTTP client with a per-scan request budget."""

    def __init__(self, timeout: float = 12.0) -> None:
        self.timeout = timeout
        self.requests = 0
        self.bytes = 0
        self.elapsed_ms = 0

    def get(self, url: str, extra_headers: dict[str, str] | None = None) -> Response:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
        }
        if extra_headers:
            headers.update(extra_headers)
        started = time.monotonic()
        self.requests += 1
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read(MAX_BYTES + 1)[:MAX_BYTES]
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    try:
                        raw = gzip.decompress(raw)
                    except OSError:
                        pass
                elapsed = int((time.monotonic() - started) * 1000)
                self.bytes += len(raw)
                self.elapsed_ms += elapsed
                return Response(
                    url=url,
                    status=int(resp.status),
                    headers={k.lower(): v for k, v in resp.headers.items()},
                    body=raw,
                    elapsed_ms=elapsed,
                )
        except urllib.error.HTTPError as exc:
            raw = exc.read(MAX_BYTES) if exc.fp else b""
            elapsed = int((time.monotonic() - started) * 1000)
            self.bytes += len(raw)
            self.elapsed_ms += elapsed
            return Response(
                url=url,
                status=int(exc.code),
                headers={k.lower(): v for k, v in (exc.headers or {}).items()},
                body=raw,
                elapsed_ms=elapsed,
            )
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            self.elapsed_ms += elapsed
            reason = getattr(exc, "reason", exc)
            return Response(url=url, status=0, headers={}, body=b"", elapsed_ms=elapsed, error=str(reason))


# --------------------------------------------------------------------------- listing

@dataclass
class ListingEntry:
    item_id: str
    title: str
    url: str
    revision: int | None
    category: str = ""


@dataclass
class ListingResult:
    ok: bool
    status: int
    entries: list[ListingEntry] = field(default_factory=list)
    total: int | None = None
    pages_fetched: int = 0
    complete: bool = False
    anomaly: str = ""
    detail: dict = field(default_factory=dict)
    region_header: str = ""


class CatalogParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.main_seen = False
        self.main_attrs: dict[str, str] = {}
        self.start_marker = False
        self.end_marker = False
        self.error_code = ""
        self.entries: list[ListingEntry] = []
        self.entry_count = 0
        self._current: dict | None = None
        self._in_title = False
        self._title_buf: list[str] = []
        self._in_category = False
        self._category_buf: list[str] = []
        self._in_entry = False
        self.empty_marker = False

    @staticmethod
    def _attrs(attrs) -> dict[str, str]:
        return {key: (val or "") for key, val in attrs}

    def handle_starttag(self, tag, attrs):
        attrs = self._attrs(attrs)
        if tag == "main":
            self.main_seen = True
            self.main_attrs = attrs
            if attrs.get("class") == "service-message":
                self.error_code = attrs.get("data-error", "service_message")
        if tag == "article" and attrs.get("class") == "entry" and attrs.get("data-item-id"):
            self._current = {
                "item_id": attrs.get("data-item-id", ""),
                "revision": attrs.get("data-revision", ""),
                "title": "",
                "url": "",
                "category": "",
            }
            self._in_entry = True
        if tag == "a" and self._in_entry and attrs.get("href", "").startswith("/content/"):
            if self._current is not None:
                self._current["url"] = attrs.get("href", "")
        if tag == "h2" and self._in_entry:
            self._in_title = True
            self._title_buf = []
        if tag == "span" and self._in_entry and attrs.get("class") == "category":
            self._in_category = True
            self._category_buf = []
        if tag == "p" and attrs.get("class") == "empty":
            self.empty_marker = True

    def handle_endtag(self, tag):
        if tag == "h2" and self._in_title:
            self._in_title = False
            if self._current is not None:
                self._current["title"] = compact_ws("".join(self._title_buf))
        if tag == "span" and self._in_category:
            self._in_category = False
            if self._current is not None:
                self._current["category"] = compact_ws("".join(self._category_buf))
        if tag == "article" and self._in_entry:
            self._in_entry = False
            if self._current and self._current["item_id"]:
                rev = self._current.get("revision") or ""
                try:
                    revision = int(rev) if rev else None
                except ValueError:
                    revision = None
                self.entries.append(
                    ListingEntry(
                        item_id=self._current["item_id"],
                        title=self._current["title"],
                        url=self._current["url"] or f"/content/{self._current['item_id']}",
                        revision=revision,
                        category=self._current["category"],
                    )
                )
                self.entry_count += 1
            self._current = None

    def handle_data(self, data):
        if self._in_title:
            self._title_buf.append(data)
        if self._in_category:
            self._category_buf.append(data)
        text = data.strip()
        if text == "<!--CATALOG-LIST-->":
            self.start_marker = True
        if text == "<!--CATALOG-END-->":
            self.end_marker = True


def parse_listing_page(text: str) -> CatalogParser:
    parser = CatalogParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:  # malformed HTML must not crash a sweep
        pass
    parser.start_marker = parser.start_marker or "<!--CATALOG-LIST-->" in text
    parser.end_marker = parser.end_marker or "<!--CATALOG-END-->" in text
    return parser


def fetch_listing(
    http: Http,
    base_url: str,
    region: str,
    page_size: int = 20,
    stop_after: int | None = None,
) -> ListingResult:
    """Walk the catalog pages for one region.

    Completeness requires: every page healthy, both catalog markers present,
    a declared total, and the collected count matching that total. A page that
    fails mid-walk makes the whole sweep incomplete.
    """
    entries: list[ListingEntry] = []
    seen: set[str] = set()
    total: int | None = None
    pages = 0
    anomaly = ""
    last_status = 0
    region_header = ""
    previous_signature: tuple[str, ...] | None = None

    for page in range(1, MAX_PAGES + 1):
        url = f"{base_url.rstrip('/')}/catalog?region={region}&page={page}&size={page_size}"
        resp = http.get(url)
        last_status = resp.status
        region_header = resp.headers.get("x-site-region", "")
        pages += 1
        if resp.error:
            return ListingResult(
                ok=False,
                status=0,
                pages_fetched=pages,
                anomaly="transport_error",
                detail={"error": resp.error[:200], "page": page},
                region_header=region_header,
            )
        if resp.status in (403, 451):
            return ListingResult(
                ok=False,
                status=resp.status,
                pages_fetched=pages,
                anomaly="region_blocked",
                detail={"page": page},
                region_header=region_header,
            )
        if resp.status >= 500:
            return ListingResult(
                ok=False,
                status=resp.status,
                pages_fetched=pages,
                anomaly="server_error",
                detail={"page": page},
                region_header=region_header,
            )
        if resp.status != 200:
            return ListingResult(
                ok=False,
                status=resp.status,
                pages_fetched=pages,
                anomaly=f"http_{resp.status}",
                detail={"page": page},
                region_header=region_header,
            )

        parsed = parse_listing_page(resp.text)
        if parsed.error_code:
            return ListingResult(
                ok=False,
                status=resp.status,
                pages_fetched=pages,
                anomaly="error_page_200",
                detail={"page": page, "error": parsed.error_code},
                region_header=region_header,
            )
        if not parsed.start_marker or not parsed.end_marker:
            return ListingResult(
                ok=False,
                status=resp.status,
                pages_fetched=pages,
                anomaly="missing_markers",
                detail={"page": page},
                region_header=region_header,
            )

        if total is None:
            try:
                total = int(parsed.main_attrs.get("data-total", ""))
            except ValueError:
                total = None
        if total == 0 or (page == 1 and parsed.empty_marker and not parsed.entries):
            return ListingResult(
                ok=False,
                status=resp.status,
                pages_fetched=pages,
                anomaly="empty_catalog",
                detail={"page": page, "total": total or 0},
                region_header=region_header,
            )

        signature = tuple(entry.item_id for entry in parsed.entries)
        if signature and signature == previous_signature:
            return ListingResult(
                ok=False,
                status=resp.status,
                pages_fetched=pages,
                anomaly="pagination_loop",
                detail={"page": page},
                region_header=region_header,
            )
        previous_signature = signature

        for entry in parsed.entries:
            if entry.item_id not in seen:
                seen.add(entry.item_id)
                entries.append(entry)

        if stop_after is not None and len(entries) >= stop_after:
            break
        if not parsed.entries:
            break
        if total is not None and len(entries) >= total:
            break

    complete = False
    if not anomaly:
        if total is None:
            anomaly = "missing_total"
        elif len(entries) != total:
            anomaly = "count_mismatch"
        else:
            complete = True

    return ListingResult(
        ok=True,
        status=last_status,
        entries=entries,
        total=total,
        pages_fetched=pages,
        complete=complete,
        anomaly=anomaly,
        detail={
            "total": total,
            "collected": len(entries),
            "pages": pages,
        },
        region_header=region_header,
    )


# --------------------------------------------------------------------------- article

@dataclass
class BodyResult:
    status: str  # ok | withdrawn | not_found | blocked | error_page | incomplete | error | skipped
    http_status: int
    text: str = ""
    excerpt: str = ""
    content_hash: str = ""
    complete: bool = False
    anomaly: str = ""
    detail: dict = field(default_factory=dict)
    bytes: int = 0


class ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.error_code = ""
        self.withdrawn = False
        self.item_meta = ""
        self.revision_meta = ""
        self.body_start = False
        self.body_end = False
        self._in_body = False
        self._paragraphs: list[str] = []
        self._buf: list[str] = []
        self._in_article = False
        self._in_p = False
        self._in_h1 = False
        self._h1: list[str] = []
        self.title = ""

    def handle_starttag(self, tag, attrs):
        attrs = {k: (v or "") for k, v in attrs}
        if tag == "meta":
            name = attrs.get("name", "")
            if name == "catalog-item":
                self.item_meta = attrs.get("content", "")
            elif name == "catalog-revision":
                self.revision_meta = attrs.get("content", "")
            elif name == "catalog-status" and attrs.get("content") == "withdrawn":
                self.withdrawn = True
        if tag == "main":
            if attrs.get("class") == "service-message":
                self.error_code = attrs.get("data-error", "service_message")
            if attrs.get("data-status") == "withdrawn":
                self.withdrawn = True
        if tag == "article":
            self._in_article = True
        if tag == "h1":
            self._in_h1 = True
            self._h1 = []
        if tag == "p" and self._in_article:
            self._in_p = True
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "h1" and self._in_h1:
            self._in_h1 = False
            self.title = compact_ws("".join(self._h1))
        if tag == "p" and self._in_p:
            self._in_p = False
            text = compact_ws("".join(self._buf))
            if text:
                self._paragraphs.append(text)
        if tag == "article":
            self._in_article = False

    def handle_data(self, data):
        if self._in_h1:
            self._h1.append(data)
        if self._in_p:
            self._buf.append(data)


def parse_article(text: str) -> ArticleParser:
    parser = ArticleParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        pass
    parser.empty = not parser._paragraphs
    parser.paragraphs = list(parser._paragraphs)
    parser.body_start = parser.body_start or "<!--ARTICLE-BODY-START-->" in text
    parser.body_end = parser.body_end or "<!--ARTICLE-BODY-END-->" in text
    return parser


def fetch_body(http: Http, base_url: str, item_id: str, region: str, min_chars: int = 40) -> BodyResult:
    url = f"{base_url.rstrip('/')}/content/{item_id}?region={region}"
    resp = http.get(url)
    if resp.error:
        return BodyResult("error", 0, anomaly="transport_error", detail={"error": resp.error[:200]})
    if resp.status == 410:
        parsed = parse_article(resp.text)
        if parsed.withdrawn or parsed.error_code == "withdrawn":
            return BodyResult(
                "withdrawn",
                410,
                complete=True,
                detail={"marker": "tombstone"},
                bytes=len(resp.body),
            )
        return BodyResult("incomplete", 410, anomaly="unmarked_410", bytes=len(resp.body))
    if resp.status == 451:
        return BodyResult("blocked", 451, anomaly="region_page_blocked", bytes=len(resp.body))
    if resp.status == 404:
        parsed = parse_article(resp.text)
        if parsed.error_code == "not_found":
            return BodyResult("not_found", 404, detail={"kind": "service_not_found"}, bytes=len(resp.body))
        return BodyResult("not_found", 404, detail={"kind": "bare_404"}, bytes=len(resp.body))
    if resp.status >= 500:
        return BodyResult("error", resp.status, anomaly="server_error", bytes=len(resp.body))
    if resp.status != 200:
        return BodyResult("error", resp.status, anomaly=f"http_{resp.status}", bytes=len(resp.body))

    parsed = parse_article(resp.text)
    if parsed.error_code:
        return BodyResult(
            "error_page",
            200,
            anomaly="error_page_200",
            detail={"error": parsed.error_code},
            bytes=len(resp.body),
        )
    if parsed.withdrawn:
        return BodyResult("withdrawn", 200, complete=True, detail={"marker": "tombstone"}, bytes=len(resp.body))

    article_text = compact_ws(" ".join(parsed.paragraphs))
    problems = []
    if not parsed.body_start or not parsed.body_end:
        problems.append("missing_body_markers")
    if parsed.item_meta and parsed.item_meta != item_id:
        problems.append("wrong_item_id")
    if len(article_text) < min_chars:
        problems.append("body_too_short")
    if problems:
        return BodyResult(
            "incomplete",
            200,
            text=article_text,
            excerpt=article_text[:160],
            bytes=len(resp.body),
            anomaly=",".join(problems),
            detail={"chars": len(article_text), "markers": [parsed.body_start, parsed.body_end]},
        )

    return BodyResult(
        "ok",
        200,
        text=article_text,
        excerpt=article_text[:220],
        content_hash=sha256_text(article_text),
        complete=True,
        bytes=len(resp.body),
        detail={"chars": len(article_text), "revision": parsed.revision_meta},
    )


def parse_regions(raw: Iterable[str] | str) -> tuple[str, ...]:
    if isinstance(raw, str):
        parts = raw.split(",")
    else:
        parts = list(raw)
    out: list[str] = []
    for part in parts:
        value = str(part).strip().lower()
        if value and value not in out:
            out.append(value)
    return tuple(out)


def safe_base(url: str) -> str:
    return url.rstrip("/")
