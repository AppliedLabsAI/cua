"""DNS-backed SSRF protection helpers."""

from __future__ import annotations

import concurrent.futures
import ipaddress
import logging
import socket
from collections import OrderedDict

log = logging.getLogger(__name__)

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def is_private_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check if an IP address falls within any blocked private network."""
    return any(addr in network for network in _PRIVATE_NETWORKS)


class DnsProtection:
    """Resolve hostnames and block private/internal network targets."""

    def __init__(self, *, timeout_s: float = 2.0, cache_max: int = 1024) -> None:
        self._timeout_s = timeout_s
        self._cache_max = cache_max
        self._cache: OrderedDict[str, str | None] = OrderedDict()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def check_hostname(self, hostname: str) -> str | None:
        """Return a blocking reason if the hostname resolves to a private IP."""
        if hostname in ("localhost", "localhost.localdomain"):
            return "Blocked: localhost (SSRF protection)"

        try:
            addr = ipaddress.ip_address(hostname)
        except ValueError:
            return self._check_resolved_hostname(hostname)

        if is_private_ip(addr):
            return f"Blocked: private IP {hostname} (SSRF protection)"
        return None

    def _check_resolved_hostname(self, hostname: str) -> str | None:
        if hostname in self._cache:
            self._cache.move_to_end(hostname)
            return self._cache[hostname]

        reason = self._resolve_and_check(hostname)
        if self._cache_max > 0:
            if len(self._cache) >= self._cache_max:
                self._cache.popitem(last=False)
            self._cache[hostname] = reason
            self._cache.move_to_end(hostname)
        return reason

    def _resolve_and_check(self, hostname: str) -> str | None:
        def do_resolve():
            return socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)

        try:
            future = self._executor.submit(do_resolve)
            addrinfos = future.result(timeout=self._timeout_s)
            for _family, _type, _proto, _canonname, sockaddr in addrinfos:
                ip_str = sockaddr[0]
                try:
                    addr = ipaddress.ip_address(ip_str)
                except ValueError:
                    continue
                if is_private_ip(addr):
                    log.warning(
                        "SSRF blocked: %s resolves to private IP %s", hostname, ip_str
                    )
                    return (
                        f"Blocked: {hostname} resolves to private IP {ip_str} "
                        "(SSRF protection)"
                    )
        except (socket.gaierror, concurrent.futures.TimeoutError) as exc:
            log.debug(
                "DNS resolution failed for %s: %s",
                hostname,
                exc,
                exc_info=True,
            )
        return None

    def close(self) -> None:
        """Shut down the shared resolver executor."""
        self._executor.shutdown(wait=False)
