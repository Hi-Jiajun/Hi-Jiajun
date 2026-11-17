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
    "C": "#555555",
    "Rust": "#DEA584",
    "Python": "#3572A5",
    "JavaScript": "#F1E05A",
    "TypeScript": "#3178C6",
    "Shell": "#89E051",
    "Java": "#B07219",
    "Svelte": "#FF3E00",
    "Swift": "#F05138",
    "Kotlin": "#A97BFF",
    "Go": "#00ADD8",
    "Astro": "#FF5A03",
}

MIME_BY_EXT = {
    ".webp": "image/webp",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
}


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
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def format_about(items: list[dict[str, str]]) -> str:
    lines = []
    for item in items:
        lines.append(f"- {item['icon']} {item['zh']}<br/>\n  <sub>{item['en']}</sub>")
    return "\n".join(lines)


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
        f'<picture>\n'
        f'    <source media="(prefers-color-scheme: dark)" srcset="{dark_url}" />\n'
        f'    <img src="{light_url}" alt="{alt}" />\n'
        f'  </picture>'
    )
    if href:
        return f'  <a href="{escape(href, quote=True)}">\n  {picture}\n  </a>'
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
    rss_url = profile.get("rss_url")
    if rss_url:
        links.append(
            make_theme_badge(
                label="RSS",
                message="feed",
                color_dark="0D9488",
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


def render_card_svg(
    repo_name: str,
    category_label: str,
    note_zh: str,
    note_en: str,
    language: str,
    stars: int,
    forks: int,
    pushed: str,
) -> str:
    title = truncate(repo_name, 30)
    zh = truncate(note_zh, 34)
    en = truncate(note_en, 66)
    stats = f"⭐ {stars} · 🍴 {forks} · updated {pushed}"
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 440 140" width="440" height="140" role="img" aria-label="{escape(repo_name, quote=True)}">
  <style>
    .card {{ fill: #F0F6FC; stroke: #cbd5e1; }}
    .tag {{ fill: #0284c7; font-weight: 700; letter-spacing: 1.2px; }}
    .title {{ fill: #0f172a; font-weight: 700; }}
    .note-zh {{ fill: #334155; }}
    .note-en {{ fill: #64748b; }}
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
  <text x="18" y="28" class="tag" font-family="{CARD_FONT_FAMILY}" font-size="11.5">{escape(category_label)}</text>
  <text x="422" y="28" class="stats" font-family="{CARD_FONT_FAMILY}" font-size="12" text-anchor="end">{escape(language)}</text>
  <text x="18" y="58" class="title" font-family="{CARD_FONT_FAMILY}" font-size="19.5">{escape(title)}</text>
  <text x="18" y="82" class="note-zh" font-family="{CARD_FONT_FAMILY}" font-size="12.8">{escape(zh)}</text>
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


def render_project_cards_table(
    entries: list[dict[str, str]],
    repo_map: dict[str, dict[str, Any]],
    cards_dir: Path,
    username: str,
    default_tag: str = "🛠 ORIGINAL",
) -> str:
    cells: list[str] = []
    for entry in entries:
        repo_name = entry["repo"]
        repo = repo_map.get(repo_name)
        repo_url = repo["html_url"] if repo else f"https://github.com/{username}/{repo_name}"
        language = entry.get("language") or (repo.get("language") if repo else None) or "—"
        stars = int(repo.get("stargazers_count", 0)) if repo else 0
        forks = int(repo.get("forks_count", 0)) if repo else 0
        pushed = format_date(repo.get("pushed_at") if repo else None)
        tag = entry.get("tag") or default_tag

        svg = render_card_svg(
            repo_name=repo_name,
            category_label=tag,
            note_zh=entry["note_zh"],
            note_en=entry["note_en"],
            language=language,
            stars=stars,
            forks=forks,
            pushed=pushed,
        )
        card_path = cards_dir / f"{repo_name}.svg"
        card_hash = write_if_changed(card_path, svg)
        img_src = f"./{card_path.as_posix()}?v={card_hash}"
        cells.append(
            f'<td><a href="{escape(repo_url, quote=True)}"><img src="{img_src}" alt="{escape(repo_name, quote=True)}" /></a></td>'
        )

    if len(cells) == 1:
        return f'<div align="center">\n\n{cells[0].replace("<td>", "").replace("</td>", "")}\n\n</div>'

    rows = []
    for i in range(0, len(cells), 2):
        pair = cells[i : i + 2]
        rows.append("  <tr>\n    " + "\n    ".join(pair) + "\n  </tr>")
    return "<table>\n" + "\n".join(rows) + "\n</table>"


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
            heading = f"### {cat['title_zh']} <sub>{cat['title_en']}</sub>"
            table_html = render_project_cards_table(cat["items"], repo_map, cards_dir, username)
            sections.append(f"{heading}\n\n{table_html}")
        original_html = "\n\n".join(sections)
    else:
        original_html = render_project_cards_table(
            projects_cfg.get("original", []), repo_map, cards_dir, username
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


def prune_stale_cards(cards_dir: Path, expected_names: set[str]) -> None:
    if not cards_dir.exists():
        return
    for existing in cards_dir.glob("*.svg"):
        if existing.stem not in expected_names:
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
    .card {{ fill: #F0F6FC; stroke: #cbd5e1; }}
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
    # RSS 2.0
    channel_items = root.findall("./channel/item")
    for item in channel_items[:limit]:
        title = clean_rss_text(item.findtext("title"))
        link = (item.findtext("link") or "").strip()
        date_str = parse_rss_date(item.findtext("pubDate"))
        desc = truncate(clean_rss_text(item.findtext("description")), 92)
        if title and link:
            posts.append({"title": title, "link": link, "date": date_str, "desc": desc})

    if posts:
        return posts

    # Atom fallback
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
        lines.append(f"→ [访问个人博客阅读全部文章 · Read more at {host}]({blog_url})")
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
                    # sqrt dampening prevents huge vendored files in one repo from drowning out all others
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

    # REST fallback if token is absent or GraphQL is restricted (e.g. GitHub Actions GITHUB_TOKEN)
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
    total_prs = safe_search_count(
        f"https://api.github.com/search/issues?q={parse.quote(f'author:{username} type:pr')}&per_page=1"
    )
    total_issues = safe_search_count(
        f"https://api.github.com/search/issues?q={parse.quote(f'author:{username} type:issue')}&per_page=1"
    )
    commits_1y = safe_search_count(
        f"https://api.github.com/search/commits?q={parse.quote(f'author:{username} committer-date:>={since_date}')}&per_page=1"
    )

    return {
        "display_name": user_data.get("name") or username,
        "commits_1y": commits_1y,
        "contributed_to": len(orig_repos),
        "total_prs": total_prs,
        "total_issues": total_issues,
        "total_stars": sum(int(r.get("stargazers_count", 0)) for r in repos),
        "original_repos": len(orig_repos),
        "followers": int(user_data.get("followers", 0)),
        "lang_weights": lang_weights,
        "lang_colors": {},
    }


def compute_top_languages(
    lang_weights: dict[str, float],
    lang_colors: dict[str, str],
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

    results = []
    for (name, _), pct in zip(sorted_items, raw_pcts):
        color = lang_colors.get(name) or DEFAULT_LANG_COLORS.get(name, "#38BDF8")
        results.append({"name": name, "pct": pct, "color": color})
    return results


def render_overview_stats_svg(
    stats: dict[str, Any],
    username: str,
    theme_cfg: dict[str, str],
) -> str:
    bg = f"#{theme_cfg['bg_color']}"
    border = f"#{theme_cfg['border_color']}"
    title_c = f"#{theme_cfg['title_color']}"
    text_c = f"#{theme_cfg['text_color']}"
    muted_c = f"#{theme_cfg['muted_color']}"
    accent_c = f"#{theme_cfg['accent_color']}"

    def fmt_num(val: int | None) -> str:
        return f"{val:,}" if isinstance(val, int) else "—"

    metrics = [
        ("Commits (1y)", fmt_num(stats.get("commits_1y"))),
        ("Pull Requests", fmt_num(stats.get("total_prs"))),
        ("Issues", fmt_num(stats.get("total_issues"))),
        ("Original Repos", fmt_num(stats.get("original_repos"))),
        ("Total Stars", fmt_num(stats.get("total_stars"))),
        ("Contributed To", fmt_num(stats.get("contributed_to"))),
    ]

    cells_svg: list[str] = []
    col_x = [24, 180, 336]
    row_y = [72, 124]
    for idx, (label, val) in enumerate(metrics):
        x = col_x[idx % 3]
        y = row_y[idx // 3]
        cells_svg.append(
            f'  <text x="{x}" y="{y}" fill="{muted_c}" font-family="{CARD_FONT_FAMILY}" font-size="11.5">{escape(label)}</text>\n'
            f'  <text x="{x}" y="{y + 23}" fill="{text_c}" font-family="{CARD_FONT_FAMILY}" font-size="20" font-weight="700">{escape(val)}</text>'
        )

    title_text = f"{stats.get('display_name') or username}'s GitHub Overview"
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 188" width="480" height="188" role="img" aria-label="{escape(title_text, quote=True)}">
  <rect x="0.5" y="0.5" width="479" height="187" rx="16" fill="{bg}" stroke="{border}" stroke-width="1" />
  <text x="24" y="34" fill="{title_c}" font-family="{CARD_FONT_FAMILY}" font-size="16" font-weight="700">{escape(title_text)}</text>
  <text x="456" y="34" fill="{accent_c}" font-family="{MONO_FONT_FAMILY}" font-size="11.5" text-anchor="end">@{escape(username)}</text>
  <line x1="24" y1="47" x2="456" y2="47" stroke="{border}" stroke-opacity="0.7" />
{chr(10).join(cells_svg)}
  <text x="24" y="172" fill="{muted_c}" font-family="{CARD_FONT_FAMILY}" font-size="10.5">Public repositories · auto-updated via GitHub Actions</text>
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

    bar_x = 24
    bar_y = 58
    bar_w = 432
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
    col_x = [24, 252]
    row_y = [95, 121, 147]
    for idx, item in enumerate(languages):
        col = idx % 2
        row = idx // 2
        if row >= len(row_y):
            break
        x = col_x[col]
        y = row_y[row]
        legend.append(
            f'  <circle cx="{x + 5}" cy="{y - 4}" r="5" fill="{escape(item["color"], quote=True)}" />\n'
            f'  <text x="{x + 17}" y="{y}" fill="{text_c}" font-family="{CARD_FONT_FAMILY}" font-size="12.5" font-weight="600">{escape(item["name"])}</text>\n'
            f'  <text x="{x + 204}" y="{y}" fill="{muted_c}" font-family="{MONO_FONT_FAMILY}" font-size="12" text-anchor="end">{item["pct"]:.1f}%</text>'
        )

    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 196" width="480" height="196" role="img" aria-label="Most Used Languages">
  <defs>
    <clipPath id="{clip_id}">
      <rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="{bar_h}" rx="5" />
    </clipPath>
  </defs>
  <rect x="0.5" y="0.5" width="479" height="195" rx="16" fill="{bg}" stroke="{border}" stroke-width="1" />
  <text x="24" y="34" fill="{title_c}" font-family="{CARD_FONT_FAMILY}" font-size="16" font-weight="700">Most Used Languages</text>
  <text x="456" y="34" fill="{muted_c}" font-family="{CARD_FONT_FAMILY}" font-size="11" text-anchor="end">Original repos (√-weighted)</text>
  <rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="{bar_h}" rx="5" fill="{track_c}" />
  <g clip-path="url(#{clip_id})">
{chr(10).join(segments)}
  </g>
{chr(10).join(legend)}
  <text x="24" y="178" fill="{muted_c}" font-family="{CARD_FONT_FAMILY}" font-size="10.5">Excludes forked &amp; asset-hosting repositories</text>
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
    top_langs = compute_top_languages(user_stats["lang_weights"], user_stats["lang_colors"], top_n=6)

    dark_cfg = stats_config["dark"]
    light_cfg = stats_config["light"]

    overview_dark_path = stats_dir / "overview-dark.svg"
    overview_light_path = stats_dir / "overview-light.svg"
    langs_dark_path = stats_dir / "languages-dark.svg"
    langs_light_path = stats_dir / "languages-light.svg"

    ov_dark_hash = write_if_changed(overview_dark_path, render_overview_stats_svg(user_stats, username, dark_cfg))
    ov_light_hash = write_if_changed(overview_light_path, render_overview_stats_svg(user_stats, username, light_cfg))
    lg_dark_hash = write_if_changed(langs_dark_path, render_languages_stats_svg(top_langs, dark_cfg, "dark"))
    lg_light_hash = write_if_changed(langs_light_path, render_languages_stats_svg(top_langs, light_cfg, "light"))

    overview_picture = (
        f'<picture>\n'
        f'  <source media="(prefers-color-scheme: dark)" srcset="./{overview_dark_path.as_posix()}?v={ov_dark_hash}" />\n'
        f'  <img src="./{overview_light_path.as_posix()}?v={ov_light_hash}" alt="GitHub overview stats" />\n'
        f'</picture>'
    )
    langs_picture = (
        f'<picture>\n'
        f'  <source media="(prefers-color-scheme: dark)" srcset="./{langs_dark_path.as_posix()}?v={lg_dark_hash}" />\n'
        f'  <img src="./{langs_light_path.as_posix()}?v={lg_light_hash}" alt="Most used languages" />\n'
        f'</picture>'
    )

    return f"{overview_picture}\n\n<br/>\n\n{langs_picture}"


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
        "HEADER_TITLE": profile["header_title"],
        "HEADER_ROLE_ZH": profile["header_role_zh"],
        "HEADER_ROLE_EN": profile["header_role_en"],
        "HEADER_INTRO_ZH": profile["header_intro_zh"],
        "HEADER_INTRO_EN": profile["header_intro_en"],
        "QUICK_LINKS": format_quick_links(profile),
        "OWNERSHIP_NOTE_ZH": profile["ownership_note_zh"],
        "OWNERSHIP_NOTE_EN": profile["ownership_note_en"],
        "ABOUT_ITEMS": format_about(config["about"]),
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
