import json
import os
import time
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse

import pandas as pd
from selenium.webdriver.common.by import By

from .browser import (
    create_driver_for_proxy,
    purge_old_profiles,
    recycle_driver_with_proxy,
    rotate_and_recycle,
    safe_quit,
    verify_proxy_is_active,
)
from .config import Config
from .extractors import (
    crawl_creator_from_creator_page,
    crawl_faqs,
    crawl_rewards,
    extract_campaign_text_and_media_counts,
    extract_comments_data,
    extract_community_data,
    extract_updates,
    load_all_comments,
    load_more_loop_with_budget,
)
from .navigation import is_human_verification_page, open_or_rotate
from .proxy import ProxyManager, build_requests_session_for_proxy, load_proxies, split_proxies_for_worker
from .utils import add_lang, colorize, scroll_page

# columns from the input dataset's older image-download stage; this pipeline never populates them
DROP_COLUMNS = [
    "product_name", "product_price", "product_description",
    "product_ships_to", "product_estimated_delivery", "product_limited_quantity",
    "campaign_full", "campaign_image_urls", "campaign_downloaded_images",
    "projects_created_text",
    "image_urls", "downloaded_images", "video",
]


@dataclass
class WorkerState:
    profile_dir: str
    proxy_mgr: ProxyManager
    current_profile_path: Optional[str] = None


def assign_datasets(dataset_start: int, dataset_end: int, worker_id: int, workers: int) -> List[int]:
    return [ds for ds in range(dataset_start, dataset_end + 1) if (ds % workers) == worker_id]


def read_csv_smart(path):
    last_err = None
    for enc in ["utf-8", "utf-8-sig", "cp1252", "latin1"]:
        try:
            return pd.read_csv(path, encoding=enc)
        except Exception as e:
            last_err = e
    raise last_err


def save_checkpoint(rows, path):
    if not rows:
        return
    df = pd.DataFrame(rows)
    existing = [c for c in DROP_COLUMNS if c in df.columns]
    if existing:
        df = df.drop(columns=existing)
    tmp = path + ".tmp"
    df.to_csv(tmp, index=False, encoding="utf-8")
    os.replace(tmp, path)


def run_worker(worker_id: int, assigned_datasets: List[int], cfg: Config,
                from_index: Optional[int] = None, max_rows: Optional[int] = None):
    profile_dir = os.path.join(cfg.profile_base_dir, f"worker_{worker_id}")
    os.makedirs(profile_dir, exist_ok=True)
    purge_old_profiles(profile_dir, keep_last=cfg.purge_keep_last,
                        max_age_hours=cfg.purge_max_age_hours, max_total_gb=cfg.purge_max_total_gb)

    all_proxies = load_proxies(cfg.proxies_path)
    my_proxies = split_proxies_for_worker(all_proxies, worker_id, cfg.workers)
    if not my_proxies:
        print(colorize(worker_id, "No proxies assigned to this worker. Exiting."))
        return

    proxy_mgr = ProxyManager(my_proxies, cfg.proxy_user, cfg.proxy_pass, auth_mode=cfg.proxy_auth_mode)
    state = WorkerState(profile_dir=profile_dir, proxy_mgr=proxy_mgr)
    print(colorize(worker_id, f"Assigned proxies: {len(my_proxies)}"))

    driver = None
    requests_session = None
    try:
        current_proxy = proxy_mgr.get_current()
        requests_session = build_requests_session_for_proxy(current_proxy)
        session_proxy_key = (current_proxy["host"], current_proxy["port"])

        driver = create_driver_for_proxy(current_proxy, state, headless=cfg.headless, version_main=cfg.chrome_version_main)
        ip_req, ip_ch = verify_proxy_is_active(driver, requests_session, desc=f"worker {worker_id}")
        if (not ip_req) or (not ip_ch) or (ip_req != ip_ch):
            print(colorize(worker_id, f"Inconsistent proxy (requests:{ip_req} chrome:{ip_ch}) -> rotate"))
            safe_quit(driver)
            requests_session.close()
            current_proxy = proxy_mgr.rotate(force=True)
            requests_session = build_requests_session_for_proxy(current_proxy)
            session_proxy_key = (current_proxy["host"], current_proxy["port"])
            driver = recycle_driver_with_proxy(current_proxy, state, headless=cfg.headless, version_main=cfg.chrome_version_main)
            ip_req, ip_ch = verify_proxy_is_active(driver, requests_session, desc=f"worker {worker_id} (retry)")
            if (not ip_req) or (not ip_ch) or (ip_req != ip_ch):
                print(colorize(worker_id, "WARNING: proxy still inconsistent, continuing anyway."))

        def sync_session_with_proxy():
            nonlocal requests_session, session_proxy_key
            cur = proxy_mgr.get_current()
            key = (cur["host"], cur["port"])
            if key != session_proxy_key:
                requests_session.close()
                requests_session = build_requests_session_for_proxy(cur)
                session_proxy_key = key

        os.makedirs(cfg.output_dir, exist_ok=True)

        for ds_num in assigned_datasets:
            suffix = f"{ds_num:03d}"
            input_csv = os.path.join(cfg.input_dir, f"Kickstarter{suffix}.csv")
            output_csv = os.path.join(cfg.output_dir, f"K{suffix}_updated.csv")
            print(colorize(worker_id, f"===== DATASET {suffix} ====="))
            if not os.path.isfile(input_csv):
                print(colorize(worker_id, f"[skip] {input_csv} not found."))
                continue
            try:
                kickstarter_data = read_csv_smart(input_csv)
            except Exception as e:
                print(colorize(worker_id, f"Error reading {input_csv}: {e}"))
                continue

            start_idx = from_index if from_index is not None else 0
            updated_rows = []
            processed_ds = 0

            for index, row in kickstarter_data.iterrows():
                if index < start_idx:
                    continue
                if max_rows is not None and processed_ds >= max_rows:
                    break

                proxy_mgr.rotate_if_stale(cfg.proxy_rotate_seconds)
                sync_session_with_proxy()

                row_out = row.copy()
                attempt_row = 0
                while attempt_row < cfg.max_driver_recycle_per_row:
                    attempt_row += 1
                    try:
                        urls_data = json.loads(row["urls"])
                        project_url = add_lang(urls_data["web"]["project"])
                        print(colorize(worker_id, f"Accessing: {project_url} [row {index}] try={attempt_row}"))

                        driver, ok = open_or_rotate(driver, project_url, state, headless=cfg.headless,
                                                     version_main=cfg.chrome_version_main,
                                                     max_nav_rotations=cfg.max_nav_rotations_per_url)
                        sync_session_with_proxy()
                        if not ok:
                            raise RuntimeError("Failed to open project page.")

                        scroll_page(driver, pause_time=3)
                        story_text, image_count, video_count = extract_campaign_text_and_media_counts(driver)
                        row_out["story"] = story_text or ""
                        row_out["image_count"] = int(image_count or 0)
                        row_out["video_count"] = int(video_count or 0)

                        base = urlparse(project_url)
                        base_url = f"{base.scheme}://{base.netloc}{base.path}".rstrip("/")

                        creator_url = add_lang(f"{base_url}/creator")
                        driver, ok = open_or_rotate(driver, creator_url, state, headless=cfg.headless, version_main=cfg.chrome_version_main)
                        sync_session_with_proxy()
                        if ok and not is_human_verification_page(driver):
                            creator_info = crawl_creator_from_creator_page(driver)
                            row_out["creator_name"] = creator_info.get("creator_name", "")
                            row_out["projects_backed_text"] = creator_info.get("projects_backed_text", "")
                            row_out["number_of_projects"] = creator_info.get("number_of_projects", "")
                            row_out["creator_account_created_date"] = creator_info.get("creator_account_created_date", "")
                            row_out["creator_description"] = creator_info.get("creator_description", "")

                        rewards_list = crawl_rewards(driver, state, project_url=project_url,
                                                      headless=cfg.headless, version_main=cfg.chrome_version_main)
                        row_out["rewards"] = json.dumps(rewards_list or [], ensure_ascii=False)

                        posts_url = add_lang(f"{base_url}/posts")
                        driver, ok = open_or_rotate(driver, posts_url, state, headless=cfg.headless, version_main=cfg.chrome_version_main)
                        sync_session_with_proxy()
                        if ok:
                            scroll_page(driver, pause_time=2)
                            try:
                                root_scope = driver.find_element(By.TAG_NAME, "body")
                            except Exception:
                                root_scope = driver
                            if not load_more_loop_with_budget(driver, root_scope, max_clicks=120, settle_timeout=7):
                                driver = rotate_and_recycle(driver, state, headless=cfg.headless, version_main=cfg.chrome_version_main)
                                sync_session_with_proxy()
                                driver, ok = open_or_rotate(driver, posts_url, state, headless=cfg.headless, version_main=cfg.chrome_version_main)
                                sync_session_with_proxy()
                                if ok:
                                    scroll_page(driver, pause_time=2)
                                    load_more_loop_with_budget(driver, driver, max_clicks=120, settle_timeout=7)
                            update_count, update_data = extract_updates(driver)
                            row_out["update_count"] = update_count if update_count else "0"
                            row_out["update_data"] = json.dumps(update_data if update_data else {})

                        community_url = add_lang(f"{base_url}/community")
                        driver, ok = open_or_rotate(driver, community_url, state, headless=cfg.headless, version_main=cfg.chrome_version_main)
                        sync_session_with_proxy()
                        if ok:
                            scroll_page(driver, pause_time=2)
                            community_data = extract_community_data(driver)
                            row_out["backers_count"] = community_data.get("backers_count", 0)
                            row_out["where_backers_come_from_cities"] = json.dumps(community_data.get("where_backers_come_from_cities", []))
                            row_out["where_backers_come_from_countries"] = json.dumps(community_data.get("where_backers_come_from_countries", []))
                            row_out["new_backers"] = community_data.get("new_backers", 0)
                            row_out["returning_backers"] = community_data.get("returning_backers", 0)

                        faqs_url = add_lang(f"{base_url}/faqs")
                        driver, ok = open_or_rotate(driver, faqs_url, state, headless=cfg.headless, version_main=cfg.chrome_version_main)
                        sync_session_with_proxy()
                        if ok:
                            scroll_page(driver, pause_time=2)
                            row_out["faqs"] = json.dumps(crawl_faqs(driver) or [], ensure_ascii=False)

                        comments_url = add_lang(f"{base_url}/comments")
                        driver, ok = open_or_rotate(driver, comments_url, state, headless=cfg.headless, version_main=cfg.chrome_version_main)
                        sync_session_with_proxy()
                        if ok:
                            scroll_page(driver, pause_time=2)
                            if not load_all_comments(driver, max_clicks=160):
                                driver = rotate_and_recycle(driver, state, headless=cfg.headless, version_main=cfg.chrome_version_main)
                                sync_session_with_proxy()
                                driver, ok = open_or_rotate(driver, comments_url, state, headless=cfg.headless, version_main=cfg.chrome_version_main)
                                sync_session_with_proxy()
                                if ok:
                                    scroll_page(driver, pause_time=2)
                                    load_all_comments(driver, max_clicks=160)
                            comments_data = extract_comments_data(driver)
                            row_out["comments_count"] = comments_data.get("comments_count", 0)
                            row_out["comments"] = json.dumps(comments_data.get("comments", []))

                        updated_rows.append(row_out)
                        save_checkpoint(updated_rows, output_csv)
                        print(colorize(worker_id, f"[checkpoint] -> {output_csv} (rows={len(updated_rows)})"))

                        processed_ds += 1

                        if time.time() - proxy_mgr.last_rotate > cfg.proxy_rotate_seconds:
                            print(colorize(worker_id, "[proxy] time-based rotate"))
                            driver = rotate_and_recycle(driver, state, headless=cfg.headless, version_main=cfg.chrome_version_main)
                            sync_session_with_proxy()

                        break

                    except KeyboardInterrupt:
                        print(colorize(worker_id, "Interrupted by user. Saving and exiting..."))
                        safe_quit(driver)
                        save_checkpoint(updated_rows, output_csv)
                        return
                    except Exception as e:
                        print(colorize(worker_id, f"Error on row {index} (try={attempt_row}/{cfg.max_driver_recycle_per_row}): {e}"))
                        safe_quit(driver)
                        driver = recycle_driver_with_proxy(proxy_mgr.get_current(), state, headless=cfg.headless, version_main=cfg.chrome_version_main)
                        if attempt_row >= cfg.max_driver_recycle_per_row:
                            print(colorize(worker_id, f"[row {index}] retries exhausted -> SKIP"))
                            break

                # self-throttle: rest between batches to stay under Kickstarter's rate limit
                if processed_ds % cfg.batch_size == 0 and processed_ds > 0:
                    save_checkpoint(updated_rows, output_csv)
                    print(colorize(worker_id, f"[maintenance] saved after {processed_ds} rows -> {output_csv}"))
                    purge_old_profiles(profile_dir, keep_last=cfg.purge_keep_last,
                                        max_age_hours=cfg.purge_max_age_hours, max_total_gb=cfg.purge_max_total_gb)
                    print(colorize(worker_id, f"{processed_ds} rows processed. Resting {cfg.rest_seconds}s..."))
                    try:
                        time.sleep(cfg.rest_seconds)
                    except KeyboardInterrupt:
                        print(colorize(worker_id, "Rest interrupted manually, continuing..."))

            save_checkpoint(updated_rows, output_csv)
            print(colorize(worker_id, f"[done] {output_csv} (rows: {len(updated_rows)})"))

    finally:
        safe_quit(driver)
        if requests_session is not None:
            try:
                requests_session.close()
            except Exception:
                pass
