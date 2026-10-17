"""Bounded HTTP smoke checks with public-address pinning on every redirect."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlparse


@dataclass(frozen=True)
class Check:
    name: str
    state: str
    detail: str


@dataclass(frozen=True)
class Verification:
    url: str
    checks: tuple[Check, ...]
    verdict: str

    def to_dict(self) -> dict[str, object]:
        return {
            "url": self.url,
            "checks": [asdict(check) for check in self.checks],
            "verdict": self.verdict,
        }


class _Assets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        item = (
            values.get("src")
            if tag == "script"
            else values.get("href")
            if tag == "link" and "stylesheet" in (values.get("rel") or "").split()
            else None
        )
        if item:
            self.urls.append(item)


def _addresses(host: str, *, allow_private: bool) -> list[str]:
    addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})
    if not addresses:
        raise ValueError("DNS returned no addresses")
    if not allow_private and any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError("URL resolves to a non-public address; --allow-private is required for local checks")
    return addresses


def _fetch(
    url: str, *, allow_private: bool, timeout: int
) -> tuple[int, str, dict[str, str], bytes, str | None, str | None, list[str]]:
    """Resolve, pin, request, and repeat for at most five redirects."""
    current = url
    first_addresses: list[str] = []
    for redirect in range(6):
        parsed = urlparse(current)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Verification requires a credential-free HTTP(S) URL")
        hostname = parsed.hostname.encode("idna").decode("ascii")
        addresses = _addresses(hostname, allow_private=allow_private)
        if redirect == 0:
            first_addresses = addresses
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        raw = None
        last_error: OSError | None = None
        for address in addresses:
            try:
                raw = socket.create_connection((address, port), timeout=timeout)
                break
            except OSError as exc:
                last_error = exc
        if raw is None:
            raise last_error or OSError("No resolved address was reachable")
        sock = raw
        tls_version: str | None = None
        cert_expiry: str | None = None
        try:
            if parsed.scheme == "https":
                sock = ssl.create_default_context().wrap_socket(raw, server_hostname=hostname)
                tls_version = sock.version()
                cert_expiry = sock.getpeercert().get("notAfter")
            else:
                sock = raw
            host_header = hostname if port in {80, 443} else f"{hostname}:{port}"
            path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
            if parsed.query:
                path += "?" + quote(parsed.query, safe="=&?/%:@!$'()*+,;=-._~")
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host_header}\r\n"
                "User-Agent: DevOps-Agent/0.1\r\n"
                "Connection: close\r\n"
                "Accept: text/html,*/*\r\n\r\n"
            )
            sock.sendall(request.encode("ascii"))
            response = http.client.HTTPResponse(sock)
            response.begin()
            status = response.status
            headers = {key.lower(): value for key, value in response.getheaders()}
            body = response.read(1024 * 1024)
        finally:
            sock.close()
        if status in {301, 302, 303, 307, 308} and headers.get("location"):
            current = urljoin(current, headers["location"])
            continue
        return status, current, headers, body, tls_version, cert_expiry, first_addresses
    raise ValueError("Too many redirects")


def verify(url: str, *, allow_private: bool = False, timeout: int = 10) -> Verification:
    checks: list[Check] = []
    try:
        status, final_url, headers, body, tls, expiry, addresses = _fetch(
            url, allow_private=allow_private, timeout=timeout
        )
        checks.append(Check("dns", "PASSED", ", ".join(addresses)))
        checks.append(
            Check(
                "https",
                "PASSED" if urlparse(final_url).scheme == "https" else "DEGRADED",
                f"TLS {tls}; certificate expires {expiry}" if tls else "Final URL uses HTTP",
            )
        )
        checks.append(
            Check(
                "http",
                "PASSED" if 200 <= status < 400 else "FAILED",
                f"HTTP {status}; final URL {final_url}",
            )
        )
        security_headers = headers.get("content-security-policy") or headers.get("x-content-type-options")
        checks.append(
            Check(
                "security_headers",
                "PASSED" if security_headers else "DEGRADED",
                "CSP or X-Content-Type-Options observed"
                if security_headers
                else "CSP and X-Content-Type-Options absent",
            )
        )
        if "text/html" in headers.get("content-type", ""):
            parser = _Assets()
            parser.feed(body.decode("utf-8", errors="replace"))
            origin = urlparse(final_url)
            assets = []
            for item in parser.urls:
                candidate = urljoin(final_url, item)
                target = urlparse(candidate)
                if target.scheme == origin.scheme and target.netloc == origin.netloc:
                    assets.append(candidate)
            for asset_url in assets[:3]:
                try:
                    asset_status, _, _, _, _, _, _ = _fetch(asset_url, allow_private=allow_private, timeout=timeout)
                    checks.append(
                        Check(
                            "asset",
                            "PASSED" if asset_status < 400 else "FAILED",
                            f"HTTP {asset_status}: {asset_url}",
                        )
                    )
                except (OSError, ValueError, ssl.SSLError, http.client.HTTPException) as exc:
                    checks.append(Check("asset", "FAILED", f"{asset_url}: {exc}"))
    except (OSError, ValueError, UnicodeError, ssl.SSLError, http.client.HTTPException) as exc:
        checks.append(Check("connectivity", "FAILED", str(exc)))
    verdict = (
        "FAILED"
        if any(c.state == "FAILED" for c in checks)
        else "DEGRADED"
        if any(c.state == "DEGRADED" for c in checks)
        else "HEALTHY"
        if checks
        else "UNVERIFIED"
    )
    return Verification(url, tuple(checks), verdict)
