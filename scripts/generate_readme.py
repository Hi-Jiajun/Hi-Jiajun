from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import parse, request

import yaml


PLACEHOLDER_PATTERN = re.compile(r"{{\s*([A-Z0-9_]+)\s*}}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate GitHub profile README from template and config.")
    parser.add_argument("--config", default="profile-data.yml")
    parser.add_argument("--template", default="README.template.md")
    parser.add_argument("--output", default="README.md")
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
