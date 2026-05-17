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
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import List


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_DEFAULT_TIMEOUT = 15
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


def _hostname_is_safe(hostname: str) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for a parsed URL hostname.

    Resolves DNS once and inspects every A/AAAA record — a single hostname
    can have both a public and a private record (DNS rebinding mitigation
    happens by resolving here, then a second time inside urlopen, which would
    re-validate if we wrapped urlopen too; we don't, so this is best-effort
    against active adversaries but solid against accidental misuse).
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


def _http_get(url: str, headers: dict | None = None, timeout: int = _DEFAULT_TIMEOUT) -> tuple[bytes, str]:
    """GET *url* and return ``(body_bytes, final_url)``. Raises on HTTP error."""
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
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - trusted user input via tool
        data = resp.read(_MAX_BYTES + 1)
        encoding = resp.headers.get("Content-Encoding", "").lower()
        if encoding == "gzip":
            try:
                data = gzip.decompress(data)
            except OSError:
                pass
        return data, resp.geturl()


def _decode(body: bytes) -> str:
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
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
    with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:  # noqa: S310
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
    allowed, reason = _hostname_is_safe(parsed.hostname or "")
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
