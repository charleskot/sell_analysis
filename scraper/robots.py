"""robots.txt compliance checker with 24h TTL cache.
Uses requests with a real User-Agent to avoid 403 blocks on robots.txt itself.
"""
import logging
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
from io import StringIO

import requests

logger = logging.getLogger(__name__)

_CACHE_TTL = 86400  # 24 hours
_UA = "Mozilla/5.0 (compatible; RobotsChecker/1.0)"


class RobotsChecker:
    def __init__(self):
        self._cache: dict[str, tuple[RobotFileParser, float]] = {}

    def is_allowed(self, url: str, user_agent: str = "*") -> bool:
        domain = urlparse(url).netloc
        scheme = urlparse(url).scheme
        robots_url = f"{scheme}://{domain}/robots.txt"

        parser, ts = self._cache.get(domain, (None, 0))
        if parser is None or time.time() - ts > _CACHE_TTL:
            parser = self._fetch_parser(robots_url)
            self._cache[domain] = (parser, time.time())

        return parser.can_fetch(user_agent, url)

    def _fetch_parser(self, robots_url: str) -> RobotFileParser:
        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            resp = requests.get(
                robots_url,
                headers={"User-Agent": _UA},
                timeout=10,
            )
            if resp.status_code == 200:
                parser.parse(resp.text.splitlines())
                logger.debug(f"Fetched robots.txt for {robots_url}")
            elif resp.status_code in (401, 403, 404):
                # Can't read robots.txt → assume allowed (standard practice)
                logger.debug(f"robots.txt {resp.status_code} for {robots_url}, assuming allowed")
                parser.parse([])  # empty = allow all
            else:
                logger.warning(f"Unexpected {resp.status_code} for {robots_url}, assuming allowed")
                parser.parse([])
        except Exception as e:
            logger.warning(f"Could not fetch {robots_url}: {e}, assuming allowed")
            parser.parse([])
        return parser
