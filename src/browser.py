import glob
import os
import random
import shutil
import socket
import time

import undetected_chromedriver as uc
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from .config import USER_AGENT
from .utils import safe_driver_get, safe_request


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


def build_driver(proxy_info=None, profile_dir=None, attempt=1, headless=False, version_main=140):
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
    opts.add_argument(f"--user-agent={USER_AGENT}")
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
    opts.add_experimental_option("prefs", {
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
        "signin_promo_enabled": False,
    })
    caps = DesiredCapabilities.CHROME.copy()
    caps["goog:loggingPrefs"] = {"performance": "ALL"}  # needed to read real 429s / Retry-After off the network log
    try:
        driver = uc.Chrome(
            options=opts,
            headless=False,
            use_subprocess=True,
            desired_capabilities=caps,
            version_main=version_main,
        )
    except (PermissionError, FileExistsError, WebDriverException) as e:
        if attempt < 3:
            print(f"[driver] error creating driver (attempt {attempt}/3): {e}")
            time.sleep(0.8)
            try:
                suffix = f"_r{random.randint(1000, 9999)}"
                new_dir = profile_dir + suffix
                shutil.copytree(profile_dir, new_dir, dirs_exist_ok=True)
                return build_driver(proxy_info=proxy_info, profile_dir=new_dir, attempt=attempt + 1,
                                     headless=headless, version_main=version_main)
            except Exception as e2:
                print(f"[driver] profile clone failed: {e2}")
                return build_driver(proxy_info=proxy_info, profile_dir=profile_dir, attempt=attempt + 1,
                                     headless=headless, version_main=version_main)
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
    print(f"[driver] Chrome started (profile {profile_dir}) debug port {free_port} headless={headless}")
    return driver


def build_driver_with_retry(proxy_info=None, base_profile_dir=None, headless=False, version_main=140):
    if base_profile_dir is None:
        raise RuntimeError("base_profile_dir required")
    slug = f"{int(time.time())}_{random.randint(1000, 9999)}"
    prof = os.path.abspath(base_profile_dir + "_" + slug)
    os.makedirs(base_profile_dir, exist_ok=True)
    driver = build_driver(proxy_info=proxy_info, profile_dir=prof, headless=headless, version_main=version_main)
    return driver, prof


def safe_quit(driver):
    try:
        if driver:
            driver.quit()
    except Exception:
        pass


def create_driver_for_proxy(proxy, state, headless=False, version_main=140):
    driver, prof = build_driver_with_retry(proxy_info=proxy, base_profile_dir=state.profile_dir,
                                            headless=headless, version_main=version_main)
    state.current_profile_path = prof
    return driver


def recycle_driver_with_proxy(proxy, state, headless=False, version_main=140, delete_prev=True):
    old_prof = state.current_profile_path
    driver, prof = build_driver_with_retry(proxy_info=proxy, base_profile_dir=state.profile_dir,
                                            headless=headless, version_main=version_main)
    if delete_prev and old_prof and os.path.isdir(old_prof) and old_prof != prof:
        shutil.rmtree(old_prof, ignore_errors=True)
        print(f"[cleanup] removed old profile: {old_prof}")
    state.current_profile_path = prof
    return driver


def rotate_and_recycle(driver, state, headless=False, version_main=140):
    safe_quit(driver)
    new_proxy = state.proxy_mgr.rotate(force=True)
    return recycle_driver_with_proxy(new_proxy, state, headless=headless, version_main=version_main)


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
        return driver.find_element(By.TAG_NAME, "body").text.strip()
    except Exception:
        return None


def verify_proxy_is_active(driver, requests_session, desc=""):
    ip_req = get_ip_via_requests(requests_session)
    ip_ch = get_ip_via_chrome(driver)
    print(f"[verify] {desc} requests_ip={ip_req} | chrome_ip={ip_ch}")
    return ip_req, ip_ch


def _dir_size_bytes(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def purge_old_profiles(base_prefix: str, keep_last: int = 3, max_age_hours: int = 12, max_total_gb: float = 30.0):
    candidates = []
    now = time.time()
    for d in glob.glob(base_prefix + "_*"):
        try:
            st = os.stat(d)
            candidates.append((d, st.st_mtime, (now - st.st_mtime) / 3600.0))
        except OSError:
            continue
    candidates.sort(key=lambda x: x[1], reverse=True)

    for d, _, age_h in candidates[keep_last:]:
        if age_h > max_age_hours:
            print(f"[purge] removing old profile: {d}")
            shutil.rmtree(d, ignore_errors=True)

    def total_gb():
        return sum(_dir_size_bytes(d) for d, _, _ in candidates if os.path.exists(d)) / (1024 ** 3)

    extra = sorted([c for c in candidates[keep_last:] if os.path.exists(c[0])], key=lambda x: x[1])
    while total_gb() > max_total_gb and extra:
        d, _, _ = extra.pop(0)
        print(f"[purge] removing to fit cap: {d}")
        shutil.rmtree(d, ignore_errors=True)
