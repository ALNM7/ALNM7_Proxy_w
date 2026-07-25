import json
import re
import time
from typing import Optional, Tuple

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from .browser import rotate_and_recycle
from .utils import safe_driver_get, wait_for_page_load

THROTTLE_PATTERNS = [
    r"\bThrottled\s*\(429\)\b",
    r"You are sending too many requests to Kickstarter at this time\.",
    r"/ref=429errorpage",
]


def looks_like_cf_js_challenge(html_lower: str) -> bool:
    return any(s in html_lower for s in [
        "just a moment", "checking your browser", "cf-chl-widget", "cf-challenge", "turnstile"
    ])


def wait_cf_js_pass(driver, max_wait=10):
    t0 = time.time()
    while time.time() - t0 < max_wait:
        if not looks_like_cf_js_challenge(driver.page_source.lower()):
            return True
        time.sleep(1.0)
    return False


def is_human_verification_page(driver):
    try:
        txt = driver.page_source.lower()
        if "verify you are human" in txt or "are you human" in txt:
            return True
        if driver.find_elements(
            By.XPATH,
            "//iframe[contains(@src,'captcha') or contains(@title,'captcha') or contains(@src,'turnstile')]"
        ):
            return True
    except Exception:
        pass
    return False


def close_cookie_modal_if_present(driver, timeout=5):
    try:
        btn = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((
                By.XPATH,
                "//button[contains(., 'Accept all') or contains(., 'Accept All') "
                "or contains(., 'Aceptar todo') or contains(., 'I agree') "
                "or contains(., 'Allow all')]"
            ))
        )
        btn.click()
    except Exception:
        pass


def pop_last_graph_retry_after(driver) -> Tuple[Optional[int], Optional[str]]:
    try:
        entries = driver.get_log("performance")
    except Exception:
        return None, None
    for e in reversed(entries):
        try:
            msg = json.loads(e.get("message", "{}")).get("message", {})
            if msg.get("method") != "Network.responseReceived":
                continue
            resp = msg.get("params", {}).get("response", {})
            status = int(resp.get("status", 0))
            if status == 429:
                headers = {k.lower(): v for k, v in (resp.get("headers", {}) or {}).items()}
                ra = headers.get("retry-after")
                try:
                    return (int(ra) if ra else 10), resp.get("url", "")
                except Exception:
                    return 10, resp.get("url", "")
        except Exception:
            continue
    return None, None


def is_kickstarter_throttled_html(driver) -> bool:
    try:
        if re.search(r"\bThrottled\s*\(429\)\b", (driver.title or ""), flags=re.I):
            return True
    except Exception:
        pass
    try:
        for md in driver.find_elements(By.CSS_SELECTOR, "div.message"):
            t = (md.text or "").strip()
            if "You are sending too many requests to Kickstarter at this time." in t:
                return True
    except Exception:
        pass
    try:
        html = driver.page_source or ""
        for pat in THROTTLE_PATTERNS:
            if re.search(pat, html, flags=re.I):
                return True
    except Exception:
        pass
    return False


def open_or_rotate(driver, url, state, headless=False, version_main=140, max_nav_rotations=2):
    tries = 0
    while True:
        tries += 1
        try:
            safe_driver_get(driver, url)
            wait_for_page_load(driver, timeout=60)
        except Exception as e:
            print(f"[nav] error loading {url}: {e} -> rotate")
            driver = rotate_and_recycle(driver, state, headless=headless, version_main=version_main)
            if tries <= max_nav_rotations:
                continue
            return driver, False

        if not wait_cf_js_pass(driver, 10):
            print("[nav] persistent Cloudflare JS challenge -> rotate")
            driver = rotate_and_recycle(driver, state, headless=headless, version_main=version_main)
            if tries <= max_nav_rotations:
                continue
            return driver, False

        if is_human_verification_page(driver):
            print("[nav] CAPTCHA detected -> rotate")
            driver = rotate_and_recycle(driver, state, headless=headless, version_main=version_main)
            if tries <= max_nav_rotations:
                continue
            return driver, False

        if is_kickstarter_throttled_html(driver):
            print("[nav] HTML throttled (429) detected -> rotate")
            driver = rotate_and_recycle(driver, state, headless=headless, version_main=version_main)
            if tries <= max_nav_rotations:
                continue
            return driver, False

        ra, u429 = pop_last_graph_retry_after(driver)
        if ra:
            print(f"[nav] 429 on {u429}, backoff {ra}s -> rotate")
            time.sleep(min(ra, 12))  # cap the wait, proxy rotation below does the real work
            driver = rotate_and_recycle(driver, state, headless=headless, version_main=version_main)
            if tries <= max_nav_rotations:
                continue
            return driver, False

        close_cookie_modal_if_present(driver, timeout=4)
        return driver, True
