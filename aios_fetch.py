"""Fetch a web page for an agent (the `fetch_url` tool). Standard library only.

* GET only, http and https only, no user:password in the URL.
* Redirects are NOT followed: the answer names the new URL, and fetching it is a new tool call that
  goes through the scope rules and approval again (so `allow = ["https://docs.python.org/*"]` cannot
  be escaped through a redirect).
* Addresses that are not public (loopback, private networks, link-local such as the cloud metadata
  service 169.254.169.254, ...) are refused unless allow_private. A direct connection goes to the
  address that was checked, so a name cannot resolve to a public address for the check and to a
  private one for the connection. Through an HTTP(S) proxy the proxy resolves names: only literal
  addresses and localhost are checked here.
* At most max_bytes of the body are read; HTML is turned into plain text; other non-text types are
  described, not shown.
"""
import asyncio
import http.client
import ipaddress
import socket
import ssl
import urllib.parse
import urllib.request
from html.parser import HTMLParser

MAX_BYTES = 1_000_000
TIMEOUT = 20
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml+xml", "application/javascript",
              "application/ld+json", "application/rss+xml", "application/atom+xml")


class FetchError(ValueError):
    pass


def _public(ip: str) -> bool:
    a = ipaddress.ip_address(ip.split("%")[0])
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not a.is_multicast


def _literal_ip(host: str):
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def check_url(url: str) -> urllib.parse.SplitResult:
    u = urllib.parse.urlsplit(url.strip())
    if u.scheme not in ("http", "https"):
        raise FetchError("only http:// and https:// URLs can be fetched")
    if not u.hostname:
        raise FetchError("the URL has no host")
    if u.username is not None or u.password is not None:
        raise FetchError("URLs with a user name or password are refused")
    try:
        u.port
    except ValueError:
        raise FetchError("bad port in the URL") from None
    return u


def _resolve(host: str, port: int, allow_private: bool) -> str:
    """The address to connect to: the first one, after checking that every one is public."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError(f"cannot resolve {host}: {e.strerror}") from None
    addrs = [i[4][0] for i in infos]
    if not allow_private and not all(_public(a) for a in addrs):
        raise FetchError(f"{host} is not a public address ({', '.join(sorted(set(addrs)))}): refused")
    return addrs[0]


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, ip, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self._ip, self._ctx = ip, context

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._ctx.wrap_socket(sock, server_hostname=self.host)  # certificate checked for the name


def _connection(u: urllib.parse.SplitResult, allow_private: bool, timeout: float):
    """(connection, request target) for this URL, through a proxy when the environment names one."""
    host, https = u.hostname, u.scheme == "https"
    port = u.port or (443 if https else 80)
    ctx = ssl.create_default_context()
    lit = _literal_ip(host)
    if not allow_private and (host.lower().rstrip(".") == "localhost" or host.lower().endswith(".localhost")
                              or (lit is not None and not _public(str(lit)))):
        raise FetchError(f"{host} is not a public address: refused")
    target = urllib.parse.urlunsplit(("", "", u.path or "/", u.query, ""))
    proxy = urllib.request.getproxies().get(u.scheme)
    if proxy and not urllib.request.proxy_bypass(host):
        p = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
        pport = p.port or 80
        if https:  # CONNECT tunnel; TLS is checked against the site's name, not the proxy's
            conn = http.client.HTTPSConnection(p.hostname, pport, timeout=timeout, context=ctx)
            conn.set_tunnel(host, port)
            return conn, target
        conn = http.client.HTTPConnection(p.hostname, pport, timeout=timeout)
        return conn, urllib.parse.urlunsplit((u.scheme, u.netloc, u.path or "/", u.query, ""))
    ip = _resolve(host, port, allow_private)
    if https:
        return _PinnedHTTPS(host, port, ip, timeout, ctx), target
    return _PinnedHTTP(host, port, ip, timeout), target


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
             "header", "footer", "pre", "blockquote", "table", "ul", "ol", "dd", "dt", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.title, self._in_title = [], 0, "", False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self.skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    p.close()
    lines = (" ".join(line.split()) for line in "".join(p.out).splitlines())
    text = "\n".join(line for line in lines if line)
    title = " ".join(p.title.split())
    return f"# {title}\n\n{text}" if title else text


def _fetch(url: str, max_bytes: int, timeout: float, allow_private: bool) -> str:
    u = check_url(url)
    conn, target = _connection(u, allow_private, timeout)
    try:
        conn.request("GET", target, headers={"Host": u.netloc.rsplit("@", 1)[-1], "User-Agent": "aios-fetch/1",
                                             "Accept": "text/html, text/*;q=0.9, */*;q=0.5",
                                             "Accept-Encoding": "identity", "Connection": "close"})
        r = conn.getresponse()
        head = f"HTTP {r.status} {urllib.parse.urlunsplit(u)}"
        if 300 <= r.status < 400 and r.getheader("Location"):
            new = urllib.parse.urljoin(urllib.parse.urlunsplit(u), r.getheader("Location"))
            return (f"{head}\nredirected to {new}\n(not followed: fetch that URL to follow it; "
                    f"it is checked against your rules again)")
        body = r.read(max_bytes + 1)
    except (OSError, http.client.HTTPException) as e:
        raise FetchError(f"cannot fetch {urllib.parse.urlunsplit(u)}: {type(e).__name__}: {e}") from None
    finally:
        conn.close()
    cut = len(body) > max_bytes
    body = body[:max_bytes]
    ctype = (r.getheader("Content-Type") or "").lower()
    mime = ctype.split(";")[0].strip()
    charset = "utf-8"
    for part in ctype.split(";")[1:]:
        k, _, v = part.strip().partition("=")
        if k == "charset" and v:
            charset = v.strip('"')
    if mime and not mime.startswith(TEXT_TYPES) and not mime.endswith(("+json", "+xml")):
        return f"{head}\ncontent-type: {mime}\n\n(binary content, {len(body)}{'+' if cut else ''} bytes: not shown)"
    try:
        text = body.decode(charset, "replace")
    except LookupError:
        text = body.decode("utf-8", "replace")
    if mime in ("text/html", "application/xhtml+xml") or (not mime and "<html" in text[:500].lower()):
        text = html_to_text(text)
    return (f"{head}\ncontent-type: {mime or 'unknown'}\n\n{text}"
            + (f"\n...(truncated after {max_bytes} bytes)" if cut else ""))


def post_json(url: str, body: bytes, headers: dict, timeout: float = TIMEOUT, allow_private: bool = False) -> int:
    """POST body to url with the same address checks as a fetch (used for A2A push notifications);
    the HTTP status. Redirects are not followed and at most 64 KB of the reply is read."""
    u = check_url(url)
    conn, target = _connection(u, allow_private, timeout)
    try:
        conn.request("POST", target, body=body, headers={"Host": u.netloc, "User-Agent": "aios-fetch/1",
                                                         "Connection": "close", **headers})
        r = conn.getresponse()
        r.read(65536)
        return r.status
    except (OSError, http.client.HTTPException) as e:
        raise FetchError(f"cannot reach {urllib.parse.urlunsplit(u)}: {type(e).__name__}: {e}") from None
    finally:
        conn.close()


async def fetch(url: str, max_bytes: int = MAX_BYTES, timeout: float = TIMEOUT, allow_private: bool = False) -> str:
    return await asyncio.to_thread(_fetch, url, max_bytes, timeout, allow_private)
