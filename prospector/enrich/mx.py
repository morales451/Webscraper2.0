"""Check that an email's domain can receive mail (has MX records, or at least an A record).

Uses DNS-over-HTTPS so it works the same on every OS and network, with no extra dependency.
This catches dead domains and typos; it can't prove a specific mailbox exists.
"""

from __future__ import annotations

import asyncio

import httpx

DOH_ENDPOINTS = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
)

OK = "ok"            # domain has MX records
OK_A = "ok_a"        # no MX, but an A record (mail may still be delivered there)
INVALID = "invalid"  # domain doesn't exist or has no mail/A records
UNKNOWN = "unknown"  # lookup failed (network); don't hold it against the lead

STATUS_LABELS = {
    OK: "domain accepts email",
    OK_A: "no MX record (may still work)",
    INVALID: "domain can't receive email",
    UNKNOWN: "not checked",
}


class MxChecker:
    def __init__(self, client: httpx.AsyncClient | None = None, timeout: float = 6.0):
        self._client = client
        self._owns = client is None
        self.timeout = timeout
        self._cache: dict[str, asyncio.Future] = {}

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def _query(self, name: str, rtype: str) -> tuple[int, list] | None:
        """Return (DNS status code, answers) or None if every resolver failed."""
        for endpoint in DOH_ENDPOINTS:
            try:
                resp = await self.client.get(
                    endpoint, params={"name": name, "type": rtype}, headers={"accept": "application/dns-json"}
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                return int(data.get("Status", 2)), data.get("Answer") or []
            except (httpx.HTTPError, ValueError):
                continue
        return None

    async def _check_domain(self, domain: str) -> str:
        mx = await self._query(domain, "MX")
        if mx is None:
            return UNKNOWN
        status, answers = mx
        if status == 3:  # NXDOMAIN
            return INVALID
        mx_hosts = [(a.get("data") or "").split()[-1:] for a in answers if a.get("type") == 15]
        if any(h and h[0] not in (".", "") for h in mx_hosts):
            return OK
        if mx_hosts:  # only a "null MX" (0 .): the domain explicitly accepts no email
            return INVALID
        a = await self._query(domain, "A")
        if a is None:
            return UNKNOWN
        return OK_A if any(x.get("type") == 1 for x in a[1]) else INVALID

    async def check_domain(self, domain: str) -> str:
        domain = domain.lower().strip(".")
        if domain not in self._cache:
            fut = asyncio.get_running_loop().create_future()
            self._cache[domain] = fut
            try:
                fut.set_result(await self._check_domain(domain))
            except Exception:
                fut.set_result(UNKNOWN)
        return await self._cache[domain]

    async def check_emails(self, emails: list[str]) -> dict[str, str]:
        domains = {e.split("@", 1)[1] for e in emails if "@" in e}
        results = dict(zip(domains, await asyncio.gather(*(self.check_domain(d) for d in domains))))
        return {e: results.get(e.split("@", 1)[1], UNKNOWN) for e in emails}

    async def close(self) -> None:
        if self._owns and self._client is not None:
            await self._client.aclose()
            self._client = None


def order_by_deliverability(emails: list[str], checks: dict[str, str]) -> list[str]:
    """Keep the existing ranking but move undeliverable addresses to the end."""
    return sorted(emails, key=lambda e: checks.get(e) == INVALID)
