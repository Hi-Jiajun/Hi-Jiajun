from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError

import yaml


PLACEHOLDER_PATTERN = re.compile(r"{{\s*([A-Z0-9_]+)\s*}}")
AUTO_GENERATED_NOTICE = (
    "<!-- AUTO-GENERATED FROM README.template.md AND profile-data.yml. DO NOT EDIT DIRECTLY. -->\n"
)
CARD_FONT_FAMILY = "Segoe UI, Helvetica Neue, Arial, sans-serif"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate GitHub profile README from template and config.")
    parser.add_argument("--config", default="profile-data.yml")
    parser.add_argument("--template", default="README.template.md")
    parser.add_argument("--output", default="README.md")
    parser.add_argument("--cards-dir", default="assets/generated/cards")
    parser.add_argument("--sponsors-dir", default="assets/generated/sponsors")
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


def format_date(value: str | None) -> str:
    if not value:
        return "-"
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def format_about(items: list[dict[str, str]]) -> str:
    lines = []
    for item in items:
        lines.append(f"- {item['icon']} {item['zh']}<br/>\n  <sub>{item['en']}</sub>")
    return "\n".join(lines)


def format_toolbox_badges(items: list[dict[str, str]]) -> str:
    badges = []
    for item in items:
        label = parse.quote(item["label"])
        message = parse.quote(item["message"])
        color = item["color"]
        params = ["labelColor=0D1B2A", "style=for-the-badge"]
        logo = item.get("logo")
        if logo:
            params.append(f"logo={parse.quote(logo)}")
            logo_color = item.get("logo_color") or "F8FAFC"
            params.append(f"logoColor={logo_color}")
        query = "&".join(params)
        alt = escape(item["label"])
        badges.append(
            f'  <img src="https://img.shields.io/badge/{label}-{message}-{color}?{query}" alt="{alt}" />'
        )
    return "\n".join(badges)


def truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def render_card_svg(
    repo_name: str,
    category_label: str,
    note_zh: str,
    note_en: str,
    language: str,
    stars: int,
    pushed: str,
) -> str:
    title = truncate(repo_name, 32)
    zh = truncate(note_zh, 28)
    en = truncate(note_en, 66)
    stats = f"⭐ {stars} · updated {pushed}"
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 440 140" width="440" height="140" role="img" aria-label="{escape(repo_name)}">
  <style>
    .card {{ fill: #ffffff; stroke: #e2e8f0; }}
    .tag {{ fill: #0284c7; font-weight: 700; letter-spacing: 1.5px; }}
    .title {{ fill: #0f172a; font-weight: 700; }}
    .note-zh {{ fill: #334155; }}
    .note-en {{ fill: #94a3b8; }}
    .stats {{ fill: #475569; }}
    @media (prefers-color-scheme: dark) {{
      .card {{ fill: #0D1B2A; stroke: #184B54; }}
      .tag {{ fill: #38BDF8; }}
      .title {{ fill: #F8FAFC; }}
      .note-zh {{ fill: #D4E7F5; }}
      .note-en {{ fill: #8DD4FF; }}
      .stats {{ fill: #C7DCEF; }}
    }}
  </style>
  <rect class="card" x="0.5" y="0.5" width="439" height="139" rx="16" stroke-width="1" />
  <text x="18" y="28" class="tag" font-family="{CARD_FONT_FAMILY}" font-size="12">{escape(category_label)}</text>
  <text x="422" y="28" class="stats" font-family="{CARD_FONT_FAMILY}" font-size="12" text-anchor="end">{escape(language)}</text>
  <text x="18" y="58" class="title" font-family="{CARD_FONT_FAMILY}" font-size="20">{escape(title)}</text>
  <text x="18" y="82" class="note-zh" font-family="{CARD_FONT_FAMILY}" font-size="13">{escape(zh)}</text>
  <text x="18" y="100" class="note-en" font-family="{CARD_FONT_FAMILY}" font-size="11">{escape(en)}</text>
  <text x="18" y="122" class="stats" font-family="{CARD_FONT_FAMILY}" font-size="11">{escape(stats)}</text>
</svg>
'''


def write_if_changed(path: Path, content: str) -> str:
    new_bytes = content.encode("utf-8")
    digest = hashlib.sha256(new_bytes).hexdigest()[:12]
    if path.exists() and path.read_bytes() == new_bytes:
        return digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(new_bytes)
    return digest


def render_project_section(
    entries: list[dict[str, str]],
    repo_map: dict[str, dict[str, Any]],
    cards_dir: Path,
    username: str,
    category_label: str,
) -> str:
    cells: list[str] = []
    for entry in entries:
        repo_name = entry["repo"]
        repo = repo_map.get(repo_name)
        repo_url = repo["html_url"] if repo else f"https://github.com/{username}/{repo_name}"
        language = (repo.get("language") if repo else None) or "—"
        stars = int(repo.get("stargazers_count", 0)) if repo else 0
        pushed = format_date(repo.get("pushed_at") if repo else None)

        svg = render_card_svg(
            repo_name=repo_name,
            category_label=category_label,
            note_zh=entry["note_zh"],
            note_en=entry["note_en"],
            language=language,
            stars=stars,
            pushed=pushed,
        )
        card_path = cards_dir / f"{repo_name}.svg"
        card_hash = write_if_changed(card_path, svg)
        img_src = f"./{card_path.as_posix()}?v={card_hash}"
        cells.append(
            f'<td><a href="{escape(repo_url)}"><img src="{img_src}" alt="{escape(repo_name)}" /></a></td>'
        )

    rows = []
    for i in range(0, len(cells), 2):
        pair = cells[i : i + 2]
        rows.append("  <tr>\n    " + "\n    ".join(pair) + "\n  </tr>")
    return "<table>\n" + "\n".join(rows) + "\n</table>"


def prune_stale_cards(cards_dir: Path, expected_names: set[str]) -> None:
    if not cards_dir.exists():
        return
    for existing in cards_dir.glob("*.svg"):
        if existing.stem not in expected_names:
            existing.unlink()


MIME_BY_EXT = {".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif"}


def fetch_qr_image(url: str) -> tuple[bytes, str]:
    req = request.Request(
        url,
        headers={"User-Agent": "Hi-Jiajun-profile-readme-generator"},
    )
    with request.urlopen(req) as response:
        body = response.read()
        content_type = response.headers.get_content_type() or ""
    if not content_type.startswith("image/"):
        ext = Path(parse.urlparse(url).path).suffix.lower()
        content_type = MIME_BY_EXT.get(ext, "application/octet-stream")
    return body, content_type


def render_sponsor_card_svg(
    name_zh: str,
    name_en: str,
    brand_color: str,
    brand_color_dark: str,
    qr_bytes: bytes,
    qr_mime: str,
) -> str:
    b64 = base64.b64encode(qr_bytes).decode("ascii")
    label = f"{name_zh} · {name_en}"
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 260 320" width="260" height="320" role="img" aria-label="{escape(name_en)} QR code">
  <style>
    .card {{ fill: #ffffff; stroke: #e2e8f0; }}
    .title {{ fill: #{brand_color}; font-weight: 700; }}
    @media (prefers-color-scheme: dark) {{
      .card {{ fill: #0D1B2A; stroke: #184B54; }}
      .title {{ fill: #{brand_color_dark}; }}
    }}
  </style>
  <rect class="card" x="0.5" y="0.5" width="259" height="319" rx="16" stroke-width="1" />
  <image href="data:{qr_mime};base64,{b64}" x="20" y="30" width="220" height="220" preserveAspectRatio="xMidYMid meet" />
  <text x="130" y="290" class="title" text-anchor="middle" font-family="{CARD_FONT_FAMILY}" font-size="18">{escape(label)}</text>
</svg>
'''


def render_sponsor_section(methods: list[dict[str, str]], sponsors_dir: Path) -> str:
    cells: list[str] = []
    expected: set[str] = set()
    for method in methods:
        slug = method["slug"]
        expected.add(slug)
        qr_bytes, qr_mime = fetch_qr_image(method["qr_url"])
        svg = render_sponsor_card_svg(
            name_zh=method["name_zh"],
            name_en=method["name_en"],
            brand_color=method["brand_color"],
            brand_color_dark=method.get("brand_color_dark", method["brand_color"]),
            qr_bytes=qr_bytes,
            qr_mime=qr_mime,
        )
        card_path = sponsors_dir / f"{slug}.svg"
        card_hash = write_if_changed(card_path, svg)
        img_src = f"./{card_path.as_posix()}?v={card_hash}"
        cells.append(
            f'<td align="center" width="50%"><img src="{img_src}" alt="{escape(method["name_en"])} sponsor QR" /></td>'
        )

    if sponsors_dir.exists():
        for existing in sponsors_dir.glob("*.svg"):
            if existing.stem not in expected:
                existing.unlink()

    return "<table>\n  <tr>\n    " + "\n    ".join(cells) + "\n  </tr>\n</table>"


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
    cards_dir = Path(args.cards_dir)
    sponsors_dir = Path(args.sponsors_dir)

    config = load_yaml(config_path)
    with template_path.open("r", encoding="utf-8") as fh:
        template_text = fh.read()

    username = config["profile"]["username"]
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    repos = fetch_repositories(username, token=token)
    repo_map = {repo["name"]: repo for repo in repos}

    expected_card_names: set[str] = {
        entry["repo"] for entry in config["projects"]["original"]
    } | {entry["repo"] for entry in config["projects"]["forks"]}
    prune_stale_cards(cards_dir, expected_card_names)

    values = {
        "USERNAME": username,
        "HEADER_TITLE": config["profile"]["header_title"],
        "HEADER_ROLE_ZH": config["profile"]["header_role_zh"],
        "HEADER_ROLE_EN": config["profile"]["header_role_en"],
        "HEADER_INTRO_ZH": config["profile"]["header_intro_zh"],
        "HEADER_INTRO_EN": config["profile"]["header_intro_en"],
        "OWNERSHIP_NOTE_ZH": config["profile"]["ownership_note_zh"],
        "OWNERSHIP_NOTE_EN": config["profile"]["ownership_note_en"],
        "ABOUT_ITEMS": format_about(config["about"]),
        "ORIGINAL_CARDS": render_project_section(
            config["projects"]["original"], repo_map, cards_dir, username, "🛠 ORIGINAL"
        ),
        "FORK_CARDS": render_project_section(
            config["projects"]["forks"], repo_map, cards_dir, username, "🍴 FORK"
        ),
        "TOOLBOX_BADGES": format_toolbox_badges(config["toolbox"]),
        "SPONSOR_CARDS": render_sponsor_section(config["sponsor"]["methods"], sponsors_dir),
        "FOOTER_ZH": config["profile"]["footer_zh"],
        "FOOTER_EN": config["profile"]["footer_en"],
    }

    output = AUTO_GENERATED_NOTICE + render_template(template_text, values)
    with output_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(output.rstrip() + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
