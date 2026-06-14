"""
Web tools: web_search and web_extract.

- ``web_search``  — Baidu / Startpage (Google proxy) / DuckDuckGo HTML
  endpoints (no API key required) with an optional Tavily provider when
  ``TAVILY_API_KEY`` is set.  ``"google"`` is accepted as an alias for
  ``"startpage"``.  Auto mode tries Baidu first and falls back on CAPTCHA.
- ``web_extract`` — fetch a URL and return readable plain text (best-effort
  HTML stripping; no JS rendering).

The HTTP layer uses only the Python standard library; no extra dependencies.
"""

from __future__ import annotations

import io
import ipaddress
import zlib
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
# 40s (was 30s, was 15s): 15s was too tight for users behind cross-border
# links to duckduckgo.com / tavily.com; 30s still caused occasional timeouts
# for Google searches from mainland China.  40s gives enough headroom while
# still bounding the round trip below the orchestrator's attention budget.
_DEFAULT_TIMEOUT = 40
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


def _gunzip_capped(data: bytes, max_bytes: int) -> bytes:
    """Decompress gzip *data*, stopping once *max_bytes* of output is produced.

    A naive ``gzip.decompress`` on attacker-controlled input is a decompression
    bomb: a few KB of compressed zeros expands to gigabytes and OOMs the agent
    process. A prompt-injected ``web_extract`` of such a URL is a real vector,
    so we inflate incrementally and truncate at the cap instead of materialising
    the whole thing. On malformed gzip we return the raw bytes unchanged (same
    fail-soft behaviour the previous ``except OSError: pass`` had).
    """
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)  # 16 = gzip header
    out = bytearray()
    try:
        # Feed in chunks so the decompressor's unconsumed_tail lets us stop
        # early without inflating the remainder of a bomb.
        view = memoryview(data)
        step = 65536
        for start in range(0, len(view), step):
            chunk = decompressor.decompress(
                bytes(view[start:start + step]), max_bytes - len(out)
            )
            out.extend(chunk)
            if len(out) >= max_bytes:
                return bytes(out[:max_bytes])
        # Drain any buffered output the cap didn't already cover.
        while True:
            chunk = decompressor.decompress(b"", max_bytes - len(out))
            if not chunk:
                break
            out.extend(chunk)
            if len(out) >= max_bytes:
                break
    except zlib.error:
        return data
    return bytes(out[:max_bytes])


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
            # Cap the *decompressed* size, not just the compressed read above —
            # otherwise a gzip bomb expands unbounded in memory.
            data = _gunzip_capped(data, _MAX_BYTES)
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
# Failure classification
# ---------------------------------------------------------------------------
def _classify_fetch_failure(exc: Exception) -> tuple[bool, str]:
    """Classify a fetch/search exception as ``(retryable, advice)``.

    ``retryable=False`` is the contract that tells the calling LLM: re-issuing
    the same kind of request — a URL variant, a different provider, an
    immediate retry — will hit the *same* wall. The agent should fall back to
    existing knowledge or a genuinely different source instead of probing
    variants. This exists because, without the hint, an LLM planner reads a
    bare "failed" and naturally improvises retries (observed: 8 wasted calls
    against an anti-scraping site + a dead network path).

    Only *known* dead-ends are flagged non-retryable. Anything we can't reason
    about defaults to retryable so we never wrongly suppress a call that might
    have worked.

    ``_search_duckduckgo`` wraps its error in a ``RuntimeError`` (dropping the
    original ``.code``), so we match on the message string as well as any
    ``.code`` attribute on the exception or its ``__cause__``.
    """
    low = str(exc).lower()
    code = getattr(exc, "code", None)
    if code is None:
        cause = getattr(exc, "__cause__", None)
        code = getattr(cause, "code", None)

    # Redirect loop — classic anti-scraping. urllib raises HTTPError(302, …)
    # whose message contains "infinite loop"; SafeRedirectHandler refusals
    # also surface here.
    if "infinite loop" in low or "redirect" in low:
        return False, (
            "The site redirected in a loop — typically anti-scraping. Do not "
            "retry URL variants; answer from existing knowledge or try a "
            "different source."
        )

    if isinstance(code, int):
        if code == 429:
            return True, "Rate limited (HTTP 429). Back off, then retry later."
        if code == 408 or 500 <= code < 600:
            return True, (
                f"Transient server error (HTTP {code}). A later retry may succeed."
            )
        if 400 <= code < 500:
            return False, (
                f"HTTP {code}: the resource is unavailable (not found / forbidden). "
                "Retrying the same or a similar URL will not help."
            )

    # DNS resolution failure — the host doesn't resolve; variants won't either.
    if (
        isinstance(exc, socket.gaierror)
        or "getaddrinfo" in low
        or "name or service not known" in low
        or "nodename nor servname" in low
        or "11001" in low  # WSAHOST_NOT_FOUND
    ):
        return False, (
            "The host could not be resolved (DNS failure). Retrying the same "
            "host will not help; verify the URL or use a different source."
        )

    # Connection-layer failures: timeout / refused / unreachable / reset.
    # Includes the Windows WSA error codes seen in the wild (10060 timeout,
    # 10061 refused, 10065 unreachable, 10054 reset). Match on code text so
    # non-English locale messages (e.g. cp936 Chinese) still classify.
    if (
        isinstance(exc, (socket.timeout, TimeoutError))
        or "timed out" in low
        or "timeout" in low
        or "refused" in low
        or "unreachable" in low
        or "connection reset" in low
        or "10060" in low
        or "10061" in low
        or "10065" in low
        or "10054" in low
    ):
        return False, (
            "Could not reach the host (connection timeout/refused). The network "
            "path to this service is down; do not retry it — answer from "
            "existing knowledge or use a different source."
        )

    return True, "Unclassified fetch error; a single retry may be worth trying."


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


_BAUID_TITLE_RE = re.compile(
    r'<h3[^>]*class="t[^"]*"[^>]*>(.*?)</h3>',
    re.DOTALL | re.IGNORECASE,
)


def _search_baidu(query: str, limit: int) -> List[dict]:
    """Scrape Baidu search results. Returns ``[{title, url, snippet}]``.

    Baidu may return a CAPTCHA page when rate-limited — the caller should
    treat an empty result list as a soft failure and fall back.
    """
    url = "https://www.baidu.com/s?" + urllib.parse.urlencode(
        {"wd": query, "rn": limit, "ie": "utf-8"},
    )
    try:
        body, _ = _http_get(url)
    except Exception as exc:
        raise RuntimeError(f"Baidu request failed: {exc}") from exc

    html = _decode(body)

    # Detect CAPTCHA / verification page (typically < 5 KB and contains
    # "安全验证" or "verify").  Raise so the caller can fall back.
    if len(body) < 5000 and ("安全验证" in html or "verify" in html.lower()):
        raise RuntimeError("Baidu returned CAPTCHA page; try another provider")

    results: List[dict] = []
    for match in _BAUID_TITLE_RE.finditer(html):
        inner = match.group(1)
        title = re.sub(r"<[^>]+>", "", inner).strip()
        if not title:
            continue

        # Extract URL from the <a> inside the <h3>.
        href_m = re.search(r'href="(https?://[^"]+)"', inner)
        link = href_m.group(1) if href_m else ""

        # Walk forward for the snippet (class contains "summary-text").
        after = html[match.end() : match.end() + 5000]
        snippet = ""
        for pat in (
            r'class="[^"]*summary-text[^"]*"[^>]*>(.*?)</span>',
            r'class="c-abstract[^"]*"[^>]*>(.*?)</(?:div|p)',
        ):
            sm = re.search(pat, after, re.DOTALL)
            if sm:
                snippet = re.sub(r"<[^>]+>", "", sm.group(1)).strip()[:200]
                break

        if not link:
            continue
        results.append({"title": title, "url": link, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


_STARTPAGE_TITLE_RE = re.compile(
    r'<h2[^>]*class="wgl-title[^"]*"[^>]*>(.*?)</h2>',
    re.DOTALL | re.IGNORECASE,
)


def _search_startpage(query: str, limit: int) -> List[dict]:
    """Scrape Startpage (Google proxy) results. Returns ``[{title, url, snippet}]``."""
    url = "https://www.startpage.com/sp/search?" + urllib.parse.urlencode(
        {"query": query, "cat": "web"},
    )
    try:
        body, _ = _http_get(url)
    except Exception as exc:
        raise RuntimeError(f"Startpage request failed: {exc}") from exc

    html = _decode(body)
    results: List[dict] = []
    for match in _STARTPAGE_TITLE_RE.finditer(html):
        title = _strip_tags(match.group(1)).strip()
        if not title:
            continue

        # Walk backwards to find the enclosing <a href="…">.
        before = html[max(0, match.start() - 2000) : match.start()]
        href_match = None
        for hm in re.finditer(
            r'<a[^>]*href="(https?://[^"]+)"[^>]*>', before
        ):
            href_match = hm
        link = href_match.group(1) if href_match else ""

        # Walk forwards for the first <p>…</p> as the snippet.
        after = html[match.end() : match.end() + 1000]
        desc_m = re.search(r"<p[^>]*>(.*?)</p>", after, re.DOTALL)
        snippet = _strip_tags(desc_m.group(1)).strip() if desc_m else ""

        if not link:
            continue
        results.append({"title": title, "url": link, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


def web_search(query: str, limit: int = 5, provider: str = "auto") -> dict:
    """Search the web.

    ``provider`` is "baidu", "startpage", "google", "duckduckgo", "tavily",
    or "auto".  "google" is an alias for "startpage".

    When ``provider="auto"``, the fallback order is:
    Tavily (if key set) → Baidu → Startpage → DuckDuckGo.
    Baidu may trigger CAPTCHA under heavy use; the function automatically
    falls back to the next provider in the chain.
    """

    query = (query or "").strip()
    if not query:
        return {"success": False, "error": "Empty query."}
    limit = max(1, min(int(limit or 5), 10))

    chosen = provider.lower()
    tavily_key = os.getenv("TAVILY_API_KEY", "").strip()

    # Env override: WEB_SEARCH_DEFAULT_PROVIDER forces the default when auto.
    default_provider = os.getenv("WEB_SEARCH_DEFAULT_PROVIDER", "").strip().lower()

    # "google" is an alias — Startpage proxies Google results.
    if chosen == "google":
        chosen = "startpage"

    # Build the candidate list for auto / fallback.
    if chosen == "auto":
        candidates: list[str] = []
        if tavily_key:
            candidates.append("tavily")
        # Env override puts the specified provider first.
        if default_provider and default_provider not in candidates:
            candidates.append(default_provider)
        for _p in ("baidu", "startpage", "duckduckgo"):
            if _p not in candidates:
                candidates.append(_p)
    else:
        candidates = [chosen]

    last_exc: Exception | None = None
    for prov in candidates:
        try:
            if prov == "tavily":
                if not tavily_key:
                    return {"success": False, "error": "TAVILY_API_KEY not set."}
                results = _search_tavily(query, limit, tavily_key)
            elif prov == "baidu":
                results = _search_baidu(query, limit)
            elif prov == "startpage":
                results = _search_startpage(query, limit)
            elif prov == "duckduckgo":
                results = _search_duckduckgo(query, limit)
            else:
                return {"success": False, "error": f"Unknown provider: {provider!r}."}
            # If we got results, return immediately.
            if results:
                return {"success": True, "provider": prov, "query": query, "results": results}
            # Empty results — treat as soft failure and try next.
            last_exc = RuntimeError(f"{prov} returned 0 results")
        except Exception as exc:
            last_exc = exc
            # For explicit (non-auto) requests, fail immediately.
            if len(candidates) == 1:
                retryable, advice = _classify_fetch_failure(exc)
                return {
                    "success": False,
                    "error": str(exc),
                    "provider": prov,
                    "retryable": retryable,
                    "advice": advice,
                }
            # Auto mode — try next provider.
            continue

    # All candidates exhausted.
    return {
        "success": False,
        "error": str(last_exc) if last_exc else "All search providers failed",
        "provider": "auto",
        "retryable": True,
        "advice": "All providers failed; retry later.",
    }


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
            # A blocked private address won't unblock on a URL variant.
            "retryable": False,
            "advice": (
                "This address is blocked by policy (SSRF guard). Do not retry "
                "internal/private URLs."
            ),
        }
    try:
        body, final_url = _http_get(url)
    except Exception as exc:
        retryable, advice = _classify_fetch_failure(exc)
        return {
            "success": False,
            "error": f"Fetch failed: {exc}",
            "url": url,
            "retryable": retryable,
            "advice": advice,
        }

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
