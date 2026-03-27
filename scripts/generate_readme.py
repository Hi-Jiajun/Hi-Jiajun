from __future__ import annotations

import argparse
import json
import os
import re
import sys
from html import escape
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import parse, request
from collections import Counter

import yaml


PLACEHOLDER_PATTERN = re.compile(r"{{\s*([A-Z0-9_]+)\s*}}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate GitHub profile README from template and config.")
    parser.add_argument("--config", default="profile-data.yml")
    parser.add_argument("--template", default="README.template.md")
    parser.add_argument("--output", default="README.md")
    parser.add_argument("--snapshot-svg", default="assets/generated/profile-snapshot.svg")
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


def write_snapshot_svg(
    snapshot_path: Path,
    username: str,
    repos: list[dict[str, Any]],
    config: dict[str, Any],
) -> None:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)

    public_repo_count = len(repos)
    fork_repo_count = sum(1 for repo in repos if repo.get("fork"))
    original_tracked = len(config["projects"]["original"])
    total_stars = sum(int(repo.get("stargazers_count", 0)) for repo in repos)
    top_languages = collect_top_languages(repos, limit=5)
    max_count = max((count for _language, count in top_languages), default=1)

    cards = [
        ("Public repos", str(public_repo_count), "#38bdf8"),
        ("Fork repos", str(fork_repo_count), "#60a5fa"),
        ("Original work", str(original_tracked), "#2dd4bf"),
        ("Total stars", str(total_stars), "#f59e0b"),
    ]

    card_parts = []
    for index, (label, value, accent) in enumerate(cards):
        x = 56 + index * 185
        card_parts.append(
            f'''
  <g transform="translate({x} 92)">
    <rect width="165" height="92" rx="18" fill="rgba(255,255,255,0.05)"/>
    <rect x="0.75" y="0.75" width="163.5" height="90.5" rx="17.25" stroke="rgba(255,255,255,0.12)"/>
    <rect x="18" y="18" width="36" height="6" rx="3" fill="{accent}"/>
    <text x="18" y="48" fill="#E5F3FF" font-family="Segoe UI, Arial, sans-serif" font-size="28" font-weight="700">{escape(value)}</text>
    <text x="18" y="70" fill="#B9D4E8" font-family="Segoe UI, Arial, sans-serif" font-size="13">{escape(label)}</text>
  </g>'''
        )

    palette = ["#38bdf8", "#2dd4bf", "#f59e0b", "#a78bfa", "#f472b6"]
    language_parts = []
    for index, (language, count) in enumerate(top_languages):
        y = 246 + index * 44
        width = 420 if max_count == 0 else round((count / max_count) * 420)
        color = palette[index % len(palette)]
        language_parts.append(
            f'''
  <text x="780" y="{y}" fill="#E5F3FF" font-family="Segoe UI, Arial, sans-serif" font-size="14" font-weight="600">{escape(language)}</text>
  <text x="1188" y="{y}" text-anchor="end" fill="#B9D4E8" font-family="Segoe UI, Arial, sans-serif" font-size="13">{count} repos</text>
  <rect x="780" y="{y + 10}" width="420" height="10" rx="5" fill="rgba(255,255,255,0.08)"/>
  <rect x="780" y="{y + 10}" width="{width}" height="10" rx="5" fill="{color}"/>'''
        )

    if not language_parts:
        language_parts.append(
            '''
  <text x="780" y="262" fill="#B9D4E8" font-family="Segoe UI, Arial, sans-serif" font-size="14">No language data available yet.</text>'''
        )

    svg = f'''<svg width="1280" height="480" viewBox="0 0 1280 480" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="title desc">
  <title id="title">{escape(username)} profile snapshot</title>
  <desc id="desc">Auto-generated GitHub snapshot showing repository metrics and top languages.</desc>
  <defs>
    <linearGradient id="bg" x1="40" y1="24" x2="1240" y2="456" gradientUnits="userSpaceOnUse">
      <stop stop-color="#08121F"/>
      <stop offset="0.55" stop-color="#0F2236"/>
      <stop offset="1" stop-color="#154F59"/>
    </linearGradient>
    <radialGradient id="glowA" cx="0" cy="0" r="1" gradientUnits="userSpaceOnUse" gradientTransform="translate(280 96) rotate(18) scale(320 180)">
      <stop stop-color="#38BDF8" stop-opacity="0.20"/>
      <stop offset="1" stop-color="#38BDF8" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="glowB" cx="0" cy="0" r="1" gradientUnits="userSpaceOnUse" gradientTransform="translate(1060 92) rotate(160) scale(340 190)">
      <stop stop-color="#2DD4BF" stop-opacity="0.16"/>
      <stop offset="1" stop-color="#2DD4BF" stop-opacity="0"/>
    </radialGradient>
    <pattern id="grid" x="0" y="0" width="32" height="32" patternUnits="userSpaceOnUse">
      <path d="M32 0H0V32" stroke="white" stroke-opacity="0.04"/>
    </pattern>
  </defs>

  <rect x="8" y="8" width="1264" height="464" rx="28" fill="url(#bg)"/>
  <rect x="8" y="8" width="1264" height="464" rx="28" fill="url(#glowA)"/>
  <rect x="8" y="8" width="1264" height="464" rx="28" fill="url(#glowB)"/>
  <rect x="8" y="8" width="1264" height="464" rx="28" fill="url(#grid)"/>
  <rect x="8.5" y="8.5" width="1263" height="463" rx="27.5" stroke="rgba(255,255,255,0.12)"/>

  <text x="56" y="56" fill="#8DD4FF" font-family="Segoe UI, Arial, sans-serif" font-size="16" font-weight="700" letter-spacing="3">SNAPSHOT / AUTO-GENERATED</text>
  <text x="56" y="86" fill="#F8FAFC" font-family="Segoe UI, Arial, sans-serif" font-size="34" font-weight="700">GitHub activity at a glance</text>
  <text x="56" y="222" fill="#8DD4FF" font-family="Segoe UI, Arial, sans-serif" font-size="16" font-weight="700" letter-spacing="2">TOP LANGUAGES</text>
  <text x="780" y="56" fill="#B9D4E8" font-family="Segoe UI, Arial, sans-serif" font-size="15">Updated from public repository data</text>
{''.join(card_parts)}
{''.join(language_parts)}
  <text x="56" y="430" fill="#B9D4E8" font-family="Segoe UI, Arial, sans-serif" font-size="13">Generated by scripts/generate_readme.py</text>
  <text x="1224" y="430" text-anchor="end" fill="#B9D4E8" font-family="Segoe UI, Arial, sans-serif" font-size="13">{escape(format_date(datetime.now(timezone.utc).isoformat()))} UTC</text>
</svg>
'''

    snapshot_path.write_text(svg, encoding="utf-8", newline="\n")


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

    config = load_yaml(config_path)
    with template_path.open("r", encoding="utf-8") as fh:
        template_text = fh.read()

    username = config["profile"]["username"]
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    repos = fetch_repositories(username, token=token)
    repo_map = {repo["name"]: repo for repo in repos}

    values = {
        "USERNAME": username,
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

    write_snapshot_svg(snapshot_svg_path, username, repos, config)

    output = render_template(template_text, values)
    with output_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(output.rstrip() + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
