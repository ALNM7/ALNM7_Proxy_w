import json
import os
import random
import time
from typing import List

import requests

from .config import USER_AGENT, ACCEPT_LANGUAGE

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": ACCEPT_LANGUAGE,
}


def _proxy_url_noauth(h, p):
    return f"http://{h}:{int(p)}"


def _proxy_url_userpass(h, p, u, pw):
    return f"http://{u}:{pw}@{h}:{int(p)}"


class ProxyManager:
    def __init__(self, proxy_list, user, password, auth_mode="ip"):
        self.all_proxies = proxy_list[:]
        self.proxies = proxy_list[:]
        random.shuffle(self.proxies)
        self.user = user
        self.password = password
        self.auth_mode = auth_mode
        self.index = -1
        self.current = None
        self.last_rotate = 0.0

    def ensure_available(self):
        if not self.proxies:
            self.proxies = self.all_proxies[:]
            random.shuffle(self.proxies)
            self.index = -1
            print("[proxy] queue exhausted, reshuffled for another pass")

    def get_current(self):
        if self.current is None:
            return self.rotate(force=True)
        return self.current

    def rotate(self, force=False):
        self.ensure_available()
        self.index = (self.index + 1) % len(self.proxies)
        entry = self.proxies[self.index]
        h, p = entry["host"], int(entry["port"])
        if self.auth_mode == "ip":
            url = _proxy_url_noauth(h, p)
        else:
            url = _proxy_url_userpass(h, p, self.user, self.password)
        self.current = {"host": h, "port": p, "url": url, "ts": time.time()}
        self.last_rotate = time.time()
        print(f"[proxy] rotated to {h}:{p} (idx {self.index})")
        warm_proxy(self.current)
        return self.current

    def rotate_if_stale(self, seconds):
        if time.time() - self.last_rotate > seconds:
            return self.rotate(force=True)
        return self.current


def build_requests_session_for_proxy(proxy_info):
    s = requests.Session()
    s.proxies = {"http": proxy_info["url"], "https": proxy_info["url"]}
    s.verify = True
    s.headers.update(HEADERS)
    return s


def warm_proxy(proxy_info):
    # one throwaway request right after rotation so a cold proxy connection isn't paid for by real traffic
    url = "https://ipv4.webshare.io/"
    try:
        r = requests.get(url, proxies={"http": proxy_info["url"], "https": proxy_info["url"]}, timeout=12)
        print(f"[proxy] warmup {proxy_info['host']}:{proxy_info['port']} -> {r.status_code}")
    except Exception as e:
        print(f"[proxy] warmup error {proxy_info['host']}:{proxy_info['port']}: {e}")


def load_proxies(path: str = "proxies.json") -> List[dict]:
    env_path = os.environ.get("PROXIES_PATH")
    for p in [env_path, path]:
        if not p or not os.path.isfile(p):
            continue
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and all(("host" in x and "port" in x) for x in data):
            print(f"[proxy] loaded {len(data)} proxies from {p}")
            return data
    raise FileNotFoundError(
        f"No proxy list found at '{path}'. Copy proxies.example.json to {path} and fill in "
        "real proxies, or point PROXIES_PATH at one."
    )


def split_proxies_for_worker(all_proxies, worker_id, total_workers):
    return [p for i, p in enumerate(all_proxies) if (i % total_workers) == worker_id]
