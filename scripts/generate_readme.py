from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import escape, unescape
from pathlib import Path
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError, URLError

import yaml


PLACEHOLDER_PATTERN = re.compile(r"{{\s*([A-Z0-9_]+)\s*}}")
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
AUTO_GENERATED_NOTICE = (
    "<!-- AUTO-GENERATED FROM README.template.md AND profile-data.yml. DO NOT EDIT DIRECTLY. -->\n"
)
CARD_FONT_FAMILY = "Segoe UI, Helvetica Neue, Arial, sans-serif"
MONO_FONT_FAMILY = "Cascadia Code, Consolas, monospace"
HTTP_TIMEOUT = 15  # seconds
MAX_RETRY = 3
MAX_PAGES = 20  # safety valve for pagination

DEFAULT_LANG_COLORS: dict[str, str] = {
    "C++": "#F34B7D",
    "C": "#64748B",
    "Rust": "#DEA584",
    "Python": "#3572A5",
    "JavaScript": "#EAB308",
    "TypeScript": "#3178C6",
    "Shell": "#22C55E",
    "Java": "#B07219",
    "Svelte": "#FF3E00",
    "Swift": "#F05138",
    "Kotlin": "#A97BFF",
    "Go": "#00ADD8",
    "Astro": "#FF5A03",
    "DTS": "#2DD4BF",
}

DARK_LANG_COLOR_OVERRIDES: dict[str, str] = {
    "C": "#60A5FA",
    "Python": "#38BDF8",
    "Shell": "#4ADE80",
    "JavaScript": "#FACC15",
}

LIGHT_LANG_COLOR_OVERRIDES: dict[str, str] = {
    "C": "#475569",
    "Shell": "#16A34A",
    "JavaScript": "#CA8A04",
}

MIME_BY_EXT = {
    ".webp": "image/webp",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
}

# Crisp 16x16 Octicon vector paths (MIT licensed by GitHub Primer Octicons)
OCTICON_REPO = (
    "M2 2.5A2.5 2.5 0 0 1 4.5 0h8.75a.75.75 0 0 1 .75.75v12.5a.75.75 0 0 1-.75.75h-2.5a.75.75 0 0 1 0-1.5"
    "h1.75v-2h-8a1 1 0 0 0-.714 1.7.75.75 0 1 1-1.072 1.05A2.495 2.495 0 0 1 2 11.5Zm10.5-1h-8a1 1 0 0 0-1 1"
    "v6.708A2.486 2.486 0 0 1 4.5 9h8ZM5 12.25a.25.25 0 0 1 .25-.25h3.5a.25.25 0 0 1 .25.25v3.25a.25.25 0 0 1"
    "-.4.2l-1.45-1.087a.249.249 0 0 0-.3 0L5.4 15.7a.25.25 0 0 1-.4-.2Z"
)
OCTICON_STAR = (
    "M8 .25a.75.75 0 0 1 .673.418l1.882 3.815 4.21.612a.75.75 0 0 1 .416 1.279l-3.046 2.97.719 4.192a.751.751"
    " 0 0 1-1.088.791L8 12.347l-3.766 1.98a.75.75 0 0 1-1.088-.79l.72-4.194L.818 6.374a.75.75 0 0 1 .416-1.28"
    "l4.21-.611L7.327.668A.75.75 0 0 1 8 .25Z"
)
OCTICON_FORK = (
    "M5 5.372v.878c0 .414.336.75.75.75h4.5a.75.75 0 0 0 .75-.75v-.878a2.25 2.25 0 1 1 1.5 0v.878a2.25 2.25"
    " 0 0 1-2.25 2.25h-1.5v2.128a2.251 2.251 0 1 1-1.5 0V8.5h-1.5A2.25 2.25 0 0 1 3.5 6.25v-.878a2.25 2.25"
    " 0 1 1 1.5 0ZM5 3.25a.75.75 0 1 0-1.5 0 .75.75 0 0 0 1.5 0Zm6.75.75a.75.75 0 1 0 0-1.5.75.75 0 0 0 0 1.5Z"
    "m-3 8.75a.75.75 0 1 0-1.5 0 .75.75 0 0 0 1.5 0Z"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate GitHub profile README from template and config.")
    parser.add_argument("--config", default="profile-data.yml")
    parser.add_argument("--template", default="README.template.md")
    parser.add_argument("--output", default="README.md")
    parser.add_argument("--cards-dir", default="assets/generated/cards")
    parser.add_argument("--sponsors-dir", default="assets/generated/sponsors")
    parser.add_argument("--stats-dir", default="assets/generated/stats")
    parser.add_argument("--rss-file", default=None, help="Optional local RSS XML file path for offline testing")
    return parser.parse_args()


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a mapping in {path}")
    return data


def github_request(
    url: str,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, str]]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Hi-Jiajun-profile-readme-generator",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    data_bytes: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data_bytes = json.dumps(payload).encode("utf-8")

    req = request.Request(url, data=data_bytes, headers=headers, method="POST" if data_bytes else "GET")
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            with request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
                body = response.read().decode("utf-8")
                header_map = {k: v for k, v in response.headers.items()}
            return json.loads(body), header_map
        except HTTPError as exc:
            exc_headers = exc.headers or {}
            remaining = exc_headers.get("X-RateLimit-Remaining")
            reset = exc_headers.get("X-RateLimit-Reset")
            detail = f" (rate limit remaining={remaining}, reset={reset})" if remaining is not None else ""
            if exc.code < 500 and exc.code != 429:
                raise RuntimeError(f"GitHub API {exc.code} on {url}{detail}: {exc.reason}") from exc
            last_exc = exc
            if attempt < MAX_RETRY:
                time.sleep(2 ** attempt)
        except (URLError, OSError) as exc:
            last_exc = exc
            if attempt < MAX_RETRY:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"GitHub API failed after {MAX_RETRY} attempts on {url}: {last_exc}") from last_exc


def fetch_repositories(username: str, token: str | None = None) -> list[dict[str, Any]]:
    repos: list[dict[str, Any]] = []
    page = 1

    while page <= MAX_PAGES:
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


def fetch_single_repo(username: str, repo_name: str, token: str | None = None) -> dict[str, Any] | None:
    """Fetch metadata for a single repository. Returns None if not found."""
    url = f"https://api.github.com/repos/{username}/{repo_name}"
    try:
        data, _headers = github_request(url, token=token)
        return data if isinstance(data, dict) else None
    except RuntimeError as exc:
        if "404" in str(exc):
            return None
        raise


def format_date(value: str | None) -> str:
    if not value:
        return "-"
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def make_theme_badge(
    label: str,
    message: str,
    color_dark: str,
    color_light: str,
    logo: str | None = None,
    logo_color_dark: str = "F8FAFC",
    logo_color_light: str = "0D1B2A",
    href: str | None = None,
) -> str:
    q_label = parse.quote(label.replace("-", "--"))
    q_message = parse.quote(message.replace("-", "--"))
    alt = escape(label, quote=True)

    params_dark = ["labelColor=0D1B2A", "style=flat"]
    params_light = ["labelColor=F0F6FC", "style=flat"]
    if logo:
        q_logo = parse.quote(logo)
        params_dark.extend([f"logo={q_logo}", f"logoColor={logo_color_dark}"])
        params_light.extend([f"logo={q_logo}", f"logoColor={logo_color_light}"])

    dark_url = f"https://img.shields.io/badge/{q_label}-{q_message}-{color_dark}?{'&'.join(params_dark)}"
    light_url = f"https://img.shields.io/badge/{q_label}-{q_message}-{color_light}?{'&'.join(params_light)}"
    picture = (
        f'<picture>'
        f'<source media="(prefers-color-scheme: dark)" srcset="{dark_url}" />'
        f'<img src="{light_url}" alt="{alt}" />'
        f'</picture>'
    )
    if href:
        return f'  <a href="{escape(href, quote=True)}">{picture}</a>'
    return f"  {picture}"


def format_quick_links(profile: dict[str, Any]) -> str:
    links: list[str] = []
    blog_url = profile.get("blog_url")
    if blog_url:
        blog_host = parse.urlparse(blog_url).netloc or blog_url
        links.append(
            make_theme_badge(
                label="Blog",
                message=blog_host,
                color_dark="0369A1",
                color_light="0284C7",
                logo="astro",
                logo_color_dark="38BDF8",
                logo_color_light="FFFFFF",
                href=blog_url,
            )
        )
    bilibili_url = profile.get("bilibili_url")
    if bilibili_url:
        links.append(
            make_theme_badge(
                label="Bilibili",
                message=profile.get("bilibili_label") or "Profile",
                color_dark="BE185D",
                color_light="FB7299",
                logo="bilibili",
                logo_color_dark="F9A8D4",
                logo_color_light="FFFFFF",
                href=bilibili_url,
            )
        )
    rss_url = profile.get("rss_url")
    if rss_url:
        links.append(
            make_theme_badge(
                label="RSS",
                message="feed",
                color_dark="0F766E",
                color_light="0D9488",
                logo="rss",
                logo_color_dark="2DD4BF",
                logo_color_light="FFFFFF",
                href=rss_url,
            )
        )
    email = profile.get("email")
    if email:
        links.append(
            make_theme_badge(
                label="Email",
                message=email,
                color_dark="1E293B",
                color_light="475569",
                logo="maildotru",
                logo_color_dark="38BDF8",
                logo_color_light="FFFFFF",
                href=f"mailto:{email}",
            )
        )
    return "\n".join(links)


def format_toolbox_badges(items: list[dict[str, str]]) -> str:
    badges = []
    for item in items:
        badges.append(
            make_theme_badge(
                label=item["label"],
                message=item["message"],
                color_dark=item["color"],
                color_light=item.get("color_light", item["color"]),
                logo=item.get("logo"),
                logo_color_dark=item.get("logo_color") or "F8FAFC",
                logo_color_light=item.get("logo_color_light") or "0D1B2A",
            )
        )
    return "\n".join(badges)


def truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def resolve_lang_color(language: str, theme_mode: str) -> str:
    primary = language.split("/")[0].strip()
    if theme_mode == "dark" and primary in DARK_LANG_COLOR_OVERRIDES:
        return DARK_LANG_COLOR_OVERRIDES[primary]
    if theme_mode == "light" and primary in LIGHT_LANG_COLOR_OVERRIDES:
        return LIGHT_LANG_COLOR_OVERRIDES[primary]
    return DEFAULT_LANG_COLORS.get(primary, "#38BDF8" if theme_mode == "dark" else "#0284C7")


def render_card_svg(
    repo_name: str,
    category_label: str,
    note_zh: str,
    note_en: str,
    language: str,
    stars: int,
    forks: int,
    pushed: str,
    theme_mode: str,
    accent_hex: str,
) -> str:
    if theme_mode == "dark":
        bg = "#0B1526"
        border = "#1E3A5F"
        pill_bg = "#112236"
        title_c = "#F8FAFC"
        zh_c = "#CBD5E1"
        en_c = "#8B9BB4"
        stats_c = "#94A3B8"
    else:
        bg = "#FFFFFF"
        border = "#CBD5E1"
        pill_bg = "#F1F5F9"
        title_c = "#0F172A"
        zh_c = "#334155"
        en_c = "#64748B"
        stats_c = "#475569"

    accent = f"#{accent_hex.lstrip('#')}"
    lang_dot = resolve_lang_color(language, theme_mode)
    safe_slug = re.sub(r"[^A-Za-z0-9_-]", "-", repo_name)
    clip_id = f"card-clip-{safe_slug}-{theme_mode}"

    title = truncate(repo_name, 28)
    zh = truncate(note_zh, 34)
    en = truncate(note_en, 60)
    pill_w = max(84, int(len(category_label) * 7.2 + 18))
    lang_dot_x = round(412 - max(len(language) * 6.8, 14) - 10, 1)

    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 430 148" width="430" height="148" role="img" aria-label="{escape(repo_name, quote=True)}">
  <defs>
    <clipPath id="{clip_id}">
      <rect x="0.5" y="0.5" width="429" height="147" rx="14" />
    </clipPath>
  </defs>
  <rect x="0.5" y="0.5" width="429" height="147" rx="14" fill="{bg}" stroke="{border}" stroke-width="1" />
  <rect x="0" y="0" width="430" height="3.5" fill="{accent}" clip-path="url(#{clip_id})" />
  <rect x="18" y="16" width="{pill_w}" height="20" rx="10" fill="{pill_bg}" stroke="{accent}" stroke-opacity="0.45" stroke-width="0.9" />
  <text x="{18 + pill_w / 2:.1f}" y="29.5" fill="{accent}" font-family="{MONO_FONT_FAMILY}" font-size="10" font-weight="700" letter-spacing="0.7" text-anchor="middle">{escape(category_label)}</text>
  <circle cx="{lang_dot_x}" cy="26" r="4.5" fill="{lang_dot}" />
  <text x="412" y="29.5" fill="{stats_c}" font-family="{CARD_FONT_FAMILY}" font-size="11.5" font-weight="600" text-anchor="end">{escape(language)}</text>
  <g transform="translate(18, 47)" fill="{accent}">
    <path d="{OCTICON_REPO}" />
  </g>
  <text x="41" y="60.5" fill="{title_c}" font-family="{CARD_FONT_FAMILY}" font-size="18.5" font-weight="700">{escape(title)}</text>
  <text x="18" y="86" fill="{zh_c}" font-family="{CARD_FONT_FAMILY}" font-size="12.8">{escape(zh)}</text>
  <text x="18" y="106" fill="{en_c}" font-family="{CARD_FONT_FAMILY}" font-size="11">{escape(en)}</text>
  <g transform="translate(18, 120) scale(0.82)" fill="{stats_c}">
    <path d="{OCTICON_STAR}" />
  </g>
  <text x="35" y="130.5" fill="{stats_c}" font-family="{MONO_FONT_FAMILY}" font-size="11">{stars}</text>
  <g transform="translate(72, 120) scale(0.82)" fill="{stats_c}">
    <path d="{OCTICON_FORK}" />
  </g>
  <text x="88" y="130.5" fill="{stats_c}" font-family="{MONO_FONT_FAMILY}" font-size="11">{forks}</text>
  <text x="412" y="130.5" fill="{en_c}" font-family="{MONO_FONT_FAMILY}" font-size="10.5" text-anchor="end">updated {escape(pushed)}</text>
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


def render_project_cards_bento(
    entries: list[dict[str, str]],
    repo_map: dict[str, dict[str, Any]],
    cards_dir: Path,
    username: str,
    accent_dark: str,
    accent_light: str,
    default_tag: str = "ORIGINAL",
) -> str:
    cards: list[str] = []
    for entry in entries:
        repo_name = entry["repo"]
        repo = repo_map.get(repo_name)
        repo_url = repo["html_url"] if repo else f"https://github.com/{username}/{repo_name}"
        language = entry.get("language") or (repo.get("language") if repo else None) or "—"
        stars = int(repo.get("stargazers_count", 0)) if repo else 0
        forks = int(repo.get("forks_count", 0)) if repo else 0
        pushed = format_date(repo.get("pushed_at") if repo else None)
        tag = entry.get("tag") or default_tag

        dark_svg = render_card_svg(
            repo_name=repo_name,
            category_label=tag,
            note_zh=entry["note_zh"],
            note_en=entry["note_en"],
            language=language,
            stars=stars,
            forks=forks,
            pushed=pushed,
            theme_mode="dark",
            accent_hex=accent_dark,
        )
        light_svg = render_card_svg(
            repo_name=repo_name,
            category_label=tag,
            note_zh=entry["note_zh"],
            note_en=entry["note_en"],
            language=language,
            stars=stars,
            forks=forks,
            pushed=pushed,
            theme_mode="light",
            accent_hex=accent_light,
        )

        dark_path = cards_dir / f"{repo_name}-dark.svg"
        light_path = cards_dir / f"{repo_name}-light.svg"
        dark_hash = write_if_changed(dark_path, dark_svg)
        light_hash = write_if_changed(light_path, light_svg)

        cards.append(
            f'  <a href="{escape(repo_url, quote=True)}"><picture>'
            f'<source media="(prefers-color-scheme: dark)" srcset="./{dark_path.as_posix()}?v={dark_hash}" />'
            f'<img width="430" src="./{light_path.as_posix()}?v={light_hash}" alt="{escape(repo_name, quote=True)}" />'
            f'</picture></a>'
        )

    rows: list[str] = []
    for i in range(0, len(cards), 2):
        pair = "\n".join(cards[i : i + 2])
        rows.append(f'<p align="center">\n{pair}\n</p>')
    return "\n\n".join(rows)


def render_projects_blocks(
    projects_cfg: dict[str, Any],
    repo_map: dict[str, dict[str, Any]],
    cards_dir: Path,
    username: str,
) -> tuple[str, str]:
    categories = projects_cfg.get("categories")
    if categories:
        sections: list[str] = []
        for cat in categories:
            heading = f"#### {cat['title_zh']} <sub>{cat['title_en']}</sub>"
            bento_html = render_project_cards_bento(
                cat["items"],
                repo_map,
                cards_dir,
                username,
                accent_dark=cat.get("accent_dark", "38BDF8"),
                accent_light=cat.get("accent_light", "0284C7"),
            )
            sections.append(f"{heading}\n\n{bento_html}")
        original_html = "\n\n".join(sections)
    else:
        original_html = render_project_cards_bento(
            projects_cfg.get("original", []),
            repo_map,
            cards_dir,
            username,
            accent_dark="38BDF8",
            accent_light="0284C7",
        )

    more_entries = projects_cfg.get("more", [])
    if not more_entries:
        return original_html, ""

    lines = [
        "<details>",
        f"<summary><b>更多开源与实验仓库 · More experiments ({len(more_entries)})</b></summary>\n",
    ]
    for entry in more_entries:
        repo_name = entry["repo"]
        repo = repo_map.get(repo_name)
        repo_url = repo["html_url"] if repo else f"https://github.com/{username}/{repo_name}"
        lang = entry.get("language") or (repo.get("language") if repo else None)
        stars = int(repo.get("stargazers_count", 0)) if repo else 0
        meta_parts = [p for p in [lang, f"⭐ {stars}" if stars else None] if p]
        meta_suffix = f" ({', '.join(meta_parts)})" if meta_parts else ""
        lines.append(f"- [`{repo_name}`]({repo_url}){meta_suffix} — {entry['note_zh']}")
    lines.append("\n</details>")
    return original_html, "\n".join(lines)


def prune_stale_cards(cards_dir: Path, expected_repos: set[str]) -> None:
    if not cards_dir.exists():
        return
    expected_stems = {f"{repo}-dark" for repo in expected_repos} | {f"{repo}-light" for repo in expected_repos}
    for existing in cards_dir.glob("*.svg"):
        if existing.stem not in expected_stems:
            existing.unlink()


def fetch_qr_image(url: str, cache_dir: Path | None = None) -> tuple[bytes, str]:
    """Download a QR image with local caching to avoid redundant fetches."""
    if cache_dir:
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
        cache_path = cache_dir / url_hash
        meta_path = cache_dir / f"{url_hash}.meta"
        if cache_path.exists() and meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("url") == url:
                return cache_path.read_bytes(), meta["content_type"]

    req = request.Request(
        url,
        headers={"User-Agent": "Hi-Jiajun-profile-readme-generator"},
    )
    with request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
        body = response.read()
        content_type = response.headers.get_content_type() or ""
    if not content_type.startswith("image/"):
        ext = Path(parse.urlparse(url).path).suffix.lower()
        content_type = MIME_BY_EXT.get(ext, "application/octet-stream")

    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(body)
        meta_path.write_text(json.dumps({"url": url, "content_type": content_type}), encoding="utf-8")

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
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 260 320" width="260" height="320" role="img" aria-label="{escape(name_en, quote=True)} QR code">
  <style>
    .card {{ fill: #FFFFFF; stroke: #cbd5e1; }}
    .title {{ fill: #{brand_color}; font-weight: 700; }}
    @media (prefers-color-scheme: dark) {{
      .card {{ fill: #0B1526; stroke: #1E3A5F; }}
      .title {{ fill: #{brand_color_dark}; }}
    }}
  </style>
  <rect class="card" x="0.5" y="0.5" width="259" height="319" rx="14" stroke-width="1" />
  <image href="data:{qr_mime};base64,{b64}" x="20" y="30" width="220" height="220" preserveAspectRatio="xMidYMid meet" />
  <text x="130" y="290" class="title" text-anchor="middle" font-family="{CARD_FONT_FAMILY}" font-size="18">{escape(label)}</text>
</svg>
'''


def render_sponsor_section(methods: list[dict[str, str]], sponsors_dir: Path) -> str:
    cells: list[str] = []
    expected: set[str] = set()
    qr_cache_dir = sponsors_dir / ".qr_cache"
    for method in methods:
        slug = method["slug"]
        expected.add(slug)
        qr_bytes, qr_mime = fetch_qr_image(method["qr_url"], cache_dir=qr_cache_dir)
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
            f'<td align="center" width="50%"><img src="{img_src}" alt="{escape(method["name_en"], quote=True)} sponsor QR" /></td>'
        )

    if sponsors_dir.exists():
        for existing in sponsors_dir.glob("*.svg"):
            if existing.stem not in expected:
                existing.unlink()

    return "<table>\n  <tr>\n    " + "\n    ".join(cells) + "\n  </tr>\n</table>"


def parse_rss_date(raw: str | None) -> str:
    if not raw:
        return "-"
    cleaned = raw.strip()
    cst = timezone(timedelta(hours=8))
    try:
        dt = parsedate_to_datetime(cleaned)
        return dt.astimezone(cst).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        return dt.astimezone(cst).strftime("%Y-%m-%d")
    except ValueError:
        return cleaned[:10] if len(cleaned) >= 10 else cleaned


def clean_rss_text(raw: str | None) -> str:
    if not raw:
        return ""
    text = HTML_TAG_PATTERN.sub(" ", unescape(raw))
    return re.sub(r"\s+", " ", text).strip()


def fetch_blog_posts(
    rss_url: str | None,
    rss_file: str | None = None,
    limit: int = 5,
) -> list[dict[str, str]]:
    raw_xml = ""
    if rss_file:
        raw_xml = Path(rss_file).read_text(encoding="utf-8")
    elif rss_url:
        req = request.Request(
            rss_url,
            headers={"User-Agent": "Hi-Jiajun-profile-readme-generator"},
        )
        try:
            with request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
                raw_xml = response.read().decode("utf-8")
        except (HTTPError, URLError, OSError) as exc:
            print(f"Warning: failed to fetch blog RSS from {rss_url}: {exc}", file=sys.stderr)
            return []
    else:
        return []

    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as exc:
        print(f"Warning: failed to parse RSS XML: {exc}", file=sys.stderr)
        return []

    posts: list[dict[str, str]] = []
    for item in root.findall("./channel/item")[:limit]:
        title = clean_rss_text(item.findtext("title"))
        link = (item.findtext("link") or "").strip()
        date_str = parse_rss_date(item.findtext("pubDate"))
        desc = truncate(clean_rss_text(item.findtext("description")), 92)
        if title and link:
            posts.append({"title": title, "link": link, "date": date_str, "desc": desc})

    if posts:
        return posts

    atom_ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("./atom:entry", atom_ns)[:limit]:
        title = clean_rss_text(entry.findtext("atom:title", default="", namespaces=atom_ns))
        link_el = entry.find("atom:link", atom_ns)
        link = (link_el.attrib.get("href", "") if link_el is not None else "").strip()
        date_str = parse_rss_date(
            entry.findtext("atom:published", default="", namespaces=atom_ns)
            or entry.findtext("atom:updated", default="", namespaces=atom_ns)
        )
        desc = truncate(
            clean_rss_text(entry.findtext("atom:summary", default="", namespaces=atom_ns)), 92
        )
        if title and link:
            posts.append({"title": title, "link": link, "date": date_str, "desc": desc})

    return posts


def format_blog_section(posts: list[dict[str, str]], blog_url: str | None) -> str:
    lines: list[str] = []
    if posts:
        for post in posts:
            safe_title = post["title"].replace("[", "\\[").replace("]", "\\]")
            desc_part = f" — {post['desc']}" if post["desc"] else ""
            lines.append(f"- `{post['date']}` **[{safe_title}]({post['link']})**{desc_part}")
    if blog_url:
        host = parse.urlparse(blog_url).netloc or blog_url
        if lines:
            lines.append("")
        lines.append(f"→ [进入博客浏览全部文章、项目矩阵与站内搜索 ({host})]({blog_url})")
    return "\n".join(lines) if lines else "_暂无已同步的文章_"


def fetch_user_stats(
    username: str,
    exclude_repos: set[str],
    token: str | None = None,
) -> dict[str, Any]:
    """Fetch user metrics and language breakdown via GraphQL (with REST fallback)."""
    if token:
        query = """
        query($login: String!) {
          user(login: $login) {
            name
            login
            followers { totalCount }
            pullRequests { totalCount }
            issues { totalCount }
            contributionsCollection {
              totalCommitContributions
              totalRepositoriesWithContributedCommits
            }
            allPublicRepos: repositories(ownerAffiliations: OWNER, privacy: PUBLIC, first: 100) {
              nodes {
                stargazerCount
              }
            }
            originalRepos: repositories(ownerAffiliations: OWNER, isFork: false, privacy: PUBLIC, first: 100, orderBy: {field: PUSHED_AT, direction: DESC}) {
              totalCount
              nodes {
                name
                stargazerCount
                forkCount
                languages(first: 8, orderBy: {field: SIZE, direction: DESC}) {
                  edges {
                    size
                    node {
                      name
                      color
                    }
                  }
                }
              }
            }
          }
        }
        """
        try:
            data, _ = github_request(
                "https://api.github.com/graphql",
                token=token,
                payload={"query": query, "variables": {"login": username}},
            )
        except RuntimeError as exc:
            print(f"Warning: GraphQL stats query failed, falling back to REST: {exc}", file=sys.stderr)
            data = None

        if isinstance(data, dict) and data.get("data", {}).get("user") and not data.get("errors"):
            user = data["data"]["user"]
            total_stars = sum(int(n["stargazerCount"]) for n in user["allPublicRepos"]["nodes"])
            orig_nodes = user["originalRepos"]["nodes"]
            lang_weights: dict[str, float] = {}
            lang_colors: dict[str, str] = {}

            for node in orig_nodes:
                if node["name"] in exclude_repos:
                    continue
                for edge in node["languages"]["edges"]:
                    lang_name = edge["node"]["name"]
                    size = int(edge["size"])
                    if size <= 0:
                        continue
                    lang_weights[lang_name] = lang_weights.get(lang_name, 0.0) + math.sqrt(size)
                    if edge["node"].get("color") and lang_name not in lang_colors:
                        lang_colors[lang_name] = edge["node"]["color"]

            return {
                "display_name": user.get("name") or username,
                "commits_1y": int(user["contributionsCollection"]["totalCommitContributions"]),
                "contributed_to": int(user["contributionsCollection"]["totalRepositoriesWithContributedCommits"]),
                "total_prs": int(user["pullRequests"]["totalCount"]),
                "total_issues": int(user["issues"]["totalCount"]),
                "total_stars": total_stars,
                "original_repos": int(user["originalRepos"]["totalCount"]),
                "followers": int(user["followers"]["totalCount"]),
                "lang_weights": lang_weights,
                "lang_colors": lang_colors,
            }
        if isinstance(data, dict) and data.get("errors"):
            print(f"Warning: GraphQL returned errors, falling back to REST: {data['errors']}", file=sys.stderr)

    def safe_search_count(url: str) -> int | None:
        try:
            res, _ = github_request(url, token=token)
            if isinstance(res, dict) and "total_count" in res:
                return int(res["total_count"])
        except RuntimeError as exc:
            print(f"Warning: REST search query failed ({url}): {exc}", file=sys.stderr)
        return None

    user_data, _ = github_request(f"https://api.github.com/users/{username}", token=token)
    repos = fetch_repositories(username, token=token)
    orig_repos = [r for r in repos if not r.get("fork")]
    lang_weights = {}
    for repo in orig_repos:
        if repo["name"] in exclude_repos:
            continue
        repo_langs: dict[str, int] | None = None
        if token or len(orig_repos) <= 30:
            try:
                lang_res, _ = github_request(
                    f"https://api.github.com/repos/{username}/{repo['name']}/languages",
                    token=token,
                )
                if isinstance(lang_res, dict):
                    repo_langs = lang_res
            except RuntimeError:
                repo_langs = None
        if repo_langs:
            for lang_name, size in repo_langs.items():
                if int(size) > 0:
                    lang_weights[lang_name] = lang_weights.get(lang_name, 0.0) + math.sqrt(int(size))
        else:
            lang = repo.get("language")
            if lang:
                lang_weights[lang] = lang_weights.get(lang, 0.0) + math.sqrt(max(repo.get("size", 1), 1) * 1024)

    since_date = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
    return {
        "display_name": user_data.get("name") or username,
        "commits_1y": safe_search_count(
            f"https://api.github.com/search/commits?q={parse.quote(f'author:{username} committer-date:>={since_date}')}&per_page=1"
        ),
        "contributed_to": len(orig_repos),
        "total_prs": safe_search_count(
            f"https://api.github.com/search/issues?q={parse.quote(f'author:{username} type:pr')}&per_page=1"
        ),
        "total_issues": safe_search_count(
            f"https://api.github.com/search/issues?q={parse.quote(f'author:{username} type:issue')}&per_page=1"
        ),
        "total_stars": sum(int(r.get("stargazers_count", 0)) for r in repos),
        "original_repos": len(orig_repos),
        "followers": int(user_data.get("followers", 0)),
        "lang_weights": lang_weights,
        "lang_colors": {},
    }


def compute_top_languages(
    lang_weights: dict[str, float],
    lang_colors: dict[str, str],
    theme_key: str,
    top_n: int = 6,
) -> list[dict[str, Any]]:
    sorted_items = sorted(lang_weights.items(), key=lambda kv: -kv[1])[:top_n]
    total = sum(w for _, w in sorted_items)
    if total <= 0:
        return []

    raw_pcts = [round((w / total) * 100.0, 1) for _, w in sorted_items]
    diff = round(100.0 - sum(raw_pcts), 1)
    if raw_pcts and diff != 0:
        raw_pcts[0] = round(raw_pcts[0] + diff, 1)

    overrides = DARK_LANG_COLOR_OVERRIDES if theme_key == "dark" else LIGHT_LANG_COLOR_OVERRIDES
    results = []
    for (name, _), pct in zip(sorted_items, raw_pcts):
        color = overrides.get(name) or lang_colors.get(name) or DEFAULT_LANG_COLORS.get(name, "#38BDF8")
        results.append({"name": name, "pct": pct, "color": color})
    return results


def render_overview_stats_svg(
    stats: dict[str, Any],
    username: str,
    theme_cfg: dict[str, str],
) -> str:
    bg = f"#{theme_cfg['bg_color']}"
    surface = f"#{theme_cfg['surface_color']}"
    border = f"#{theme_cfg['border_color']}"
    title_c = f"#{theme_cfg['title_color']}"
    text_c = f"#{theme_cfg['text_color']}"
    muted_c = f"#{theme_cfg['muted_color']}"
    accent_c = f"#{theme_cfg['accent_color']}"

    def fmt_num(val: int | None) -> str:
        return f"{val:,}" if isinstance(val, int) else "—"

    metrics = [
        ("Commits (1y)", fmt_num(stats.get("commits_1y")), title_c),
        ("Pull Requests", fmt_num(stats.get("total_prs")), accent_c),
        ("Issues", fmt_num(stats.get("total_issues")), "#818CF8"),
        ("Original Repos", fmt_num(stats.get("original_repos")), "#F59E0B"),
        ("Total Stars", fmt_num(stats.get("total_stars")), title_c),
        ("Contributed To", fmt_num(stats.get("contributed_to")), accent_c),
    ]

    cells_svg: list[str] = []
    col_x = [20, 156, 292]
    row_y = [60, 114]
    for idx, (label, val, dot_c) in enumerate(metrics):
        x = col_x[idx % 3]
        y = row_y[idx // 3]
        cells_svg.append(
            f'  <rect x="{x}" y="{y}" width="118" height="46" rx="10" fill="{surface}" stroke="{border}" stroke-opacity="0.65" stroke-width="0.8" />\n'
            f'  <circle cx="{x + 12}" cy="{y + 15}" r="3" fill="{dot_c}" />\n'
            f'  <text x="{x + 20}" y="{y + 18.5}" fill="{muted_c}" font-family="{CARD_FONT_FAMILY}" font-size="10.5">{escape(label)}</text>\n'
            f'  <text x="{x + 12}" y="{y + 38}" fill="{text_c}" font-family="{MONO_FONT_FAMILY}" font-size="16" font-weight="700">{escape(val)}</text>'
        )

    title_text = f"{stats.get('display_name') or username}'s GitHub Overview"
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 430 196" width="430" height="196" role="img" aria-label="{escape(title_text, quote=True)}">
  <rect x="0.5" y="0.5" width="429" height="195" rx="14" fill="{bg}" stroke="{border}" stroke-width="1" />
  <text x="20" y="33" fill="{title_c}" font-family="{CARD_FONT_FAMILY}" font-size="15.5" font-weight="700">{escape(title_text)}</text>
  <text x="410" y="33" fill="{accent_c}" font-family="{MONO_FONT_FAMILY}" font-size="11" text-anchor="end">@{escape(username)}</text>
  <line x1="20" y1="45" x2="410" y2="45" stroke="{border}" stroke-opacity="0.7" />
{chr(10).join(cells_svg)}
  <text x="20" y="180" fill="{muted_c}" font-family="{CARD_FONT_FAMILY}" font-size="10.5">Public repositories · auto-synced via GitHub Actions</text>
</svg>
'''


def render_languages_stats_svg(
    languages: list[dict[str, Any]],
    theme_cfg: dict[str, str],
    theme_key: str,
) -> str:
    bg = f"#{theme_cfg['bg_color']}"
    border = f"#{theme_cfg['border_color']}"
    title_c = f"#{theme_cfg['title_color']}"
    text_c = f"#{theme_cfg['text_color']}"
    muted_c = f"#{theme_cfg['muted_color']}"
    track_c = f"#{theme_cfg['track_color']}"

    bar_x = 20
    bar_y = 58
    bar_w = 390
    bar_h = 10
    clip_id = f"lang-bar-clip-{theme_key}"

    segments: list[str] = []
    cursor_x = float(bar_x)
    for idx, item in enumerate(languages):
        seg_w = (bar_w - (cursor_x - bar_x)) if idx == len(languages) - 1 else (bar_w * item["pct"] / 100.0)
        if seg_w <= 0:
            continue
        segments.append(
            f'    <rect x="{cursor_x:.2f}" y="{bar_y}" width="{seg_w:.2f}" height="{bar_h}" fill="{escape(item["color"], quote=True)}" />'
        )
        cursor_x += seg_w

    legend: list[str] = []
    col_x = [20, 224]
    row_y = [96, 122, 148]
    for idx, item in enumerate(languages):
        col = idx % 2
        row = idx // 2
        if row >= len(row_y):
            break
        x = col_x[col]
        y = row_y[row]
        legend.append(
            f'  <circle cx="{x + 5}" cy="{y - 4}" r="4.5" fill="{escape(item["color"], quote=True)}" />\n'
            f'  <text x="{x + 16}" y="{y}" fill="{text_c}" font-family="{CARD_FONT_FAMILY}" font-size="12.5" font-weight="600">{escape(item["name"])}</text>\n'
            f'  <text x="{x + 186}" y="{y}" fill="{muted_c}" font-family="{MONO_FONT_FAMILY}" font-size="11.5" text-anchor="end">{item["pct"]:.1f}%</text>'
        )

    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 430 196" width="430" height="196" role="img" aria-label="Most Used Languages">
  <defs>
    <clipPath id="{clip_id}">
      <rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="{bar_h}" rx="5" />
    </clipPath>
  </defs>
  <rect x="0.5" y="0.5" width="429" height="195" rx="14" fill="{bg}" stroke="{border}" stroke-width="1" />
  <text x="20" y="33" fill="{title_c}" font-family="{CARD_FONT_FAMILY}" font-size="15.5" font-weight="700">Most Used Languages</text>
  <text x="410" y="33" fill="{muted_c}" font-family="{MONO_FONT_FAMILY}" font-size="10.5" text-anchor="end">sqrt-weighted</text>
  <line x1="20" y1="45" x2="410" y2="45" stroke="{border}" stroke-opacity="0.7" />
  <rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="{bar_h}" rx="5" fill="{track_c}" />
  <g clip-path="url(#{clip_id})">
{chr(10).join(segments)}
  </g>
{chr(10).join(legend)}
  <text x="20" y="180" fill="{muted_c}" font-family="{CARD_FONT_FAMILY}" font-size="10.5">Excludes forked &amp; asset-hosting repositories</text>
</svg>
'''


def render_stats_section(
    username: str,
    stats_config: dict[str, Any],
    stats_dir: Path,
    token: str | None = None,
) -> str:
    exclude_repos = set(stats_config.get("exclude_repos", []))
    user_stats = fetch_user_stats(username, exclude_repos, token=token)
    top_langs_dark = compute_top_languages(user_stats["lang_weights"], user_stats["lang_colors"], "dark", top_n=6)
    top_langs_light = compute_top_languages(user_stats["lang_weights"], user_stats["lang_colors"], "light", top_n=6)

    dark_cfg = stats_config["dark"]
    light_cfg = stats_config["light"]

    overview_dark_path = stats_dir / "overview-dark.svg"
    overview_light_path = stats_dir / "overview-light.svg"
    langs_dark_path = stats_dir / "languages-dark.svg"
    langs_light_path = stats_dir / "languages-light.svg"

    ov_dark_hash = write_if_changed(overview_dark_path, render_overview_stats_svg(user_stats, username, dark_cfg))
    ov_light_hash = write_if_changed(overview_light_path, render_overview_stats_svg(user_stats, username, light_cfg))
    lg_dark_hash = write_if_changed(langs_dark_path, render_languages_stats_svg(top_langs_dark, dark_cfg, "dark"))
    lg_light_hash = write_if_changed(langs_light_path, render_languages_stats_svg(top_langs_light, light_cfg, "light"))

    overview_picture = (
        f'<picture>'
        f'<source media="(prefers-color-scheme: dark)" srcset="./{overview_dark_path.as_posix()}?v={ov_dark_hash}" />'
        f'<img width="430" src="./{overview_light_path.as_posix()}?v={ov_light_hash}" alt="GitHub overview stats" />'
        f'</picture>'
    )
    langs_picture = (
        f'<picture>'
        f'<source media="(prefers-color-scheme: dark)" srcset="./{langs_dark_path.as_posix()}?v={lg_dark_hash}" />'
        f'<img width="430" src="./{langs_light_path.as_posix()}?v={lg_light_hash}" alt="Most used languages" />'
        f'</picture>'
    )

    return f"  {overview_picture}\n  {langs_picture}"


def render_template(template_text: str, values: dict[str, str]) -> str:
    template_keys = set(PLACEHOLDER_PATTERN.findall(template_text))
    unused = set(values.keys()) - template_keys
    if unused:
        print(f"Warning: values provided but not used in template: {', '.join(sorted(unused))}", file=sys.stderr)

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(f"Missing template value for {key}")
        return values[key]

    return PLACEHOLDER_PATTERN.sub(replace, template_text)


def collect_needed_repos(projects_cfg: dict[str, Any]) -> tuple[set[str], set[str]]:
    card_repos: set[str] = set()
    for cat in projects_cfg.get("categories", []):
        for entry in cat.get("items", []):
            card_repos.add(entry["repo"])
    for entry in projects_cfg.get("original", []):
        card_repos.add(entry["repo"])
    for entry in projects_cfg.get("forks", []):
        card_repos.add(entry["repo"])

    all_repos = set(card_repos)
    for entry in projects_cfg.get("more", []):
        all_repos.add(entry["repo"])
    return card_repos, all_repos


def main() -> int:
    args = parse_args()
    config_path = Path(args.config)
    template_path = Path(args.template)
    output_path = Path(args.output)
    cards_dir = Path(args.cards_dir)
    sponsors_dir = Path(args.sponsors_dir)
    stats_dir = Path(args.stats_dir)

    config = load_yaml(config_path)
    with template_path.open("r", encoding="utf-8") as fh:
        template_text = fh.read()

    profile = config["profile"]
    username = profile["username"]
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")

    card_repos, all_needed_repos = collect_needed_repos(config["projects"])
    repo_map: dict[str, dict[str, Any]] = {}
    for repo_name in sorted(all_needed_repos):
        repo = fetch_single_repo(username, repo_name, token=token)
        if repo:
            repo_map[repo_name] = repo

    prune_stale_cards(cards_dir, card_repos)

    original_cards_html, more_projects_md = render_projects_blocks(
        config["projects"], repo_map, cards_dir, username
    )
    blog_posts = fetch_blog_posts(
        profile.get("rss_url"),
        rss_file=args.rss_file,
        limit=int(profile.get("max_blog_posts", 5)),
    )

    values = {
        "HEADER_INTRO_ZH": profile["header_intro_zh"],
        "HEADER_INTRO_EN": profile["header_intro_en"],
        "QUICK_LINKS": format_quick_links(profile),
        "ORIGINAL_CARDS": original_cards_html,
        "MORE_PROJECTS": more_projects_md,
        "BLOG_POSTS": format_blog_section(blog_posts, profile.get("blog_url")),
        "TOOLBOX_BADGES": format_toolbox_badges(config["toolbox"]),
        "SPONSOR_CARDS": render_sponsor_section(config["sponsor"]["methods"], sponsors_dir),
        "STATS_SECTION": render_stats_section(username, config["stats"], stats_dir, token=token),
        "FOOTER_ZH": profile["footer_zh"],
        "FOOTER_EN": profile["footer_en"],
    }

    output = AUTO_GENERATED_NOTICE + render_template(template_text, values)
    with output_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(output.rstrip() + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
