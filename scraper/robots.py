"""robots.txt compliance checker with 24h TTL cache."""
import logging
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

logger = logging.getLogger(__name__)

_CACHE_TTL = 86400  # 24 hours


class RobotsChecker:
    def __init__(self):
        self._cache: dict[str, tuple[RobotFileParser, float]] = {}

    def is_allowed(self, url: str, user_agent: str = "*") -> bool:
        domain = urlparse(url).netloc
        scheme = urlparse(url).scheme
        robots_url = f"{scheme}://{domain}/robots.txt"

        parser, ts = self._cache.get(domain, (None, 0))
        if parser is None or time.time() - ts > _CACHE_TTL:
            parser = RobotFileParser(robots_url)
            try:
                parser.read()
                self._cache[domain] = (parser, time.time())
                logger.debug(f"Fetched robots.txt for {domain}")
            except Exception as e:
                logger.warning(f"Could not fetch robots.txt for {domain}: {e}")
                return True

        return parser.can_fetch(user_agent, url)
