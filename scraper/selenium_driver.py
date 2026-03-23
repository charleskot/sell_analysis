"""Selenium headless browser driver - lazy singleton for JS-heavy portals."""
import logging
import time
import random
from pathlib import Path

logger = logging.getLogger(__name__)

_driver = None


def _build_options(headless: bool = True):
    """Build Chrome options with anti-detection flags."""
    from selenium.webdriver.chrome.options import Options
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--lang=es-ES")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    return opts


def get_driver(headless: bool = True, use_undetected: bool = False):
    """Return a singleton Selenium WebDriver. Raises if Chrome unavailable."""
    global _driver
    if _driver is not None:
        try:
            _ = _driver.current_url  # check if still alive
            return _driver
        except Exception:
            _driver = None

    opts = _build_options(headless)

    if use_undetected:
        try:
            import undetected_chromedriver as uc
            _driver = uc.Chrome(options=opts, use_subprocess=True)
            logger.info("Using undetected-chromedriver")
            return _driver
        except ImportError:
            logger.warning("undetected-chromedriver not installed, falling back to standard selenium")

    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service

        # Try webdriver-manager first
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            service = Service(ChromeDriverManager().install())
            _driver = webdriver.Chrome(service=service, options=opts)
        except Exception:
            # Try system chromedriver
            _driver = webdriver.Chrome(options=opts)

        # Patch navigator.webdriver
        _driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
                Object.defineProperty(navigator, 'languages', {get: () => ['es-ES','es','en']});
            """
        })
        logger.info("Selenium Chrome driver initialized")
        return _driver
    except Exception as e:
        raise RuntimeError(f"Chrome/ChromeDriver not available: {e}") from e


def get_page_html(url: str, wait_selector: str = None, wait_seconds: float = 3.0,
                  headless: bool = True, use_undetected: bool = False) -> str | None:
    """Fetch page HTML after JS rendering. Returns None if Chrome unavailable."""
    try:
        driver = get_driver(headless=headless, use_undetected=use_undetected)
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.common.by import By

        driver.get(url)
        time.sleep(random.uniform(wait_seconds, wait_seconds + 1.5))

        if wait_selector:
            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, wait_selector))
                )
            except Exception:
                logger.debug(f"Wait selector {wait_selector!r} not found, continuing")

        return driver.page_source
    except RuntimeError as e:
        logger.warning(f"Selenium unavailable: {e}")
        return None
    except Exception as e:
        logger.error(f"Selenium error fetching {url}: {e}")
        return None


def close_driver():
    global _driver
    if _driver:
        try:
            _driver.quit()
        except Exception:
            pass
        _driver = None
