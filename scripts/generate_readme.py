from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import hashlib
from html import escape
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError
from collections import Counter

import yaml
from PIL import Image, ImageDraw, ImageFont


AUTO_GENERATED_NOTICE = (
    "<!-- AUTO-GENERATED FROM README.template.md AND profile-data.yml. DO NOT EDIT DIRECTLY. -->\n"
)


PLACEHOLDER_PATTERN = re.compile(r"{{\s*([A-Z0-9_]+)\s*}}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate GitHub profile README from template and config.")
    parser.add_argument("--config", default="profile-data.yml")
    parser.add_argument("--template", default="README.template.md")
    parser.add_argument("--output", default="README.md")
    parser.add_argument("--snapshot-png", default="assets/generated/profile-snapshot.png")
    return parser.parse_args()


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a mapping in {path}")
    return data


def github_request(url: str, token: str | None = None) -> tuple[Any, dict[str, str]]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Hi-Jiajun-profile-readme-generator",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = request.Request(url, headers=headers)
    try:
        with request.urlopen(req) as response:
            body = response.read().decode("utf-8")
            header_map = {k: v for k, v in response.headers.items()}
    except HTTPError as exc:
        exc_headers = exc.headers or {}
        remaining = exc_headers.get("X-RateLimit-Remaining")
        reset = exc_headers.get("X-RateLimit-Reset")
        detail = f" (rate limit remaining={remaining}, reset={reset})" if remaining is not None else ""
        raise RuntimeError(f"GitHub API {exc.code} on {url}{detail}: {exc.reason}") from exc
    return json.loads(body), header_map


def fetch_repositories(username: str, token: str | None = None) -> list[dict[str, Any]]:
    repos: list[dict[str, Any]] = []
    page = 1

    while True:
        url = f"https://api.github.com/users/{username}/repos?per_page=100&page={page}&sort=updated&type=owner"
        data, _headers = github_request(url, token=token)
        if not data:
            break
        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected GitHub API response: {data}")

        repos.extend(data)

        if len(data) < 100:
            break
        page += 1

    return repos


def escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br/>")


def format_date(value: str | None) -> str:
    if not value:
        return "-"
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def format_list(items: list[dict[str, str]]) -> str:
    lines = []
    for item in items:
        icon = item["icon"]
        en = item["en"]
        zh = item["zh"]
        lines.append(f"- {icon} {en} / {zh}")
    return "\n".join(lines)


def format_toolbox_badges(items: list[dict[str, str]]) -> str:
    badges = []
    for item in items:
        label = parse.quote(item["label"])
        message = parse.quote(item["message"])
        color = item["color"]
        alt = escape_cell(item["label"])
        badges.append(
            f'  <img src="https://img.shields.io/badge/{label}-{message}-{color}?style=flat-square" alt="{alt}" />'
        )
    return "\n".join(badges)


def format_activity(repo: dict[str, Any]) -> str:
    language = repo.get("language") or "—"
    stars = repo.get("stargazers_count", 0)
    pushed = format_date(repo.get("pushed_at") or repo.get("updated_at"))
    return escape_cell(f"{language} · ⭐ {stars} · updated {pushed}")


def build_project_rows(
    entries: list[dict[str, str]],
    repo_map: dict[str, dict[str, Any]],
    username: str,
) -> str:
    rows = []
    for entry in entries:
        repo_name = entry["repo"]
        repo = repo_map.get(repo_name)
        repo_url = repo["html_url"] if repo else f"https://github.com/{username}/{repo_name}"
        note = f'{entry["note_en"]}<br/>{entry["note_zh"]}'
        activity = format_activity(repo) if repo else "Unknown · ⭐ 0 · updated -"
        rows.append(
            f'| [{escape_cell(repo_name)}]({repo_url}) | {escape_cell(note)} | {escape_cell(activity)} |'
        )
    return "\n".join(rows)


def build_recent_rows(
    config: dict[str, Any],
    repo_map: dict[str, dict[str, Any]],
    username: str,
) -> str:
    tracked_entries = []
    categories = [
        ("🛠️ Original", config["projects"]["original"]),
        ("🍴 Fork", config["projects"]["forks"]),
    ]

    for category_label, entries in categories:
        for entry in entries:
            repo_name = entry["repo"]
            repo = repo_map.get(repo_name)
            pushed_at = repo.get("pushed_at") if repo else None
            tracked_entries.append((pushed_at or "", category_label, entry, repo))

    tracked_entries.sort(key=lambda item: item[0], reverse=True)
    limit = int(config.get("recent_activity", {}).get("limit", 5))

    rows = []
    for _pushed_at, category_label, entry, repo in tracked_entries[:limit]:
        repo_name = entry["repo"]
        repo_url = repo["html_url"] if repo else f"https://github.com/{username}/{repo_name}"
        pushed = format_date(repo.get("pushed_at") if repo else None)
        note = f'{category_label} · {entry["note_en"]}<br/>{entry["note_zh"]}<br/>updated {pushed}'
        rows.append(f'| [{escape_cell(repo_name)}]({repo_url}) | {escape_cell(note)} |')
    return "\n".join(rows)


def collect_top_languages(repos: list[dict[str, Any]], limit: int = 5) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for repo in repos:
        language = repo.get("language")
        if language:
            counter[language] += 1
    return counter.most_common(limit)


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = []
    if bold:
        candidates.extend(
            [
                "C:/Windows/Fonts/segoeuib.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            ]
        )
    else:
        candidates.extend(
            [
                "C:/Windows/Fonts/segoeui.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            ]
        )

    for candidate in candidates:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def write_snapshot_png(snapshot_path: Path, username: str, repos: list[dict[str, Any]], config: dict[str, Any]) -> str:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)

    width, height = 1280, 460
    image = Image.new("RGBA", (width, height), (8, 18, 31, 255))
    draw = ImageDraw.Draw(image, "RGBA")

    title_font = load_font(42, bold=True)
    label_font = load_font(18, bold=True)
    stat_value_font = load_font(36, bold=True)
    stat_label_font = load_font(16, bold=False)
    small_font = load_font(14, bold=False)

    for x in range(width):
        blend = x / max(width - 1, 1)
        r = int(8 + (18 - 8) * blend)
        g = int(18 + (75 - 18) * blend)
        b = int(31 + (87 - 31) * blend)
        draw.line([(x, 0), (x, height)], fill=(r, g, b, 255))

    draw.rounded_rectangle((8, 8, width - 8, height - 8), radius=30, outline=(255, 255, 255, 24), width=1)
    draw.ellipse((905, 40, 1190, 260), fill=(56, 189, 248, 14))
    draw.ellipse((780, 220, 1050, 430), fill=(45, 212, 191, 10))

    public_repo_count = len(repos)
    fork_repo_count = sum(1 for repo in repos if repo.get("fork"))
    original_tracked = len(config["projects"]["original"])
    total_stars = sum(int(repo.get("stargazers_count", 0)) for repo in repos)

    draw.text((72, 58), "SNAPSHOT / AUTO-GENERATED", font=label_font, fill="#8DD4FF")
    draw.text((72, 102), "GitHub snapshot", font=title_font, fill="#F8FAFC")
    draw.text((72, 154), "A compact view of repositories, tracked work, and language mix", font=stat_label_font, fill="#D4E7F5")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    draw.text((1160, 62), f"{today} UTC", font=small_font, fill="#B7D1E6", anchor="ra")

    cards = [
        ("Public repos", str(public_repo_count), "#38bdf8"),
        ("Fork repos", str(fork_repo_count), "#60a5fa"),
        ("Original work", str(original_tracked), "#2dd4bf"),
        ("Total stars", str(total_stars), "#f59e0b"),
    ]

    card_positions = [
        (72, 214),
        (336, 214),
        (72, 336),
        (336, 336),
    ]
    for (label, value, color), (x0, y0) in zip(cards, card_positions):
        x1 = x0 + 224
        y1 = y0 + 92
        draw.rounded_rectangle((x0, y0, x1, y1), radius=22, fill=(16, 30, 50, 218), outline=(255, 255, 255, 24), width=1)
        draw.rounded_rectangle((x0 + 18, y0 + 18, x0 + 68, y0 + 24), radius=3, fill=color)
        draw.text((x0 + 18, y0 + 34), value, font=stat_value_font, fill="#F8FAFC")
        draw.text((x0 + 18, y0 + 72), label, font=stat_label_font, fill="#C7DCEF")

    panel_x0, panel_y0, panel_x1, panel_y1 = 636, 214, 1200, 436
    draw.rounded_rectangle((panel_x0, panel_y0, panel_x1, panel_y1), radius=24, fill=(13, 28, 44, 210), outline=(255, 255, 255, 22), width=1)
    draw.text((664, 258), "TOP LANGUAGES", font=label_font, fill="#8DD4FF")

    top_languages = collect_top_languages(repos, limit=4)
    max_count = max((count for _language, count in top_languages), default=1)
    palette = ["#38bdf8", "#2dd4bf", "#f59e0b", "#a78bfa", "#f472b6"]

    for index, (language, count) in enumerate(top_languages):
        y = 294 + index * 34
        bar_y = y + 20
        bar_width = 476
        fill_width = bar_width if max_count == 0 else round((count / max_count) * bar_width)
        color = palette[index % len(palette)]

        draw.text((664, y), language, font=stat_label_font, fill="#F8FAFC")
        draw.text((1168, y), f"{count} repos", font=small_font, fill="#C7DCEF", anchor="ra")
        draw.rounded_rectangle((664, bar_y, 664 + bar_width, bar_y + 10), radius=5, fill=(255, 255, 255, 18))
        draw.rounded_rectangle((664, bar_y, 664 + fill_width, bar_y + 10), radius=5, fill=color)

    image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    new_bytes = buffer.getvalue()

    if snapshot_path.exists() and snapshot_path.read_bytes() == new_bytes:
        return hashlib.sha256(new_bytes).hexdigest()[:12]

    snapshot_path.write_bytes(new_bytes)
    return hashlib.sha256(new_bytes).hexdigest()[:12]


def render_template(template_text: str, values: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(f"Missing template value for {key}")
        return values[key]

    return PLACEHOLDER_PATTERN.sub(replace, template_text)


def main() -> int:
    args = parse_args()
    config_path = Path(args.config)
    template_path = Path(args.template)
    output_path = Path(args.output)
    snapshot_png_path = Path(args.snapshot_png)

    config = load_yaml(config_path)
    with template_path.open("r", encoding="utf-8") as fh:
        template_text = fh.read()

    username = config["profile"]["username"]
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    repos = fetch_repositories(username, token=token)
    repo_map = {repo["name"]: repo for repo in repos}

    snapshot_hash = write_snapshot_png(snapshot_png_path, username, repos, config)

    values = {
        "USERNAME": username,
        "SNAPSHOT_IMAGE_URL": f"./assets/generated/profile-snapshot.png?v={snapshot_hash}",
        "HEADER_TITLE": config["profile"]["header_title"],
        "HEADER_ROLE": config["profile"]["header_role"],
        "HEADER_INTRO_ZH": config["profile"]["header_intro_zh"],
        "HEADER_INTRO_EN": config["profile"]["header_intro_en"],
        "OWNERSHIP_NOTE_ZH": config["profile"]["ownership_note_zh"],
        "OWNERSHIP_NOTE_EN": config["profile"]["ownership_note_en"],
        "ABOUT_INTRO_ZH": config["section_copy"]["about_intro_zh"],
        "ABOUT_INTRO_EN": config["section_copy"]["about_intro_en"],
        "ABOUT_ITEMS": format_list(config["about"]),
        "FOCUS_ITEMS": format_list(config["current_focus"]),
        "ORIGINAL_INTRO_ZH": config["section_copy"]["original_intro_zh"],
        "ORIGINAL_INTRO_EN": config["section_copy"]["original_intro_en"],
        "ORIGINAL_ROWS": build_project_rows(config["projects"]["original"], repo_map, username),
        "FORKS_INTRO_ZH": config["section_copy"]["forks_intro_zh"],
        "FORKS_INTRO_ZH_EXTRA": config["section_copy"]["forks_intro_zh_extra"],
        "FORKS_INTRO_EN": config["section_copy"]["forks_intro_en"],
        "FORK_ROWS": build_project_rows(config["projects"]["forks"], repo_map, username),
        "RECENT_INTRO_ZH": config["section_copy"]["recent_intro_zh"],
        "RECENT_INTRO_EN": config["section_copy"]["recent_intro_en"],
        "RECENT_ROWS": build_recent_rows(config, repo_map, username),
        "SNAPSHOT_INTRO_ZH": config["section_copy"]["snapshot_intro_zh"],
        "SNAPSHOT_INTRO_EN": config["section_copy"]["snapshot_intro_en"],
        "WORK_ITEMS": format_list(config["how_i_work"]),
        "TOOLBOX_BADGES": format_toolbox_badges(config["toolbox"]),
        "FIND_HERE_ITEMS": format_list(config["find_here"]),
        "FOOTER_ZH": config["profile"]["footer_zh"],
        "FOOTER_EN": config["profile"]["footer_en"],
    }

    output = AUTO_GENERATED_NOTICE + render_template(template_text, values)
    with output_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(output.rstrip() + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
