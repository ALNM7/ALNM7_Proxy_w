import os
import re
import json
import time
import random
import shutil
import socket
import glob
import requests
import pandas as pd
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from typing import List, Optional, Tuple

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    WebDriverException,
)
import unicodedata
import argparse
from datetime import datetime
import errno
from functools import wraps


def _is_win_socket_10055(exc: BaseException) -> bool:
    winerr = getattr(exc, "winerror", None)
    errnum = getattr(exc, "errno", None)
    return (winerr == 10055) or (errnum == errno.ENOBUFS)

def backoff_forever_on_10055(fn):
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


# default config

DATASET_START = 1
DATASET_END   = 82
WORKERS = 4

TARGET_ROWS: Optional[int] = None     #  --max-rows
START_INDEX: Optional[int] = None     # --from-index 

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


EMBEDDED_PROXIES = [
    {"host":"38.225.2.253","port":6036},
    {"host":"92.112.235.176","port":6699},
    {"host":"82.23.237.220","port":7554},
    {"host":"82.26.246.212","port":8036},
    {"host":"193.239.176.210","port":5616},
    {"host":"23.236.222.123","port":7154},
    {"host":"45.39.17.7","port":5430},
    {"host":"45.131.102.135","port":5787},
    {"host":"145.223.46.71","port":5621},
    {"host":"192.198.126.146","port":7189},
    {"host":"206.206.119.252","port":6163},
    {"host":"38.154.224.235","port":6776},
    {"host":"142.202.253.178","port":5853},
    {"host":"82.29.245.145","port":6969},
    {"host":"192.227.131.109","port":6693},
    {"host":"192.241.112.83","port":7585},
    {"host":"198.154.89.212","port":6303},
]


# auth for proxy server
PROXY_AUTH_MODE = "ip"     # "ip" or "userpass"
PROXY_USER = None
PROXY_PASS = None

# proxy rotation
PROXY_ROTATE_SECONDS = 3 * 60
BATCH_SIZE           = 15
REST_SECONDS         = 2 * 35

# main chrome
CHROME_VERSION_MAIN = 140
HEADLESS = False

PROFILE_DIR = None
proxy_mgr   = None
CURRENT_PROFILE_PATH: Optional[str] = None

# profile purge
DELETE_PREV_ON_REPLACE = True
PURGE_KEEP_LAST = 3
PURGE_MAX_AGE_HOURS = 8
PURGE_MAX_TOTAL_GB = 20.0

# row control
MAX_DRIVER_RECYCLE_PER_ROW = 3
MAX_NAV_ROTATIONS_PER_URL  = 2

# utils

_COLORS = ["\033[95m","\033[94m","\033[96m","\033[92m","\033[93m","\033[91m"]
_RESET = "\033[0m"

def colorize(worker_id: int, msg: str) -> str:
    try:
        c = _COLORS[worker_id % len(_COLORS)]
        return f"{c}[W{worker_id}] {msg}{_RESET}"
    except Exception:
        return f"[W{worker_id}] {msg}"

def jitter_short():
    time.sleep(0.25 + random.random()*0.35)

def wait_for_page_load(driver, timeout=60):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )

def scroll_page(driver, pause_time=4):
    last_h = driver.execute_script("return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)")
    y = 0
    while True:
        y += 1200
        driver.execute_script(f"window.scrollTo(0, {y});")
        time.sleep(pause_time/2)
        new_h = driver.execute_script("return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)")
        if y >= new_h:
            break
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(0.3)

def add_lang(url: str, lang="en"):
    try:
        parts = urlparse(url)
        q = dict(parse_qsl(parts.query))
        q["lang"] = lang
        return urlunparse((parts.scheme, parts.netloc, parts.path, parts.params, urlencode(q), parts.fragment))
    except Exception:
        return url

def looks_like_cf_js_challenge(html_lower: str) -> bool:
    return any(s in html_lower for s in [
        "just a moment", "checking your browser", "cf-chl-widget", "cf-challenge", "turnstile"
    ])

def wait_cf_js_pass(driver, max_wait=10):
    t0 = time.time()
    while time.time()-t0 < max_wait:
        if not looks_like_cf_js_challenge(driver.page_source.lower()):
            return True
        time.sleep(1.0)
    return False

def is_human_verification_page(driver):
    try:
        txt = driver.page_source.lower()
        if "verify you are human" in txt or "are you human" in txt:
            return True
        if driver.find_elements(By.XPATH, "//iframe[contains(@src,'captcha') or contains(@title,'captcha') or contains(@src,'turnstile')]"):
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
    import json as _json
    try:
        entries = driver.get_log("performance")
    except Exception:
        return None, None
    for e in reversed(entries):
        try:
            msg = _json.loads(e.get("message","{}")).get("message",{})
            if msg.get("method") != "Network.responseReceived":
                continue
            resp = msg.get("params",{}).get("response",{})
            status = int(resp.get("status",0))
            if status == 429:
                headers = {k.lower(): v for k,v in (resp.get("headers",{}) or {}).items()}
                ra = headers.get("retry-after")
                try:
                    return (int(ra) if ra else 10), resp.get("url","")
                except Exception:
                    return 10, resp.get("url","")
        except Exception:
            continue
    return None, None

THROTTLE_PATTERNS = [
    r"\bThrottled\s*\(429\)\b",
    r"You are sending too many requests to Kickstarter at this time\.",
    r"/ref=429errorpage",
]

def is_kickstarter_throttled_html(driver) -> bool:
    import re as _re
    try:
        if _re.search(r"\bThrottled\s*\(429\)\b", (driver.title or ""), flags=_re.I):
            return True
    except Exception:
        pass
    try:
        msg_divs = driver.find_elements(By.CSS_SELECTOR, "div.message")
        for md in msg_divs:
            t = (md.text or "").strip()
            if "You are sending too many requests to Kickstarter at this time." in t:
                return True
    except Exception:
        pass
    try:
        html = driver.page_source or ""
        for pat in THROTTLE_PATTERNS:
            if _re.search(pat, html, flags=_re.I):
                return True
    except Exception:
        pass
    return False

def _proxy_url_noauth(h, p):
    return f"http://{h}:{int(p)}"

def _proxy_url_userpass(h, p, u, pw):
    return f"http://{u}:{pw}@{h}:{int(p)}"

class ProxyManager:
    def __init__(self, proxy_list, user, password):
        self.all_proxies = proxy_list[:]
        self.proxies = proxy_list[:]
        random.shuffle(self.proxies)
        self.user = user
        self.password = password
        self.index = -1
        self.current = None
        self.last_rotate = 0.0

    def ensure_available(self):
        if not self.proxies:
            self.proxies = self.all_proxies[:]
            random.shuffle(self.proxies)
            self.index = -1
            print("[proxy] Cola repuesta (rotación continua).")

    def get_current(self):
        if self.current is None:
            return self.rotate(force=True)
        return self.current

    def rotate(self, force=False):
        self.ensure_available()
        self.index = (self.index + 1) % len(self.proxies)
        entry = self.proxies[self.index]
        h, p = entry["host"], int(entry["port"])
        if PROXY_AUTH_MODE == "ip":
            url = _proxy_url_noauth(h, p)
        else:
            url = _proxy_url_userpass(h, p, self.user, self.password)
        self.current = {"host": h, "port": p, "url": url, "ts": time.time()}
        self.last_rotate = time.time()
        print(f"[proxy] Rotated to {h}:{p} (idx {self.index})")
        warm_proxy_auth_exact(self.current)
        return self.current

    def rotate_if_stale(self, seconds):
        if time.time() - self.last_rotate > seconds:
            return self.rotate(force=True)
        return self.current

def build_requests_session_for_proxy(proxy_info):
    s = requests.Session()
    proxy_url = proxy_info["url"]
    s.proxies = {"http": proxy_url, "https": proxy_url}
    s.verify = True
    s.headers.update(HEADERS)
    return s

def warm_proxy_auth_exact(proxy_info):
    url = "https://ipv4.webshare.io/"
    proxy_url = proxy_info["url"]
    try:
        r = requests.get(url, proxies={"http": proxy_url, "https": proxy_url}, timeout=12)
        print(f"[proxy] warmup {proxy_info['host']}:{proxy_info['port']} -> {r.status_code}")
    except Exception as e:
        print(f"[proxy] warmup error {proxy_info['host']}:{proxy_info['port']}: {e}")

def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

def _make_profile_dir(base_dir):
    os.makedirs(base_dir, exist_ok=True)
    default_dir = os.path.join(base_dir, "Default")
    os.makedirs(default_dir, exist_ok=True)
    prefs = os.path.join(default_dir, "Preferences")
    if not os.path.exists(prefs):
        with open(prefs, "w", encoding="utf-8") as f:
            f.write("{}\n")

def build_driver(proxy_info=None, profile_dir=None, attempt=1, headless=False, version_main=None):
    VERSION_MAIN = version_main or CHROME_VERSION_MAIN
    UA = HEADERS["User-Agent"]
    if profile_dir is None:
        raise RuntimeError("profile_dir is required")
    _make_profile_dir(profile_dir)
    opts = uc.ChromeOptions()
    opts.add_argument(f"--user-data-dir={os.path.abspath(profile_dir)}")
    opts.add_argument("--profile-directory=Default")
    opts.add_argument("--lang=en-US,en")
    opts.add_argument("--disable-popup-blocking")
    opts.add_argument("--disable-features=AutomationControlled")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-software-rasterizer")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--no-first-run")
    opts.add_argument("--disable-sync")
    opts.add_argument("--disable-features=ChromeWhatsNewUI,SignInProfileCreation,AccountConsistency")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument(f"--user-agent={UA}")
    if headless:
        opts.add_argument("--headless=new")
    free_port = _free_port()
    opts.add_argument(f"--remote-debugging-port={free_port}")
    opts.add_argument("--aggressive-cache-discard")
    opts.add_argument("--disable-application-cache")
    opts.add_argument("--disk-cache-size=10485760")
    opts.add_argument("--media-cache-size=1048576")
    cache_dir = os.path.abspath(os.path.join(profile_dir, "..", "cache"))
    os.makedirs(cache_dir, exist_ok=True)
    opts.add_argument(f"--disk-cache-dir={cache_dir}")
    if proxy_info:
        opts.add_argument(f"--proxy-server=http://{proxy_info['host']}:{int(proxy_info['port'])}")
        print(f"[driver] Chrome via proxy {proxy_info['host']}:{proxy_info['port']} (debug {free_port})")
    prefs = {
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
        "signin_promo_enabled": False
    }
    opts.add_experimental_option("prefs", prefs)
    from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
    caps = DesiredCapabilities.CHROME.copy()
    caps["goog:loggingPrefs"] = {"performance": "ALL"}
    try:
        driver = uc.Chrome(
            options=opts,
            headless=False,  # uc uses headless on the flags; keep False here
            use_subprocess=True,
            desired_capabilities=caps,
            version_main=VERSION_MAIN,
        )
    except (PermissionError, FileExistsError, WebDriverException) as e:
        if attempt < 3:
            print(f"[driver] error creating driver (attempt {attempt}/3): {e}")
            try:
                time.sleep(0.8)
                suffix = f"_r{random.randint(1000,9999)}"
                new_dir = profile_dir + suffix
                shutil.copytree(profile_dir, new_dir, dirs_exist_ok=True)
                return build_driver(proxy_info=proxy_info, profile_dir=new_dir, attempt=attempt+1, headless=headless, version_main=VERSION_MAIN)
            except Exception as e2:
                print(f"[driver] profile clone failed: {e2}")
                time.sleep(0.8)
                return build_driver(proxy_info=proxy_info, profile_dir=profile_dir, attempt=attempt+1, headless=headless, version_main=VERSION_MAIN)
        raise
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"}
        )
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setBlockedURLs", {
            "urls": [
                "*://ev.kck.st/*",
                "*apple-pay*",
                "*project-page-recommendations*",
                "*/web/track*",
            ]
        })
    except Exception:
        pass
    print(f"[driver] Chrome iniciado (perfil {profile_dir}) puerto debug {free_port} headless={headless}")
    return driver

def build_driver_with_retry(proxy_info=None, base_profile_dir=None, headless=False, version_main=None):
    if base_profile_dir is None:
        raise RuntimeError("base_profile_dir required")
    slug = f"{int(time.time())}_{random.randint(1000,9999)}"
    prof = os.path.abspath(base_profile_dir + "_" + slug)
    os.makedirs(base_profile_dir, exist_ok=True)
    drv = build_driver(proxy_info=proxy_info, profile_dir=prof, headless=headless, version_main=version_main)
    return drv, prof

def safe_quit(driver):
    try:
        if driver:
            driver.quit()
    except Exception:
        pass

def create_driver_for_proxy(proxy, headless=False, version_main=None):
    global CURRENT_PROFILE_PATH
    driver, prof = build_driver_with_retry(proxy_info=proxy, base_profile_dir=PROFILE_DIR, headless=headless, version_main=version_main)
    CURRENT_PROFILE_PATH = prof
    return driver

def recycle_driver_with_proxy(proxy, headless=False, version_main=None):
    global CURRENT_PROFILE_PATH
    old_prof = CURRENT_PROFILE_PATH
    driver, prof = build_driver_with_retry(proxy_info=proxy, base_profile_dir=PROFILE_DIR, headless=headless, version_main=version_main)
    if DELETE_PREV_ON_REPLACE and old_prof and os.path.isdir(old_prof) and old_prof != prof:
        try:
            shutil.rmtree(old_prof, ignore_errors=True)
            print(f"[cleanup] removed old profile: {old_prof}")
        except Exception as e:
            print(f"[cleanup] failed {old_prof}: {e}")
    CURRENT_PROFILE_PATH = prof
    return driver

def get_ip_via_requests(session):
    try:
        r = safe_request(session, "GET", "https://api.ipify.org?format=text", timeout=12)
        return r.text.strip()
    except Exception:
        return None

def get_ip_via_chrome(driver):
    try:
        safe_driver_get(driver, "https://api.ipify.org?format=text")
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "pre")))
    except Exception:
        pass
    try:
        txt = driver.find_element(By.TAG_NAME, "body").text.strip()
        return txt
    except Exception:
        return None

def verify_proxy_is_active(driver, requests_session, desc=""):
    ip_req = get_ip_via_requests(requests_session)
    ip_ch  = get_ip_via_chrome(driver)
    print(f"[verify] {desc} requests_ip={ip_req} | chrome_ip={ip_ch}")
    return ip_req, ip_ch

def _dir_size_bytes(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except:
                pass
    return total

def purge_old_profiles(base_prefix: str, keep_last:int=3, max_age_hours:int=12, max_total_gb:float=30.0):
    pattern = base_prefix + "_*"
    candidates = []
    now = time.time()
    for d in glob.glob(pattern):
        try:
            st = os.stat(d)
            age_h = (now - st.st_mtime) / 3600.0
            candidates.append((d, st.st_mtime, age_h))
        except Exception:
            continue
    candidates.sort(key=lambda x: x[1], reverse=True)
    survivors = candidates[:keep_last]  # noqa: F841
    oldies    = [c for c in candidates[keep_last:] if c[2] > max_age_hours]
    for d, _, _ in oldies:
        print(f"[purge] removing old profile: {d}")
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception as e:
            print(f"[purge] failed {d}: {e}")
    def total_gb():
        s = 0
        for d, _, _ in candidates:
            if os.path.exists(d):
                s += _dir_size_bytes(d)
        return s / (1024**3)
    extra = sorted([c for c in candidates[keep_last:] if os.path.exists(c[0])], key=lambda x: x[1])
    while total_gb() > max_total_gb and extra:
        d, _, _ = extra.pop(0)
        print(f"[purge] removing to fit cap: {d}")
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception as e:
            print(f"[purge] failed {d}: {e}")

def open_or_rotate(driver, url, retry_once=True, headless=False, version_main=None):
    tries = 0
    while True:
        tries += 1
        try:
            safe_driver_get(driver, url)  # backoff 2s loop
            wait_for_page_load(driver, timeout=60)
        except Exception as e:
            print(f"[nav] error loading {url}: {e} -> rotate")
            safe_quit(driver)
            new_proxy = proxy_mgr.rotate(force=True)
            driver = recycle_driver_with_proxy(new_proxy, headless=headless, version_main=version_main)
            if tries <= MAX_NAV_ROTATIONS_PER_URL:
                continue
            return driver, False

        if not wait_cf_js_pass(driver, 10):
            print("[nav] CF JS challenge persistente -> rotate")
            safe_quit(driver)
            driver = recycle_driver_with_proxy(proxy_mgr.rotate(True), headless=headless, version_main=version_main)
            if tries <= MAX_NAV_ROTATIONS_PER_URL:
                continue
            return driver, False

        if is_human_verification_page(driver):
            print("[nav] CAPTCHA detected -> rotate")
            safe_quit(driver)
            driver = recycle_driver_with_proxy(proxy_mgr.rotate(True), headless=headless, version_main=version_main)
            if tries <= MAX_NAV_ROTATIONS_PER_URL:
                continue
            return driver, False

        if is_kickstarter_throttled_html(driver):
            print("[nav] HTML Throttled (429) detected -> rotate")
            safe_quit(driver)
            driver = recycle_driver_with_proxy(proxy_mgr.rotate(True), headless=headless, version_main=version_main)
            if tries <= MAX_NAV_ROTATIONS_PER_URL:
                continue
            return driver, False

        ra, u429 = pop_last_graph_retry_after(driver)
        if ra:
            print(f"[nav] 429 on {u429}. backoff {ra}s -> rotate")
            time.sleep(min(ra, 12))
            safe_quit(driver)
            driver = recycle_driver_with_proxy(proxy_mgr.rotate(True), headless=headless, version_main=version_main)
            if tries <= MAX_NAV_ROTATIONS_PER_URL:
                continue
            return driver, False

        close_cookie_modal_if_present(driver, timeout=4)
        return driver, True

# scrappers 

def click_all_see_more(driver, scope):
    texts = ["see more","show more","read more","+ more","ver más","mostrar más","cargar más"]
    try:
        buttons = scope.find_elements(By.XPATH, ".//button|.//a")
        for b in buttons:
            label = (b.text or b.get_attribute("aria-label") or "").strip().lower()
            if any(t in label for t in texts):
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", b)
                    jitter_short()
                    try:
                        b.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", b)
                    time.sleep(0.2 + random.random()*0.2)
                except Exception:
                    continue
    except Exception:
        pass

def extract_campaign_text_and_media_counts(driver):
    def normtxt(s):
        if not s:
            return ""
        s = s.replace("\u00a0", " ")
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        s = re.sub(r"\n{3,}", "\n\n", s)
        s = "\n".join([ln.strip() for ln in s.split("\n")])
        return unicodedata.normalize("NFC", s)

    story_scope = None
    try:
        story_scope = driver.find_element(By.CSS_SELECTOR, "div.col.col-12.grid-col-9-lg.z1")
    except Exception:
        story_scope = None

    if story_scope is None:
        try:
            story_scope = driver.find_element(By.CSS_SELECTOR, ".px3")
        except Exception:
            story_scope = None

    if story_scope is None:
        for sel in [".story-content", ".story-content .rte__content", "body"]:
            try:
                story_scope = driver.find_element(By.CSS_SELECTOR, sel)
                if story_scope:
                    break
            except Exception:
                continue
    if story_scope is None:
        story_scope = driver.find_element(By.TAG_NAME, "body")

    try:
        click_all_see_more(driver, story_scope)
    except Exception:
        pass

    story_text = ""
    try:
        story_text = driver.execute_script("return arguments[0].innerText || '';", story_scope)
    except Exception:
        story_text = ""
    story_text = normtxt(story_text)

    if not story_text:
        parts = []
        xpaths = [
            ".//h1", ".//h2", ".//h3", ".//h4", ".//h5", ".//h6",
            ".//p", ".//*[contains(@class,'ace-line')]",
            ".//li", ".//figcaption", ".//blockquote", ".//pre"
        ]
        for xp in xpaths:
            try:
                for el in story_scope.find_elements(By.XPATH, xp):
                    t = normtxt(el.text or "")
                    if not t:
                        continue
                    tag = el.tag_name.lower().strip()
                    if tag in {"h1","h2","h3","h4","h5","h6"}:
                        parts.append("\n\n" + t)
                    elif tag == "li":
                        parts.append("• " + t)
                    else:
                        parts.append(t)
            except Exception:
                pass
        story_text = normtxt("\n".join([p for p in parts if p]).strip())

    image_count = 0
    video_count = 0
    try:
        figs = story_scope.find_elements(By.CSS_SELECTOR, "figure.image")
        image_count = len([f for f in figs if f.is_displayed()])
    except Exception:
        image_count = 0

    try:
        native_videos = story_scope.find_elements(By.TAG_NAME, "video")
        iframes = story_scope.find_elements(By.TAG_NAME, "iframe")
        iframe_like_videos = 0
        for fr in iframes:
            try:
                src = (fr.get_attribute("src") or "").lower()
            except Exception:
                src = ""
            if any(k in src for k in ["youtube.com", "youtu.be", "vimeo.com", "player.vimeo.com", "embedly", "wistia", "kck.st/video"]):
                iframe_like_videos += 1
        embed_divs = story_scope.find_elements(By.CSS_SELECTOR, "div.text-center.clip.mb5, .video, .embedded-video")
        video_count = len([v for v in native_videos if v.is_displayed()]) + iframe_like_videos + len([e for e in embed_divs if e.is_displayed()])
    except Exception:
        video_count = 0

    return story_text, image_count, video_count

def crawl_creator_from_creator_page(driver):
    result = {
        "creator_name": "",
        "projects_backed_text": "",
        "number_of_projects": "",
        "creator_account_created_date": "",
        "creator_description": "",
    }
    def norm(s: str) -> str:
        return (s or "").strip()
    def lower_no_ws(s: str) -> str:
        return norm(s).lower()
    cont = None
    try:
        cont = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".grid-col-12.grid-col-8-md"))
        )
    except Exception as e:
        print("Creator main container not found:", e)
        return result
    try:
        blk = cont.find_element(By.CSS_SELECTOR, ".kds-flex.kds-items-center.kds-gap-05.kds-mb-06")
        col = blk.find_element(By.CSS_SELECTOR, ".kds-flex.kds-flex-col.kds-gap-02")
        h3 = col.find_element(By.TAG_NAME, "h3")
        name = norm(h3.text)
        if name:
            result["creator_name"] = name
    except Exception:
        pass
    wrapper = None
    try:
        wrapper = cont.find_element(
            By.XPATH,
            ".//*[contains(@class,'kds-flex') and contains(@class,'kds-flex-row') and contains(@class,'kds-flex-wrap') and contains(@class,'kds-gap-04') and contains(@class,'md:kds-gap-16')]"
        )
    except Exception:
        pass
    if wrapper:
        try:
            inner = wrapper.find_element(
                By.XPATH,
                ".//*[contains(@class,'kds-flex') and contains(@class,'kds-flex-col-reverse') and contains(@class,'basis-auto-md') and contains(@class,'kds-mb-03') and contains(@class,'md:kds-mb-0') and contains(@class,'basis100p') and contains(@class,'do-not-visually-track')]"
            )
            a_links = inner.find_elements(By.XPATH, ".//a[contains(@class,'kds-text-primary')]")
            def a_text(ael, prefer_lg=False):
                try:
                    if prefer_lg:
                        sp = ael.find_element(By.XPATH, ".//span[contains(@class,'kds-type-heading-lg')]")
                        return norm(sp.text)
                except Exception:
                    pass
                try:
                    sp = ael.find_element(By.XPATH, ".//span[contains(@class,'kds-type-heading-sm')]")
                    return norm(sp.text)
                except Exception:
                    pass
                return norm(ael.text)
            if len(a_links) >= 1:
                result["projects_backed_text"] = a_text(a_links[0], prefer_lg=False)
            if len(a_links) >= 2:
                result["number_of_projects"] = a_text(a_links[1], prefer_lg=True)
        except Exception:
            pass
        try:
            blocks = wrapper.find_elements(
                By.XPATH,
                ".//div[contains(@class,'kds-flex') and contains(@class,'kds-flex-col-reverse') and contains(@class,'do-not-visually-track')]"
            )
            for b in blocks:
                try:
                    label_sm = b.find_element(By.XPATH, ".//span[contains(@class,'kds-type-heading-sm')]")
                    lbl = lower_no_ws(label_sm.text)
                except Exception:
                    continue
                if any(k in lbl for k in ["cuenta creada", "account created", "member since", "joined", "miembro desde"]):
                    try:
                        val_lg = b.find_element(By.XPATH, ".//span[contains(@class,'kds-type-heading-lg')]")
                        result["creator_account_created_date"] = norm(val_lg.text)
                        break
                    except Exception:
                        pass
        except Exception:
            pass
    try:
        bio_el = driver.find_element(
            By.XPATH,
            "//*[contains(@class,'text-preline') and contains(@class,'do-not-visually-track') and contains(@class,'kds-type') and contains(@class,'kds-type-body-md')]"
        )
        bio_text = norm(bio_el.text)
        if bio_text:
            result["creator_description"] = bio_text
    except Exception:
        pass
    return result

def _find_rewards_sidebar(driver):
    try:
        return driver.find_element(By.ID, "react-rewards-sidebar")
    except Exception:
        pass
    for sel in ["[data-test-id='rewards-sidebar']", "[role='complementary']"]:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            if el:
                return el
        except Exception:
            continue
    return None

def _click_nav_tab(driver, tab_text="Rewards"):
    try:
        navs = driver.find_elements(By.XPATH, "//nav|//header")
        for nav in navs:
            btns = nav.find_elements(By.XPATH, ".//a|.//button")
            for b in btns:
                t = (b.text or b.get_attribute("outerText") or "").strip().lower()
                if tab_text.lower() in t:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", b)
                    jitter_short()
                    try:
                        b.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", b)
                    time.sleep(0.8)
                    return True
    except Exception:
        pass
    return False

def _parse_rewards_from(scope):
    rewards_data = []
    cards = scope.find_elements(By.TAG_NAME, "article")
    for idx, card in enumerate(cards, 1):
        try:
            name = ""
            for xp in [".//header//*[self::h3 or self::h2 or self::h4]", ".//*[@data-test-id='reward-title']"]:
                els = card.find_elements(By.XPATH, xp)
                if els:
                    name = (els[0].text or "").strip()
                    if name:
                        break
            if not name:
                name = f"Reward #{idx}"
            try:
                price = card.find_element(By.XPATH, ".//header//p|.//*[@data-test-id='reward-amount']").text.strip()
            except Exception:
                price = ""
            desc = ""
            try:
                blk = card.find_element(By.XPATH, ".//*[contains(@class,'block') and contains(@class,'z1') or contains(@class,'content') or @data-test-id='reward-description']")
                desc = (blk.text or "").strip()
            except Exception:
                ps = card.find_elements(By.TAG_NAME, "p")
                desc = "\n".join((p.text or "").strip() for p in ps if (p.text or "").strip())
            try:
                ships_to = card.find_element(By.XPATH, ".//div[h3[contains(.,'Ships to')]]/div").text.strip()
            except Exception:
                ships_to = ""
            try:
                est = card.find_element(By.XPATH, ".//div[h3[contains(.,'Estimated delivery')]]//time").text.strip()
            except Exception:
                est = ""
            try:
                limited = card.find_element(By.XPATH, ".//div[h3[contains(.,'Limited quantity')]]/div").text.strip()
            except Exception:
                limited = ""
            rewards_data.append({
                "name": name,
                "price": price,
                "description": desc,
                "ships_to": ships_to,
                "estimated_delivery": est,
                "limited_quantity": limited,
            })
        except Exception:
            continue
    return rewards_data

def crawl_rewards(driver, project_url=None):
    sidebar = _find_rewards_sidebar(driver)
    if sidebar:
        click_all_see_more(driver, sidebar)
        data = _parse_rewards_from(sidebar)
        if data:
            return data
    if project_url:
        driver, ok = open_or_rotate(driver, project_url)
        if not ok:
            return []
        sidebar = _find_rewards_sidebar(driver)
        if sidebar:
            click_all_see_more(driver, sidebar)
            data = _parse_rewards_from(sidebar)
            if data:
                return data
        if _click_nav_tab(driver, "Rewards"):
            time.sleep(0.8)
            try:
                main_rewards = driver.find_element(By.XPATH, "//main|//div[@role='main']|//section[contains(@aria-label,'Rewards')]")
                click_all_see_more(driver, main_rewards)
                data = _parse_rewards_from(main_rewards)
                if data:
                    return data
            except Exception:
                pass
    return []

def load_more_loop_with_budget(driver, scope, max_clicks=80, settle_timeout=7):
    def visible_items():
        its = scope.find_elements(By.XPATH, ".//article|.//li|.//*[@role='listitem']")
        return [i for i in its if i.is_displayed()]
    clicks = 0
    while clicks < max_clicks:
        ra, _u = pop_last_graph_retry_after(driver)
        if ra:
            print(f"[network] 429 durante load-more, backoff {ra}s and rotate.")
            return False
        before = len(visible_items())
        candidates = []
        for xp in [".//button[normalize-space()]", ".//*[@role='button']"]:
            candidates += scope.find_elements(By.XPATH, xp)
        def match_text(el):
            try:
                t = (el.text or el.get_attribute("aria-label") or el.get_attribute("outerText") or "").strip().lower()
            except Exception:
                t = ""
            keys = ["load more", "show more", "cargar más", "mostrar más", "ver más"]
            return any(k in t for k in keys)
        btns = [b for b in candidates if b.is_displayed() and match_text(b)]
        if not btns:
            print("No more Load/Show buttons.")
            break
        btn = btns[0]
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        jitter_short()
        try:
            btn.click()
        except Exception:
            driver.execute_script("arguments[0].click();", btn)
        clicks += 1
        print(f"Load-more click #{clicks}")
        try:
            WebDriverWait(driver, settle_timeout).until(
                lambda d: len(visible_items()) > before or pop_last_graph_retry_after(driver)[0] is not None
            )
        except TimeoutException:
            after = len(visible_items())
            if after <= before:
                print("No new items after click. Assuming done.")
                break
        ra2, _u2 = pop_last_graph_retry_after(driver)
        if ra2:
            print(f"[network] 429 detected after click, backoff {ra2}s and rotate.")
            return False
        jitter_short()
    return True

def extract_updates(driver):
    try:
        try:
            project_post_interface = driver.find_element(By.ID, "project-post-interface")
            updates = project_post_interface.find_elements(By.CLASS_NAME, "truncated-post")
        except NoSuchElementException:
            updates = driver.find_elements(By.CSS_SELECTOR, "[data-test-id='post'], article.truncated-post")
            if not updates:
                updates = driver.find_elements(By.XPATH, "//article[.//h2 or .//h3]")
        update_count_result = None
        try:
            update_count_wrapper = driver.find_element(By.ID, 'updates-emoji')
            update_count_result = update_count_wrapper.find_element(By.CLASS_NAME, 'count').text.strip()
        except Exception:
            update_count_result = str(len(updates))
        update_data_result = {}
        for i, update in enumerate(updates, 1):
            try:
                try:
                    update_number_element = update.find_element(By.XPATH, ".//span[contains(text(), 'Update #')]")
                    update_number = update_number_element.text.strip()
                except NoSuchElementException:
                    update_number = f"Update #{i}"
                title = ""
                for tag in ("h2", "h3"):
                    try:
                        title = update.find_element(By.TAG_NAME, tag).text.strip()
                        if title:
                            break
                    except NoSuchElementException:
                        continue
                content = ""
                try:
                    rte_content = update.find_element(By.CLASS_NAME, "rte__content")
                    content = rte_content.text.strip()
                except NoSuchElementException:
                    ps = update.find_elements(By.TAG_NAME, "p")
                    content = "\n".join(p.text.strip() for p in ps if p.text.strip()) or "No content found."
                update_data_result[update_number] = {"title": title, "content": content}
            except NoSuchElementException:
                print("Skipping update due to missing elements.")
        return update_count_result, update_data_result
    except Exception as e:
        print(f"Error extracting updates: {e}")
        return "0", {}

def extract_community_data(driver):
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CLASS_NAME, "community-section__hero"))
        )
        backers_text = driver.find_element(By.CSS_SELECTOR, ".community-section__hero .title").text.strip()
        m = re.search(r"\d[\d,\.]*", backers_text)
        backers_count = m.group(0) if m else "0"
        city_data = []
        for item in driver.find_elements(By.CSS_SELECTOR, ".community-section__locations_cities .location-list__item"):
            try:
                city = item.find_element(By.CSS_SELECTOR, ".primary-text").text.strip()
                country = item.find_element(By.CSS_SELECTOR, ".secondary-text").text.strip()
                backers = item.find_element(By.CSS_SELECTOR, ".tertiary-text").text.strip()
                city_data.append({f"{city}, {country}": backers})
            except Exception:
                continue
        country_data = []
        for item in driver.find_elements(By.CSS_SELECTOR, ".community-section__locations_countries .location-list__item"):
            try:
                country = item.find_element(By.CSS_SELECTOR, ".primary-text").text.strip()
                backers = item.find_element(By.CSS_SELECTOR, ".tertiary-text").text.strip()
                country_data.append({country: backers})
            except Exception:
                continue
        new_backers = 0
        returning_backers = 0
        try:
            new_backers = int(driver.find_element(By.CSS_SELECTOR, ".new-backers .count").text.strip())
        except Exception:
            pass
        try:
            returning_backers = int(driver.find_element(By.CSS_SELECTOR, ".existing-backers .count").text.strip())
        except Exception:
            pass
        data = {
            "backers_count": backers_count,
            "where_backers_come_from_cities": city_data,
            "where_backers_come_from_countries": country_data,
            "new_backers": new_backers,
            "returning_backers": returning_backers
        }
        return data
    except Exception as e:
        print(f"Error extracting community data: {e}")
        return {}

def crawl_faqs(driver):
    faqs = []
    try:
        ul = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "ul.faqs.grid-col-8-sm"))
        )
    except TimeoutException:
        print("FAQs list not found.")
        return faqs
    items = ul.find_elements(By.XPATH, "./li[contains(@class,'js-faq')]")
    print(f"Found {len(items)} faq items.")
    for li in items:
        try:
            question = ""
            try:
                a_toggle = li.find_element(By.XPATH, ".//a[contains(@class,'js-faq-question-toggle')]")
                try:
                    span_q = a_toggle.find_element(By.XPATH, ".//span[contains(@class,'type-14') and contains(@class,'navy-700') and contains(@class,'medium')]")
                    question = (span_q.text or "").strip()
                except Exception:
                    question = (a_toggle.text or "").strip()
            except Exception:
                try:
                    span_q = li.find_element(By.XPATH, ".//span[contains(@class,'type-14') and contains(@class,'navy-700') and contains(@class,'medium')]")
                    question = (span_q.text or "").strip()
                except Exception:
                    question = ""
            def find_answer_text():
                try:
                    ans_div = li.find_element(By.XPATH, ".//div[contains(@class,'js-faq-answer')]")
                    inner = ans_div.find_element(By.XPATH, ".//*[contains(@class,'type-14') and contains(@class,'navy-700') and contains(@class,'normal')]")
                    ps = inner.find_elements(By.TAG_NAME, "p")
                    if ps:
                        return "\n\n".join([(p.text or "").strip() for p in ps if (p.text or "").strip()])
                    return (inner.text or "").strip()
                except Exception:
                    return ""
            answer = find_answer_text()
            if not answer:
                try:
                    a_toggle = li.find_element(By.XPATH, ".//a[contains(@class,'js-faq-question-toggle')]")
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", a_toggle)
                    jitter_short()
                    try:
                        a_toggle.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", a_toggle)
                    time.sleep(0.35)
                    answer = find_answer_text()
                except Exception:
                    pass
            if question or answer:
                faqs.append({"question": question, "answer": answer})
        except Exception as e:
            print("Error parsing one FAQ:", e)
            continue
    return faqs

def load_all_comments(driver, max_clicks=120):
    try:
        container = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.ID, "react-project-comments"))
        )
    except TimeoutException:
        print("No comments container found.")
        return True
    ok = load_more_loop_with_budget(driver, container, max_clicks=max_clicks, settle_timeout=7)
    return ok

def extract_comments_data(driver):
    container = WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.ID, "react-project-comments"))
    )
    try:
        root = container.find_element(By.TAG_NAME, "ul")
        items = root.find_elements(By.XPATH, "./li")
    except Exception:
        items = container.find_elements(By.XPATH, ".//*[@role='listitem']")
    items = [i for i in items if i.is_displayed()]
    comments_count = len(items)
    print(f"Found {comments_count} comments.")
    comments_data = []
    for item in items:
        try:
            username = ""
            for xp in [
                ".//span[contains(@class,'do-not-visually-track')]",
                ".//a[contains(@href,'/profile/')]",
                ".//span[@data-test-id='author-name']",
                ".//strong",
            ]:
                els = item.find_elements(By.XPATH, xp)
                if els and els[0].text.strip():
                    username = els[0].text.strip()
                    break
            try:
                t_el = item.find_element(By.TAG_NAME, "time")
                datetime_val = t_el.get_attribute("title") or t_el.get_attribute("datetime") or t_el.text.strip()
                if not datetime_val:
                    datetime_val = "Unknown"
            except Exception:
                datetime_val = "Unknown"
            content = ""
            for xp in [
                ".//p[contains(@class,'data-comment-text')]",
                ".//div[contains(@class,'rte__content')]//p",
                ".//p",
            ]:
                ps = item.find_elements(By.XPATH, xp)
                if ps:
                    content = "\n".join([p.text.strip() for p in ps if p.text.strip()])
                    if content:
                        break
            replies = []
            is_creator_reply = False
            for reply in item.find_elements(By.XPATH, ".//ul//li|.//*[@role='listitem']"):
                if not reply.is_displayed():
                    continue
                try:
                    r_user = ""
                    for rxp in [
                        ".//span[contains(@class,'do-not-visually-track')]",
                        ".//a[contains(@href,'/profile/')]",
                        ".//span[@data-test-id='author-name']",
                        ".//strong",
                    ]:
                        els = reply.find_elements(By.XPATH, rxp)
                        if els and els[0].text.strip():
                            r_user = els[0].text.strip()
                            break
                    try:
                        t_el = reply.find_element(By.TAG_NAME, "time")
                        r_time = t_el.get_attribute("title") or t_el.get_attribute("datetime") or t_el.text.strip()
                    except Exception:
                        r_time = "Unknown"
                    r_text_nodes = reply.find_elements(By.XPATH, ".//p[contains(@class,'data-comment-text')]|.//p")
                    r_text = "\n".join([p.text.strip() for p in r_text_nodes if p.text.strip()])
                    if reply.find_elements(By.XPATH, './/span[contains(text(), "Creator")]'):
                        is_creator_reply = True
                    replies.append({"username": r_user or "Unknown", "datetime": r_time, "content": r_text})
                except Exception:
                    continue
            comments_data.append({
                "username": username or "Unknown",
                "datetime": datetime_val,
                "content": content,
                "replies": replies,
                "replies_count": len(replies),
                "is_creator_reply": is_creator_reply,
            })
        except Exception as e:
            print(f"Error extracting comment: {e}")
    return {"comments_count": comments_count, "comments": comments_data}

def read_csv_smart(path):
    last_err = None
    for enc in ["utf-8","utf-8-sig","cp1252","latin1"]:
        try:
            print(f"Trying encoding: {enc}")
            return pd.read_csv(path, encoding=enc)
        except Exception as e:
            last_err = e
    raise last_err

def split_proxies_for_worker(all_proxies, worker_id, total_workers):
    return [p for i, p in enumerate(all_proxies) if (i % total_workers) == worker_id]

def assign_datasets(dataset_start: int, dataset_end: int, worker_id: int, workers: int) -> List[int]:
    return [ds for ds in range(dataset_start, dataset_end + 1) if (ds % workers) == worker_id]

# proxy load

def load_proxies_dynamic() -> List[dict]:
    env_path = os.environ.get("WEB_PROXIES_JSON")
    paths = [env_path] if env_path else []
    paths.append("proxies.json")
    for p in paths:
        if not p:
            continue
        try:
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list) and all(("host" in x and "port" in x) for x in data):
                    print(f"[proxy] proxies cargados desde {p}: {len(data)}")
                    return data
        except Exception as e:
            print(f"[proxy] no se pudo leer {p}: {e}")
    print("[proxy] usando proxies embebidos (fallback)")
    return EMBEDDED_PROXIES[:]

# workflow loop

def worker_loop(worker_id: int, assigned_datasets: List[int], args):
    global PROFILE_DIR, proxy_mgr, WORKERS, CURRENT_PROFILE_PATH
    PROFILE_DIR = f'./ks_profile_en_w{worker_id}'
    os.makedirs(PROFILE_DIR, exist_ok=True)
    purge_old_profiles(PROFILE_DIR, keep_last=args.purge_keep,
                       max_age_hours=args.purge_age_hours,
                       max_total_gb=args.purge_cap_gb)

    # global config
    global PROXY_AUTH_MODE, PROXY_USER, PROXY_PASS
    PROXY_AUTH_MODE = args.proxy_auth_mode
    PROXY_USER = args.proxy_user
    PROXY_PASS = args.proxy_pass

    global PROXY_ROTATE_SECONDS, BATCH_SIZE, REST_SECONDS, CHROME_VERSION_MAIN, HEADLESS
    PROXY_ROTATE_SECONDS = args.rotate_seconds
    BATCH_SIZE = args.batch_size
    REST_SECONDS = args.rest_seconds
    CHROME_VERSION_MAIN = args.chrome_version
    HEADLESS = args.headless

    driver = None
    requests_session = None
    try:
        all_proxies = load_proxies_dynamic()
        my_proxies = split_proxies_for_worker(all_proxies, worker_id, WORKERS)
        if not my_proxies:
            print(colorize(worker_id, "No hay proxies asignados. Saliendo."))
            return
        proxy_mgr = ProxyManager(my_proxies, PROXY_USER, PROXY_PASS)
        print(colorize(worker_id, f"Proxies asignados: {len(my_proxies)}"))

        current_proxy = proxy_mgr.get_current()
        requests_session = build_requests_session_for_proxy(current_proxy)   # only 1 sesion per proxy
        session_proxy_key = (current_proxy["host"], current_proxy["port"])

        driver = create_driver_for_proxy(current_proxy, headless=HEADLESS, version_main=CHROME_VERSION_MAIN)
        ip_req, ip_ch = verify_proxy_is_active(driver, requests_session, desc=f"worker {worker_id}")
        if (not ip_req) or (not ip_ch) or (ip_req != ip_ch):
            print(colorize(worker_id, f"Proxy inconsistente (requests:{ip_req} chrome:{ip_ch}) -> rotate"))
            safe_quit(driver)
            # close session before switching proxys
            try:
                requests_session.close()
            except Exception:
                pass
            current_proxy = proxy_mgr.rotate(force=True)
            requests_session = build_requests_session_for_proxy(current_proxy)
            session_proxy_key = (current_proxy["host"], current_proxy["port"])
            driver = recycle_driver_with_proxy(current_proxy, headless=HEADLESS, version_main=CHROME_VERSION_MAIN)
            ip_req, ip_ch = verify_proxy_is_active(driver, requests_session, desc=f"worker {worker_id} (retry)")
            if (not ip_req) or (not ip_ch) or (ip_req != ip_ch):
                print(colorize(worker_id, "WARNING: proxy aún inconsistente. Continuando igualmente."))

        processed_global = 0

    
        def _sync_session_with_proxy():
            nonlocal requests_session, session_proxy_key
            cur = proxy_mgr.get_current()
            key = (cur["host"], cur["port"])
            if key != session_proxy_key:
                try:
                    requests_session.close()
                except Exception:
                    pass
                requests_session = build_requests_session_for_proxy(cur)
                session_proxy_key = key

        for ds_num in assigned_datasets:
            suffix = f"{ds_num:03d}"
            INPUT_CSV   = f'./Kickstarter{suffix}.csv'
            UPDATED_CSV = f'./K{suffix}_updated.csv'
            print(colorize(worker_id, f"===== DATASET {suffix} ====="))
            if not os.path.isfile(INPUT_CSV):
                print(colorize(worker_id, f"[skip] No se encontró {INPUT_CSV}."))
                continue
            try:
                kickstarter_data = read_csv_smart(INPUT_CSV)
            except Exception as e:
                print(colorize(worker_id, f"Error leyendo {INPUT_CSV}: {e}"))
                continue

            start_idx = args.from_index if args.from_index is not None else 0

            updated_rows = []
            processed_ds = 0
            for index, row in kickstarter_data.iterrows():
                if index < start_idx:
                    continue
                if args.max_rows is not None and processed_ds >= args.max_rows:
                    break

                proxy_mgr.rotate_if_stale(PROXY_ROTATE_SECONDS)  # podría rotar
                current_proxy = proxy_mgr.get_current()
                if (current_proxy["host"], current_proxy["port"]) != session_proxy_key:
                    try:
                        requests_session.close()
                    except Exception:
                        pass
                    requests_session = build_requests_session_for_proxy(current_proxy)
                    session_proxy_key = (current_proxy["host"], current_proxy["port"])

                row_out = row.copy()
                attempt_row = 0
                while attempt_row < MAX_DRIVER_RECYCLE_PER_ROW:
                    attempt_row += 1
                    try:
                        urls_data = json.loads(row['urls'])
                        project_url = add_lang(urls_data['web']['project'])
                        print(colorize(worker_id, f"Accessing: {project_url} [row {index}] try={attempt_row}"))

                        driver, ok = open_or_rotate(driver, project_url, headless=HEADLESS, version_main=CHROME_VERSION_MAIN)
                        _sync_session_with_proxy()
                        if not ok:
                            raise RuntimeError("Failed to open project page.")

                        scroll_page(driver, pause_time=3)
                        story_text, image_count, video_count = extract_campaign_text_and_media_counts(driver)
                        row_out['story'] = story_text or ""
                        row_out['image_count'] = int(image_count or 0)
                        row_out['video_count'] = int(video_count or 0)

                        base = urlparse(project_url)
                        base_url = f"{base.scheme}://{base.netloc}{base.path}".rstrip("/")

                        creator_url = add_lang(f"{base_url}/creator")
                        driver, ok = open_or_rotate(driver, creator_url, headless=HEADLESS, version_main=CHROME_VERSION_MAIN)
                        _sync_session_with_proxy()
                        if ok and not is_human_verification_page(driver):
                            creator_info = crawl_creator_from_creator_page(driver)
                            row_out['creator_name']                  = creator_info.get('creator_name', '')
                            row_out['projects_backed_text']          = creator_info.get('projects_backed_text', '')
                            row_out['number_of_projects']            = creator_info.get('number_of_projects', '')
                            row_out['creator_account_created_date']  = creator_info.get('creator_account_created_date', '')
                            row_out['creator_description']           = creator_info.get('creator_description', '')

                        rewards_list = crawl_rewards(driver, project_url=project_url)
                        row_out['rewards'] = json.dumps(rewards_list or [], ensure_ascii=False)

                        posts_url = add_lang(f"{base_url}/posts")
                        driver, ok = open_or_rotate(driver, posts_url, headless=HEADLESS, version_main=CHROME_VERSION_MAIN)
                        _sync_session_with_proxy()
                        if ok:
                            scroll_page(driver, pause_time=2)
                            try:
                                root_scope = driver.find_element(By.TAG_NAME, "body")
                            except Exception:
                                root_scope = driver
                            oklm = load_more_loop_with_budget(driver, root_scope, max_clicks=120, settle_timeout=7)
                            if not oklm:
                                safe_quit(driver)
                                driver = recycle_driver_with_proxy(proxy_mgr.rotate(True), headless=HEADLESS, version_main=CHROME_VERSION_MAIN)
                                _sync_session_with_proxy()
                                driver, ok = open_or_rotate(driver, posts_url, headless=HEADLESS, version_main=CHROME_VERSION_MAIN)
                                _sync_session_with_proxy()
                                if ok:
                                    scroll_page(driver, pause_time=2)
                                    load_more_loop_with_budget(driver, driver, max_clicks=120, settle_timeout=7)
                            update_count, update_data = extract_updates(driver)
                            row_out['update_count'] = update_count if update_count else "0"
                            row_out['update_data']  = json.dumps(update_data if update_data else {})

                        community_url = add_lang(f"{base_url}/community")
                        driver, ok = open_or_rotate(driver, community_url, headless=HEADLESS, version_main=CHROME_VERSION_MAIN)
                        _sync_session_with_proxy()
                        if ok:
                            scroll_page(driver, pause_time=2)
                            community_data = extract_community_data(driver)
                            row_out['backers_count'] = community_data.get('backers_count', 0)
                            row_out['where_backers_come_from_cities'] = json.dumps(
                                community_data.get('where_backers_come_from_cities', [])
                            )
                            row_out['where_backers_come_from_countries'] = json.dumps(
                                community_data.get('where_backers_come_from_countries', [])
                            )
                            row_out['new_backers'] = community_data.get('new_backers', 0)
                            row_out['returning_backers'] = community_data.get('returning_backers', 0)

                        faqs_url = add_lang(f"{base_url}/faqs")
                        driver, ok = open_or_rotate(driver, faqs_url, headless=HEADLESS, version_main=CHROME_VERSION_MAIN)
                        _sync_session_with_proxy()
                        if ok:
                            scroll_page(driver, pause_time=2)
                            faqs = crawl_faqs(driver)
                            row_out['faqs'] = json.dumps(faqs or [], ensure_ascii=False)

                        comments_url = add_lang(f"{base_url}/comments")
                        driver, ok = open_or_rotate(driver, comments_url, headless=HEADLESS, version_main=CHROME_VERSION_MAIN)
                        _sync_session_with_proxy()
                        if ok:
                            scroll_page(driver, pause_time=2)
                            ok_c = load_all_comments(driver, max_clicks=160)
                            if not ok_c:
                                safe_quit(driver)
                                driver = recycle_driver_with_proxy(proxy_mgr.rotate(True), headless=HEADLESS, version_main=CHROME_VERSION_MAIN)
                                _sync_session_with_proxy()
                                driver, ok = open_or_rotate(driver, comments_url, headless=HEADLESS, version_main=CHROME_VERSION_MAIN)
                                _sync_session_with_proxy()
                                if ok:
                                    scroll_page(driver, pause_time=2)
                                    load_all_comments(driver, max_clicks=160)
                            comments_data = extract_comments_data(driver)
                            row_out['comments_count'] = comments_data.get('comments_count', 0)
                            row_out['comments']       = json.dumps(comments_data.get('comments', []))

                        updated_rows.append(row_out)

                        # checkpoint to save data
                        try:
                            df_ckpt = pd.DataFrame(updated_rows)
                            cols_drop = [
                                'product_name','product_price','product_description',
                                'product_ships_to','product_estimated_delivery','product_limited_quantity',
                                'campaign_full','campaign_image_urls','campaign_downloaded_images',
                                'projects_created_text',
                                'image_urls','downloaded_images','video'
                            ]
                            existing = [c for c in cols_drop if c in df_ckpt.columns]
                            if existing:
                                df_ckpt.drop(columns=existing, inplace=True)
                            tmp_name = UPDATED_CSV + ".tmp"
                            df_ckpt.to_csv(tmp_name, index=False, encoding="utf-8")
                            os.replace(tmp_name, UPDATED_CSV)
                            print(colorize(worker_id, f"[checkpoint] -> {UPDATED_CSV} (rows={len(updated_rows)})"))
                        except Exception as e:
                            print(colorize(worker_id, f"checkpoint save failed: {e}"))

                        processed_ds += 1
                        processed_global += 1

                        # rotation of proxys
                        if time.time() - proxy_mgr.last_rotate > PROXY_ROTATE_SECONDS:
                            print(colorize(worker_id, "[proxy] time-based rotate"))
                            safe_quit(driver)
                            new_proxy = proxy_mgr.rotate(True)
                            driver = recycle_driver_with_proxy(new_proxy, headless=HEADLESS, version_main=CHROME_VERSION_MAIN)
                            # new sesion for new proxy
                            try:
                                requests_session.close()
                            except Exception:
                                pass
                            requests_session = build_requests_session_for_proxy(new_proxy)
                            session_proxy_key = (new_proxy["host"], new_proxy["port"])

                        break  

                    except KeyboardInterrupt:
                        print(colorize(worker_id, "Interrumpido por el usuario. Guardando y saliendo..."))
                        safe_quit(driver)
                        try:
                            df_final = pd.DataFrame(updated_rows)
                            df_final.to_csv(UPDATED_CSV, index=False, encoding="utf-8")
                        except Exception:
                            pass
                        return
                    except Exception as e:
                        print(colorize(worker_id, f"Error en fila {index} (try={attempt_row}/{MAX_DRIVER_RECYCLE_PER_ROW}): {e}"))
                        safe_quit(driver)
                        # recycle same proxy first
                        driver = recycle_driver_with_proxy(proxy_mgr.get_current(), headless=HEADLESS, version_main=CHROME_VERSION_MAIN)
                        if attempt_row >= MAX_DRIVER_RECYCLE_PER_ROW:
                            print(colorize(worker_id, f"[row {index}] agotados reintentos -> SKIP"))
                            break

                # sleep time to avoid rate limiter
                if processed_ds % BATCH_SIZE == 0 and processed_ds > 0:
                    try:
                        df_ckpt = pd.DataFrame(updated_rows)
                        cols_drop = [
                            'product_name','product_price','product_description',
                            'product_ships_to','product_estimated_delivery','product_limited_quantity',
                            'campaign_full','campaign_image_urls','campaign_downloaded_images',
                            'projects_created_text',
                            'image_urls','downloaded_images','video'
                        ]
                        existing = [c for c in cols_drop if c in df_ckpt.columns]
                        if existing:
                            df_ckpt.drop(columns=existing, inplace=True)
                        tmp_name = UPDATED_CSV + ".tmp"
                        df_ckpt.to_csv(tmp_name, index=False, encoding="utf-8")
                        os.replace(tmp_name, UPDATED_CSV)
                        print(colorize(worker_id, f"[maintenance] guardado tras {processed_ds} filas -> {UPDATED_CSV}"))
                    except Exception as e:
                        print(colorize(worker_id, f"[maintenance] error guardando checkpoint: {e}"))

                    purge_old_profiles(PROFILE_DIR, keep_last=args.purge_keep,
                                       max_age_hours=args.purge_age_hours,
                                       max_total_gb=args.purge_cap_gb)
                    print(colorize(worker_id, f"{processed_ds} filas procesadas. Descansando {REST_SECONDS}s..."))
                    try:
                        time.sleep(REST_SECONDS)
                    except KeyboardInterrupt:
                        print(colorize(worker_id, "Descanso interrumpido manualmente; continuando..."))

            # final save from the dataset
            try:
                df_final = pd.DataFrame(updated_rows)
                cols_drop = [
                    'product_name','product_price','product_description',
                    'product_ships_to','product_estimated_delivery','product_limited_quantity',
                    'campaign_full','campaign_image_urls','campaign_downloaded_images',
                    'projects_created_text',
                    'image_urls','downloaded_images','video'
                ]
                existing = [c for c in cols_drop if c in df_final.columns]
                if existing:
                    df_final.drop(columns=existing, inplace=True)
                tmp_name = UPDATED_CSV + ".tmp"
                df_final.to_csv(tmp_name, index=False, encoding="utf-8")
                os.replace(tmp_name, UPDATED_CSV)
                print(colorize(worker_id, f"[done] {UPDATED_CSV} (rows: {len(updated_rows)})"))
            except Exception as e:
                print(colorize(worker_id, f"[final-save] Error guardando {UPDATED_CSV}: {e}"))

    finally:
        safe_quit(driver)
        if requests_session is not None:
            try:
                requests_session.close()
            except Exception:
                pass

# Main

def build_arg_parser():
    p = argparse.ArgumentParser(description="Crawler multi-dataset con perfiles rodantes, reintentos y purga.")
    p.add_argument("--worker-id", type=int, required=True, help="ID de este worker (0..workers-1)")
    p.add_argument("--workers", type=int, required=True, help="Total de workers lanzados manualmente")
    p.add_argument("--dataset-start", type=int, default=DATASET_START, help="Primer dataset (por defecto 1)")
    p.add_argument("--dataset-end", type=int, default=DATASET_END, help="Último dataset (por defecto 20)")

    p.add_argument("--target-rows", type=int, default=None, help="Máximo de filas por dataset (obsoleto, usa --max-rows)")
    p.add_argument("--max-rows", type=int, default=None, help="Máximo de filas por dataset")
    p.add_argument("--from-index", type=int, default=None, help="Reanudar desde índice de fila (0-based)")

    p.add_argument("--rotate-seconds", type=int, default=PROXY_ROTATE_SECONDS, help="Segundos para rotar proxy")
    p.add_argument("--proxy-auth-mode", type=str, choices=["ip","userpass"], default=PROXY_AUTH_MODE, help="Modo de autenticación de proxy")
    p.add_argument("--proxy-user", type=str, default=PROXY_USER, help="Usuario proxy (si userpass)")
    p.add_argument("--proxy-pass", type=str, default=PROXY_PASS, help="Password proxy (si userpass)")

    p.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="Filas entre descansos/checkpoints")
    p.add_argument("--rest-seconds", type=int, default=REST_SECONDS, help="Descanso tras batch")

    p.add_argument("--headless", action="store_true", help="Usar modo headless")
    p.add_argument("--chrome-version", type=int, default=CHROME_VERSION_MAIN, help="Chrome mayor para undetected-chromedriver")

    p.add_argument("--purge-keep", type=int, default=PURGE_KEEP_LAST, help="Perfiles recientes a conservar")
    p.add_argument("--purge-age-hours", type=int, default=PURGE_MAX_AGE_HOURS, help="Edad máxima en horas para purgar")
    p.add_argument("--purge-cap-gb", type=float, default=PURGE_MAX_TOTAL_GB, help="Capacidad total de perfiles GB")

    return p

def main():
    global DATASET_START, DATASET_END, WORKERS, TARGET_ROWS, START_INDEX
    parser = build_arg_parser()
    args = parser.parse_args()

    WORKERS = args.workers
    DATASET_START = args.dataset_start
    DATASET_END   = args.dataset_end
    START_INDEX   = args.from_index

    if args.max_rows is None and args.target_rows is not None:
        args.max_rows = args.target_rows

    if DATASET_START < 1 or DATASET_END < DATASET_START:
        raise ValueError("Rango inválido: ajusta --dataset-start y --dataset-end")
    if not (0 <= args.worker_id < WORKERS):
        raise ValueError(f"--worker-id debe estar en [0, {WORKERS-1}]")

    assigned = assign_datasets(DATASET_START, DATASET_END, args.worker_id, WORKERS)
    if not assigned:
        print(colorize(args.worker_id, "No hay datasets asignados para este worker. Revisa el rango o los IDs."))
        return

    print(colorize(args.worker_id,
                   f"Inicio worker | workers={WORKERS} | datasets={assigned} | headless={args.headless} | rotate={args.rotate_seconds}s | chrome={args.chrome_version} | from_index={args.from_index} | max_rows={args.max_rows}"))
    worker_loop(args.worker_id, assigned, args)

if __name__ == "__main__":
    main()
