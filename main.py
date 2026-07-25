import argparse

from src.config import load_config
from src.pipeline import assign_datasets, run_worker
from src.utils import colorize


def build_arg_parser(cfg):
    p = argparse.ArgumentParser(
        description="Multi-dataset Kickstarter crawler with rolling browser profiles, retries and proxy rotation."
    )
    p.add_argument("--config", type=str, default="config.yaml", help="Path to a config.yaml (see config.example.yaml)")
    p.add_argument("--worker-id", type=int, required=True, help="ID of this worker (0..workers-1)")
    p.add_argument("--workers", type=int, default=cfg.workers, help="Total number of workers launched manually")
    p.add_argument("--dataset-start", type=int, default=cfg.dataset_start)
    p.add_argument("--dataset-end", type=int, default=cfg.dataset_end)

    p.add_argument("--max-rows", type=int, default=None, help="Max rows to process per dataset shard")
    p.add_argument("--from-index", type=int, default=None, help="Resume from a row index (0-based)")

    p.add_argument("--rotate-seconds", type=int, default=cfg.proxy_rotate_seconds)
    p.add_argument("--proxy-auth-mode", type=str, choices=["ip", "userpass"], default=cfg.proxy_auth_mode)
    p.add_argument("--proxy-user", type=str, default=cfg.proxy_user)
    p.add_argument("--proxy-pass", type=str, default=cfg.proxy_pass)

    p.add_argument("--batch-size", type=int, default=cfg.batch_size)
    p.add_argument("--rest-seconds", type=int, default=cfg.rest_seconds)

    p.add_argument("--headless", action="store_true", default=cfg.headless)
    p.add_argument("--chrome-version", type=int, default=cfg.chrome_version_main)

    p.add_argument("--purge-keep", type=int, default=cfg.purge_keep_last)
    p.add_argument("--purge-age-hours", type=int, default=cfg.purge_max_age_hours)
    p.add_argument("--purge-cap-gb", type=float, default=cfg.purge_max_total_gb)

    return p


def main():
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", type=str, default="config.yaml")
    pre_args, _ = pre.parse_known_args()
    cfg = load_config(pre_args.config)

    args = build_arg_parser(cfg).parse_args()

    cfg.workers = args.workers
    cfg.dataset_start = args.dataset_start
    cfg.dataset_end = args.dataset_end
    cfg.proxy_rotate_seconds = args.rotate_seconds
    cfg.proxy_auth_mode = args.proxy_auth_mode
    cfg.proxy_user = args.proxy_user
    cfg.proxy_pass = args.proxy_pass
    cfg.batch_size = args.batch_size
    cfg.rest_seconds = args.rest_seconds
    cfg.headless = args.headless
    cfg.chrome_version_main = args.chrome_version
    cfg.purge_keep_last = args.purge_keep
    cfg.purge_max_age_hours = args.purge_age_hours
    cfg.purge_max_total_gb = args.purge_cap_gb

    if cfg.dataset_start < 1 or cfg.dataset_end < cfg.dataset_start:
        raise ValueError("Invalid range: check --dataset-start and --dataset-end")
    if not (0 <= args.worker_id < cfg.workers):
        raise ValueError(f"--worker-id must be in [0, {cfg.workers - 1}]")

    assigned = assign_datasets(cfg.dataset_start, cfg.dataset_end, args.worker_id, cfg.workers)
    if not assigned:
        print(colorize(args.worker_id, "No datasets assigned to this worker. Check the range or worker ids."))
        return

    print(colorize(
        args.worker_id,
        f"Starting worker | workers={cfg.workers} | datasets={assigned} | headless={cfg.headless} "
        f"| rotate={cfg.proxy_rotate_seconds}s | chrome={cfg.chrome_version_main} "
        f"| from_index={args.from_index} | max_rows={args.max_rows}"
    ))
    run_worker(args.worker_id, assigned, cfg, from_index=args.from_index, max_rows=args.max_rows)


if __name__ == "__main__":
    main()
