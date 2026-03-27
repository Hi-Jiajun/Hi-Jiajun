from __future__ import annotations

import argparse
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
from collections import Counter

import yaml
from PIL import Image, ImageDraw, ImageFont


PLACEHOLDER_PATTERN = re.compile(r"{{\s*([A-Z0-9_]+)\s*}}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate GitHub profile README from template and config.")
    parser.add_argument("--config", default="profile-data.yml")
    parser.add_argument("--template", default="README.template.md")
    parser.add_argument("--output", default="README.md")
    parser.add_argument("--snapshot-svg", default="assets/generated/profile-snapshot.svg")
    parser.add_argument("--focus-gif", default="assets/generated/profile-focus.gif")
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
    with request.urlopen(req) as response:
        body = response.read().decode("utf-8")
        header_map = {k: v for k, v in response.headers.items()}
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

        public_repos = [repo for repo in data if not repo.get("private", False)]
        repos.extend(public_repos)

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
    language = repo.get("language") or "Mixed"
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


def build_snapshot_rows(
    repos: list[dict[str, Any]],
    config: dict[str, Any],
) -> str:
    public_repo_count = len(repos)
    fork_repo_count = sum(1 for repo in repos if repo.get("fork"))
    original_tracked = len(config["projects"]["original"])
    fork_tracked = len(config["projects"]["forks"])
    total_stars = sum(int(repo.get("stargazers_count", 0)) for repo in repos)
    active_limit = int(config.get("recent_activity", {}).get("limit", 5))

    metrics = [
        ("📦 Public repositories / 公开仓库", str(public_repo_count)),
        ("🍴 Fork repositories / Fork 仓库", str(fork_repo_count)),
        ("🛠️ Tracked original projects / 跟踪的原创项目", str(original_tracked)),
        ("🤝 Tracked forks & references / 跟踪的 fork 与参考项目", str(fork_tracked)),
        ("⭐ Total stars received / 获得的总 Star", str(total_stars)),
        ("⏱️ Recently active list size / 最近活跃列表长度", str(active_limit)),
    ]
    return "\n".join(f"| {escape_cell(label)} | {escape_cell(value)} |" for label, value in metrics)


def build_top_languages(repos: list[dict[str, Any]], limit: int = 6) -> str:
    counter: Counter[str] = Counter()
    for repo in repos:
        language = repo.get("language")
        if language:
            counter[language] += 1

    if not counter:
        return "- No language data available yet / 暂时没有可用的语言数据"

    lines = []
    for language, count in counter.most_common(limit):
        lines.append(f"- `{language}` in {count} public repos / `{language}` 出现在 {count} 个公开仓库中")
    return "\n".join(lines)


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


def write_focus_gif(focus_path: Path, items: list[dict[str, str]]) -> str:
    focus_path.parent.mkdir(parents=True, exist_ok=True)

    width, height = 1280, 82
    title_font = load_font(16, bold=True)
    label_font = load_font(18, bold=True)

    frames = []
    frame_count = max(9, len(items) * 3)

    for frame_index in range(frame_count):
        active_index = frame_index % len(items)
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image, "RGBA")

        draw.rounded_rectangle(
            (8, 8, width - 8, height - 8),
            radius=24,
            fill=(12, 22, 36, 230),
            outline=(255, 255, 255, 28),
            width=1,
        )

        draw.text((28, 20), "CURRENTLY EXPLORING", font=title_font, fill="#8DD4FF")

        x = 250
        for index, item in enumerate(items):
            label = item["label"]
            color = item["color"]

            fill_alpha = 118 if index == active_index else 50
            border_alpha = 150 if index == active_index else 66
            bbox = draw.textbbox((0, 0), label, font=label_font)
            text_width = bbox[2] - bbox[0]
            box_width = max(180, text_width + 44)

            draw.rounded_rectangle(
                (x, 16, x + box_width, 66),
                radius=18,
                fill=(23, 50, 82, fill_alpha),
                outline=(255, 255, 255, border_alpha),
                width=1,
            )
            draw.rounded_rectangle(
                (x + 14, 22, x + 14 + min(70, box_width - 28), 27),
                radius=3,
                fill=color if index == active_index else "#5b7fa6",
            )
            draw.text((x + 14, 39), label, font=label_font, fill="#EFF6FF")
            x += box_width + 18

        frames.append(image.convert("P", palette=Image.Palette.ADAPTIVE))

    frames[0].save(
        focus_path,
        save_all=True,
        append_images=frames[1:],
        duration=180,
        loop=0,
        optimize=False,
        disposal=2,
        transparency=0,
    )
    return hashlib.sha256(focus_path.read_bytes()).hexdigest()[:12]


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
    snapshot_svg_path = Path(args.snapshot_svg)
    focus_gif_path = Path(args.focus_gif)

    config = load_yaml(config_path)
    with template_path.open("r", encoding="utf-8") as fh:
        template_text = fh.read()

    username = config["profile"]["username"]
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    repos = fetch_repositories(username, token=token)
    repo_map = {repo["name"]: repo for repo in repos}

    focus_hash = write_focus_gif(focus_gif_path, config["focus_strip"])

    values = {
        "USERNAME": username,
        "FOCUS_STRIP_URL": f"./assets/generated/profile-focus.gif?v={focus_hash}",
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
        "SNAPSHOT_ROWS": build_snapshot_rows(repos, config),
        "TOP_LANGUAGES": build_top_languages(repos),
        "WORK_ITEMS": format_list(config["how_i_work"]),
        "TOOLBOX_BADGES": format_toolbox_badges(config["toolbox"]),
        "FIND_HERE_ITEMS": format_list(config["find_here"]),
        "FOOTER_ZH": config["profile"]["footer_zh"],
        "FOOTER_EN": config["profile"]["footer_en"],
    }

    output = render_template(template_text, values)
    with output_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(output.rstrip() + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
