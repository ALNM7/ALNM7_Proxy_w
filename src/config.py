import os
from dataclasses import dataclass
from typing import Optional

import yaml

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE = "en-US,en;q=0.9"


@dataclass
class Config:
    dataset_start: int = 1
    dataset_end: int = 82
    workers: int = 4

    proxy_auth_mode: str = "ip"  # "ip" (whitelisted IP) or "userpass"
    proxy_user: Optional[str] = None
    proxy_pass: Optional[str] = None
    proxy_rotate_seconds: int = 180
    proxies_path: str = "proxies.json"

    batch_size: int = 15
    rest_seconds: int = 70

    chrome_version_main: int = 140
    headless: bool = False

    delete_prev_profile_on_replace: bool = True
    purge_keep_last: int = 3
    purge_max_age_hours: int = 8
    purge_max_total_gb: float = 20.0

    max_driver_recycle_per_row: int = 3
    max_nav_rotations_per_url: int = 2

    input_dir: str = "./data/raw"
    output_dir: str = "./data/processed"
    profile_base_dir: str = "./profiles"


def load_config(path: str = "config.yaml") -> Config:
    cfg = Config()
    if not os.path.isfile(path):
        return cfg
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    for key, value in raw.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    return cfg
