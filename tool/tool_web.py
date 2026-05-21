"""
Web tools: web_search and web_extract.

- ``web_search``  — DuckDuckGo HTML endpoint (no API key required) with an
  optional Tavily provider when ``TAVILY_API_KEY`` is set.
- ``web_extract`` — fetch a URL and return readable plain text (best-effort
  HTML stripping; no JS rendering).

The HTTP layer uses only the Python standard library; no extra dependencies.
"""

from __future__ import annotations

import gzip
import io
import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import List


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# 30s (was 15s): 15s was too tight for users behind cross-border links to
# duckduckgo.com / tavily.com — the call would routinely time out and the
# user saw "web_search always fails". 30s still bounds the tool round trip
# below the orchestrator's status-spinner attention budget.
_DEFAULT_TIMEOUT = 30
_MAX_BYTES = 2_000_000


class _SSRFBlocked(ValueError):
    """Raised when web_extract is asked to fetch a URL that resolves to a
    private/loopback/link-local/multicast/reserved address.

    Prompt-injection vector: a malicious file the agent reads tells the LLM to
    "verify by fetching http://127.0.0.1:11434/api/tags" (or 169.254.169.254
    for cloud metadata, or an internal 10.x service). Without this check, the
    agent obliges. The block fails closed; set LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS=1
    to opt out for local development.
    """


def hostname_is_safe(hostname: str) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for a parsed URL hostname.

    Resolves DNS once and inspects every A/AAAA record — a single hostname
    can have both a public and a private record (DNS rebinding mitigation
    happens by resolving here, then a second time inside urlopen, which would
    re-validate if we wrapped urlopen too; we don't, so this is best-effort
    against active adversaries but solid against accidental misuse).

    Public so peer modules (``tool_vision`` etc.) can apply the same check
    before doing their own ``urllib.request`` calls.
    """
    if not hostname:
        return False, "empty hostname"
    if os.environ.get("LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS") == "1":
        return True, ""

    # Bracketed IPv6 literals arrive with brackets stripped by urlparse.
    try:
        ip_literal = ipaddress.ip_address(hostname)
    except ValueError:
        ip_literal = None

    candidates: list[ipaddress._BaseAddress] = []
    if ip_literal is not None:
        candidates.append(ip_literal)
    else:
        try:
            infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror as exc:
            return False, f"DNS lookup failed: {exc}"
        for info in infos:
            addr = info[4][0]
            try:
                candidates.append(ipaddress.ip_address(addr.split("%", 1)[0]))
            except ValueError:
                continue
        if not candidates:
            return False, "no resolvable address"

    for ip in candidates:
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False, f"address {ip} is private/loopback/link-local/reserved"
    return True, ""


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate the destination host on every HTTP redirect.

    Without this, ``urllib.request.urlopen`` follows 30x redirects
    transparently — a public host can redirect to ``http://127.0.0.1/`` or
    ``http://169.254.169.254/`` and the seed-host check in ``web_extract`` is
    bypassed. This handler runs ``hostname_is_safe`` on each ``Location:``
    before allowing the redirect; failure raises ``HTTPError`` so the caller
    surfaces a refused-fetch instead of returning private-network content.

    ``LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS=1`` still bypasses the check (the
    env var hook lives inside ``hostname_is_safe``).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_host = urllib.parse.urlparse(newurl).hostname or ""
        allowed, reason = hostname_is_safe(new_host)
        if not allowed:
            raise urllib.error.HTTPError(
                newurl, code,
                f"Refused redirect to {new_host}: {reason}",
                headers, fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Cached opener with the safe-redirect handler installed. ``build_opener``
# is cheap but not free, and ``_http_get`` is called from web_search /
# web_extract / web_crawl — building once at import keeps the hot path lean.
#
# Public (no underscore): peer tool modules (``tool_homeassistant``,
# ``tool_osv``, ``tool_x_search``, ``tool_vision``) reuse it so every
# HTTP fetcher in the project benefits from the same redirect validation.
# The opener itself doesn't validate the *initial* host — callers do that
# (or, in HA's case, accept the user-configured HASS_URL). The opener only
# guards the 30x redirect path, which is enough to keep tokens from leaking
# to a private IP when a public endpoint redirects elsewhere.
OPENER = urllib.request.build_opener(SafeRedirectHandler())


def _http_get(url: str, headers: dict | None = None, timeout: int = _DEFAULT_TIMEOUT) -> tuple[bytes, str]:
    """GET *url* and return ``(body_bytes, final_url)``. Raises on HTTP error.

    Redirects are followed only when the destination host passes the same
    private-IP check the caller did on the seed URL — see ``SafeRedirectHandler``.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.7,zh;q=0.6",
            "Accept-Encoding": "gzip",
            **(headers or {}),
        },
    )
    with OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 - trusted user input via tool
        data = resp.read(_MAX_BYTES + 1)
        encoding = resp.headers.get("Content-Encoding", "").lower()
        if encoding == "gzip":
            try:
                data = gzip.decompress(data)
            except OSError:
                pass
        return data, resp.geturl()


def _decode(body: bytes) -> str:
    """Decode response bytes, honoring the ``Content-Type`` charset hint where
    possible. The previous heuristic (``utf-8 → gbk → latin-1``) silently
    mis-decoded Korean/Russian pages into plausible-looking Chinese — better
    to fall back to ``utf-8 errors='replace'`` which produces ``�``
    placeholders that downstream prompts can see and ignore.

    Callers that hold the ``Content-Type`` header can pre-pass it via
    ``_decode_with_charset`` (kept private until a need for charset-aware
    decoding actually surfaces — most pages serve UTF-8 today).
    """
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------
def _search_duckduckgo(query: str, limit: int) -> List[dict]:
    """Scrape DuckDuckGo's HTML endpoint. Returns ``[{title, url, snippet}]``."""
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    try:
        body, _ = _http_get(url)
    except Exception as exc:
        raise RuntimeError(f"DuckDuckGo request failed: {exc}") from exc

    html = _decode(body)
    results: List[dict] = []
    # Each result block looks roughly like:
    # <a class="result__a" href="LINK">TITLE</a>...<a class="result__snippet">SNIPPET</a>
    pattern = re.compile(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
        r'.*?<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )
    for match in pattern.finditer(html):
        raw_href = match.group(1)
        title = _strip_tags(match.group(2))
        snippet = _strip_tags(match.group(3))
        link = _unwrap_ddg_link(raw_href)
        if not link:
            continue
        results.append({"title": title.strip(), "url": link, "snippet": snippet.strip()})
        if len(results) >= limit:
            break
    return results


def _unwrap_ddg_link(href: str) -> str:
    """DuckDuckGo wraps results as //duckduckgo.com/l/?uddg=<URL>. Unwrap it."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path == "/l/":
        qs = urllib.parse.parse_qs(parsed.query)
        if "uddg" in qs and qs["uddg"]:
            return urllib.parse.unquote(qs["uddg"][0])
    return href


def _search_tavily(query: str, limit: int, api_key: str) -> List[dict]:
    payload = json.dumps({"query": query, "max_results": limit, "api_key": api_key}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT},
        method="POST",
    )
    # Use the shared OPENER so a Tavily-side compromise (or DNS hijack)
    # can't 30x-redirect the request — with API key in the headers — to a
    # private/loopback address. The seed URL is the known-good public host;
    # SafeRedirectHandler covers the rest.
    with OPENER.open(req, timeout=_DEFAULT_TIMEOUT) as resp:  # noqa: S310
        data = json.loads(resp.read().decode("utf-8"))
    return [
        {
            "title": item.get("title", "").strip(),
            "url": item.get("url", "").strip(),
            "snippet": (item.get("content") or "").strip(),
        }
        for item in (data.get("results") or [])
    ]


def web_search(query: str, limit: int = 5, provider: str = "auto") -> dict:
    """Search the web. ``provider`` is "duckduckgo", "tavily", or "auto"."""
    query = (query or "").strip()
    if not query:
        return {"success": False, "error": "Empty query."}
    limit = max(1, min(int(limit or 5), 10))

    chosen = provider.lower()
    tavily_key = os.getenv("TAVILY_API_KEY", "").strip()

    if chosen == "auto":
        chosen = "tavily" if tavily_key else "duckduckgo"

    try:
        if chosen == "tavily":
            if not tavily_key:
                return {"success": False, "error": "TAVILY_API_KEY not set."}
            results = _search_tavily(query, limit, tavily_key)
        elif chosen == "duckduckgo":
            results = _search_duckduckgo(query, limit)
        else:
            return {"success": False, "error": f"Unknown provider: {provider!r}."}
    except Exception as exc:
        return {"success": False, "error": str(exc), "provider": chosen}

    return {"success": True, "provider": chosen, "query": query, "results": results}


# ---------------------------------------------------------------------------
# web_extract
# ---------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    """Minimal HTML→text: drops script/style, collapses whitespace."""

    SKIP_TAGS = {"script", "style", "noscript", "svg", "header", "footer", "nav"}

    def __init__(self) -> None:
        super().__init__()
        self._buf: List[str] = []
        self._skip_depth = 0
        self._title: List[str] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._buf.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._buf.append("\n")

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        if self._in_title:
            self._title.append(data)
        else:
            self._buf.append(data)

    @property
    def title(self) -> str:
        return " ".join(t.strip() for t in self._title if t.strip())

    @property
    def text(self) -> str:
        raw = "".join(self._buf)
        # Collapse runs of whitespace within lines, preserve paragraph breaks.
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw.splitlines()]
        # Drop empty lines but keep paragraph separators.
        compact: List[str] = []
        prev_blank = False
        for ln in lines:
            if ln:
                compact.append(ln)
                prev_blank = False
            elif not prev_blank:
                compact.append("")
                prev_blank = True
        return "\n".join(compact).strip()


def _strip_tags(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text


def web_extract(url: str, max_chars: int = 8000) -> dict:
    """Fetch a URL and return ``{title, url, text}`` truncated to ``max_chars``."""
    url = (url or "").strip()
    if not url:
        return {"success": False, "error": "Empty URL."}
    if not url.startswith(("http://", "https://")):
        return {"success": False, "error": "URL must start with http:// or https://"}
    parsed = urllib.parse.urlparse(url)
    allowed, reason = hostname_is_safe(parsed.hostname or "")
    if not allowed:
        return {
            "success": False,
            "error": (
                f"Refused: {reason}. Internal/private addresses are blocked to "
                "prevent SSRF. Set LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS=1 to opt "
                "out (development only)."
            ),
            "url": url,
        }
    try:
        body, final_url = _http_get(url)
    except Exception as exc:
        return {"success": False, "error": f"Fetch failed: {exc}", "url": url}

    html = _decode(body)
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = parser.text
    truncated = False
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    return {
        "success": True,
        "title": parser.title,
        "url": final_url,
        "text": text,
        "truncated": truncated,
        "byteLength": len(body),
    }


# ---------------------------------------------------------------------------
# web_crawl — same-domain BFS, no LLM summarization
# ---------------------------------------------------------------------------
_HREF_PATTERN = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)


def _extract_links(html: str, base_url: str) -> list[str]:
    """Extract absolute http(s) links from an HTML body."""
    found: list[str] = []
    seen: set[str] = set()
    for href in _HREF_PATTERN.findall(html):
        absolute = urllib.parse.urljoin(base_url, href.strip())
        if "#" in absolute:
            absolute = absolute.split("#", 1)[0]
        if not absolute.startswith(("http://", "https://")):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        found.append(absolute)
    return found


def web_crawl(
    url: str,
    max_pages: int = 5,
    max_chars_per_page: int = 4000,
    same_host_only: bool = True,
    include_links: bool = False,
) -> dict:
    """BFS-crawl a website starting from ``url``.

    Stays on the seed host by default. No JS rendering, no LLM summarization.
    Returns ``{success, seed, pages: [{url, title, text, links?}]}``.
    """
    url = (url or "").strip()
    if not url:
        return {"success": False, "error": "Empty URL."}
    if not url.startswith(("http://", "https://")):
        return {"success": False, "error": "URL must start with http:// or https://"}

    max_pages = max(1, min(int(max_pages or 5), 25))
    max_chars_per_page = max(200, min(int(max_chars_per_page or 4000), 20000))

    parsed_seed = urllib.parse.urlparse(url)
    seed_host = (parsed_seed.hostname or "").lower()
    allowed, reason = hostname_is_safe(seed_host)
    if not allowed:
        return {"success": False, "error": f"Refused: {reason}.", "url": url}

    queue: list[str] = [url]
    visited: set[str] = set()
    pages: list[dict] = []

    while queue and len(pages) < max_pages:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        current_host = (urllib.parse.urlparse(current).hostname or "").lower()
        if same_host_only and current_host != seed_host:
            continue
        host_ok, _ = hostname_is_safe(current_host)
        if not host_ok:
            continue

        try:
            body, final_url = _http_get(current)
        except Exception as exc:
            pages.append({"url": current, "error": f"Fetch failed: {exc}"})
            continue

        html = _decode(body)
        parser = _TextExtractor()
        parser.feed(html)
        parser.close()
        text = parser.text
        truncated = len(text) > max_chars_per_page
        if truncated:
            text = text[:max_chars_per_page]

        page: dict = {
            "url": final_url,
            "title": parser.title,
            "text": text,
            "truncated": truncated,
        }
        links = _extract_links(html, final_url)
        if include_links:
            page["links"] = links[:50]
        pages.append(page)

        if len(pages) < max_pages:
            for link in links:
                if link in visited:
                    continue
                if same_host_only and (urllib.parse.urlparse(link).hostname or "").lower() != seed_host:
                    continue
                queue.append(link)

    return {
        "success": True,
        "seed": url,
        "pages_crawled": len(pages),
        "pages": pages,
    }
