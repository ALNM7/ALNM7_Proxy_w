import os

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "figures", "architecture.png")

BOX_FACE = "#eef2f7"
BOX_EDGE = "#2b3a55"
WORKER_FACE = "#dce8f5"
STORE_FACE = "#e8f5e9"
LOOP_FACE = "#fdf2e3"
LOOP_EDGE = "#a06a1a"
TEXT_COLOR = "#1a1a1a"


def box(ax, xy, w, h, text, face=BOX_FACE, edge=BOX_EDGE, fontsize=10, weight="normal", z=2):
    x, y = xy
    patch = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.02,rounding_size=0.06",
        linewidth=1.4, edgecolor=edge, facecolor=face, zorder=z,
    )
    ax.add_patch(patch)
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
             fontsize=fontsize, color=TEXT_COLOR, weight=weight, zorder=z + 1)
    return patch


def arrow(ax, start, end, color=BOX_EDGE, lw=1.6, connectionstyle="arc3,rad=0.0", z=3):
    ax.add_patch(FancyArrowPatch(
        start, end, arrowstyle="-|>", mutation_scale=15,
        linewidth=lw, color=color, connectionstyle=connectionstyle, zorder=z,
    ))


def main():
    fig, ax = plt.subplots(figsize=(13, 7.2), dpi=150)
    ax.set_xlim(0, 13)
    ax.set_ylim(0, 7.2)
    ax.axis("off")
    fig.patch.set_facecolor("white")

    ax.text(6.5, 6.85, "Distributed Kickstarter Crawling Pipeline", ha="center",
             fontsize=15, weight="bold", color=TEXT_COLOR)

    box(ax, (0.3, 5.0), 2.3, 1.2,
        "Kickstarter\nproject pages\n(story, creator,\nrewards, posts,\ncommunity, FAQs,\ncomments)",
        fontsize=8.5)

    box(ax, (0.3, 3.1), 2.3, 1.2,
        "Dataset shards\nKickstarter{NNN}.csv\n(82 shards)", fontsize=9)

    arrow(ax, (1.45, 5.0), (1.45, 4.3))

    box(ax, (3.1, 0.7), 6.3, 5.0, "", face="#f7f9fc", edge="#8496b0", z=1)
    ax.text(6.25, 5.35, "Worker pool (static partition, no queue/broker)",
             ha="center", fontsize=10.5, weight="bold", color=TEXT_COLOR)
    ax.text(6.25, 5.0, "dataset id % workers  ·  proxy index % workers",
             ha="center", fontsize=8.5, style="italic", color="#44506b")

    worker_xs = [3.4, 5.1, 6.8, 8.5]
    for i, wx in enumerate(worker_xs):
        box(ax, (wx, 3.5), 1.5, 1.3,
            f"Worker {i}\n\nundetected-\nchromedriver\n+ Selenium",
            face=WORKER_FACE, fontsize=7.8)

    arrow(ax, (2.6, 3.7), (3.35, 3.9), lw=1.2)

    box(ax, (4.0, 1.0), 4.5, 1.9,
        "Rate-limit / robustness loop\nCloudflare + CAPTCHA + 429 detection\nrotate proxy -> rebuild driver -> retry\n(per-row and per-batch backoff)",
        face=LOOP_FACE, edge=LOOP_EDGE, fontsize=8.5)
    for wx in worker_xs:
        arrow(ax, (wx + 0.75, 3.5), (wx + 0.75, 2.9), color=LOOP_EDGE, lw=1.2)

    box(ax, (9.9, 3.6), 2.8, 1.4,
        "Checkpointed CSV output\nK{NNN}_updated.csv\natomic write (tmp + replace)\nevery batch and per row",
        face=STORE_FACE, edge="#2e7d32", fontsize=8)
    arrow(ax, (8.5, 1.95), (9.85, 3.9), color=LOOP_EDGE, lw=1.4,
          connectionstyle="arc3,rad=-0.2")

    box(ax, (9.9, 1.0), 2.8, 1.9,
        "Preprocessing\ndrop stale columns from\nan earlier scraper stage\nbefore each save", fontsize=8.3,
        face=STORE_FACE, edge="#2e7d32")
    arrow(ax, (11.3, 3.6), (11.3, 2.9), color="#2e7d32")

    ax.text(0.3, 0.35,
            "Static partition: each worker is a separate OS process launched manually, not a job queue.",
            fontsize=8, style="italic", color="#555")

    plt.tight_layout()
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    plt.savefig(OUT_PATH, facecolor="white", bbox_inches="tight")
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
