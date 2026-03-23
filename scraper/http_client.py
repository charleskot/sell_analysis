"""Anti-detection HTTP client with rate limiting, UA rotation, and proxy support."""
import hashlib
import itertools
import logging
import random
import time
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}


class RateLimiter:
    def __init__(self, delay_min: float, delay_max: float):
        self._delay_min = delay_min
        self._delay_max = delay_max
        self._last_request: dict[str, float] = {}
        self._request_count: dict[str, int] = {}

    def wait(self, domain: str) -> None:
        now = time.time()
        last = self._last_request.get(domain, 0)
        elapsed = now - last
        delay = random.uniform(self._delay_min, self._delay_max)

        count = self._request_count.get(domain, 0)
        if count > 0 and count % random.randint(10, 15) == 0:
            delay += random.uniform(3, 8)

        if elapsed < delay:
            time.sleep(delay - elapsed)

        self._last_request[domain] = time.time()
        self._request_count[domain] = count + 1


class ProxyPool:
    def __init__(self, proxy_list: list[str]):
        self._proxies = proxy_list
        self._failed: set[str] = set()
        self._cycle = itertools.cycle(proxy_list) if proxy_list else None

    def next_proxy(self) -> dict | None:
        if not self._proxies:
            return None
        available = [p for p in self._proxies if p not in self._failed]
        if not available:
            self._failed.clear()
            available = self._proxies
        proxy = random.choice(available)
        return {"http": proxy, "https": proxy}

    def mark_failed(self, proxy_url: str) -> None:
        self._failed.add(proxy_url)


class HttpClient:
    def __init__(self, config: dict):
        self._delay_min = config.get("delay_min_seconds", 3)
        self._delay_max = config.get("delay_max_seconds", 8)
        self._max_retries = config.get("max_retries", 3)
        self._rate_limiter = RateLimiter(self._delay_min, self._delay_max)
        self._proxy_pool = ProxyPool(config.get("proxy_list", []))
        self._ua_list = USER_AGENTS[:]
        random.shuffle(self._ua_list)
        self._ua_cycle = itertools.cycle(self._ua_list)
        self._session = self._new_session()
        self._request_count = 0

    def _new_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=self._max_retries,
            backoff_factor=2,
            status_forcelist=[429, 503, 502],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def _rotate_session_if_needed(self) -> None:
        self._request_count += 1
        if self._request_count % random.randint(15, 25) == 0:
            self._session = self._new_session()
            logger.debug("Rotated HTTP session")

    def get(self, url: str, referer: str | None = None, extra_headers: dict | None = None, timeout: int = 20) -> requests.Response | None:
        domain = urlparse(url).netloc
        self._rate_limiter.wait(domain)
        self._rotate_session_if_needed()

        ua = next(self._ua_cycle)
        headers = {**BASE_HEADERS, "User-Agent": ua}
        if referer:
            headers["Referer"] = referer
        if extra_headers:
            headers.update(extra_headers)

        proxies = self._proxy_pool.next_proxy()

        try:
            resp = self._session.get(url, headers=headers, proxies=proxies, timeout=timeout)
            if self._is_blocked(resp):
                logger.warning(f"Blocked response ({resp.status_code}) from {domain}")
                return None
            logger.debug(f"GET {url} -> {resp.status_code}")
            return resp
        except requests.RequestException as e:
            logger.error(f"Request failed for {url}: {e}")
            return None

    def _is_blocked(self, resp: requests.Response) -> bool:
        if resp.status_code == 403:
            return True
        if resp.status_code in (200, 301, 302):
            text = resp.text[:2000].lower()
            if "just a moment" in text or "cf-browser-verification" in text:
                return True
            if "acceso denegado" in text or "access denied" in text:
                return True
        return False

    @staticmethod
    def hash_content(content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()[:16]
