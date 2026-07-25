import errno
import random
import time
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import requests
from selenium.webdriver.support.ui import WebDriverWait

_COLORS = ["\033[95m", "\033[94m", "\033[96m", "\033[92m", "\033[93m", "\033[91m"]
_RESET = "\033[0m"


def colorize(worker_id: int, msg: str) -> str:
    c = _COLORS[worker_id % len(_COLORS)]
    return f"{c}[W{worker_id}] {msg}{_RESET}"


def jitter_short():
    time.sleep(0.25 + random.random() * 0.35)  # short human-like pause before UI interactions


def _is_win_socket_10055(exc: BaseException) -> bool:
    winerr = getattr(exc, "winerror", None)
    errnum = getattr(exc, "errno", None)
    return (winerr == 10055) or (errnum == errno.ENOBUFS)


def backoff_forever_on_10055(fn):
    # Windows runs out of ephemeral socket buffers under sustained Selenium traffic; retrying is the only fix
    while True:
        try:
            return fn()
        except OSError as e:
            if _is_win_socket_10055(e):
                time.sleep(2)
                continue
            raise


def safe_driver_get(driver, url):
    return backoff_forever_on_10055(lambda: driver.get(url))


def safe_request(session: requests.Session, method: str, url: str, **kw):
    return backoff_forever_on_10055(lambda: session.request(method, url, **kw))


def add_lang(url: str, lang="en"):
    try:
        parts = urlparse(url)
        q = dict(parse_qsl(parts.query))
        q["lang"] = lang
        return urlunparse((parts.scheme, parts.netloc, parts.path, parts.params, urlencode(q), parts.fragment))
    except Exception:
        return url


def wait_for_page_load(driver, timeout=60):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )


def scroll_page(driver, pause_time=4):
    y = 0
    while True:
        y += 1200
        driver.execute_script(f"window.scrollTo(0, {y});")
        time.sleep(pause_time / 2)
        new_h = driver.execute_script(
            "return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
        )
        if y >= new_h:
            break
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(0.3)
