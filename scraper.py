"""Scrape OptiSigns support articles via Zendesk API and save as Markdown."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md

BASE_URL = "https://support.optisigns.com"
API_URL = f"{BASE_URL}/api/v2/help_center/en-us/articles.json"
DOCS_DIR = Path(__file__).parent / "docs"

# Ensure test question "How do I add a YouTube video?" has a matching doc.
MUST_INCLUDE_IDS = [
    360051014713,  # How to use YouTube with OptiSigns
]


def slug_from_url(html_url: str) -> str:
    """Extract slug from Zendesk html_url (segment after article id)."""
    match = re.search(r"/articles/\d+-(.+)$", html_url)
    if match:
        return match.group(1).lower()
    return re.sub(r"[^a-z0-9]+", "-", html_url.rsplit("/", 1)[-1]).strip("-").lower()


def fetch_article_by_id(article_id: int) -> dict | None:
    url = f"{BASE_URL}/api/v2/help_center/en-us/articles/{article_id}.json"
    response = requests.get(url, timeout=30)
    if response.status_code != 200:
        return None
    return response.json().get("article")


def fetch_articles_page(page: int = 1, per_page: int = 100) -> list[dict]:
    response = requests.get(
        API_URL,
        params={"page": page, "per_page": per_page},
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("articles", [])


def fetch_latest_articles(limit: int) -> list[dict]:
    articles: list[dict] = []
    page = 1
    while len(articles) < limit:
        batch = fetch_articles_page(page=page)
        if not batch:
            break
        articles.extend(batch)
        page += 1
    return articles[:limit]


def clean_html(html: str) -> str:
    """Remove Zendesk cruft while keeping structure, links, and code."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(["script", "style"]):
        tag.decompose()

    for tag in soup.find_all(attrs={"data-list-item-id": True}):
        del tag["data-list-item-id"]

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if href.startswith("#"):
            continue
        if href.startswith("/"):
            anchor["href"] = urljoin(BASE_URL, href)

    for img in soup.find_all("img", src=True):
        src = img["src"]
        if src.startswith("/"):
            img["src"] = urljoin(BASE_URL, src)

    return str(soup)


def html_to_markdown(html: str) -> str:
    cleaned = clean_html(html)
    markdown = md(
        cleaned,
        heading_style="ATX",
        bullets="-",
        strip=["figure"],
    )
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return markdown.strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_markdown(article: dict) -> str:
    title = article["title"]
    html_url = article["html_url"]
    updated_at = article.get("updated_at", "")
    article_id = article["id"]
    body_md = html_to_markdown(article.get("body") or "")

    frontmatter = {
        "title": title,
        "article_id": article_id,
        "article_url": html_url,
        "updated_at": updated_at,
    }

    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"Article URL: {html_url}")
    lines.append("")
    if body_md:
        lines.append(body_md)

    return "\n".join(lines)


def save_article(article: dict, docs_dir: Path = DOCS_DIR) -> Path:
    docs_dir.mkdir(parents=True, exist_ok=True)
    slug = slug_from_url(article["html_url"])
    path = docs_dir / f"{slug}.md"
    path.write_text(build_markdown(article), encoding="utf-8")
    return path


def collect_articles(limit: int = 30) -> list[dict]:
    """Return up to `limit` articles, always including MUST_INCLUDE_IDS."""
    by_id: dict[int, dict] = {}

    for article_id in MUST_INCLUDE_IDS:
        article = fetch_article_by_id(article_id)
        if article and not article.get("draft"):
            by_id[article["id"]] = article

    remaining = max(0, limit - len(by_id))
    if remaining:
        for article in fetch_latest_articles(remaining + len(by_id)):
            if article.get("draft"):
                continue
            by_id.setdefault(article["id"], article)
            if len(by_id) >= limit:
                break

    return list(by_id.values())[:limit]


def scrape(limit: int = 30, docs_dir: Path = DOCS_DIR) -> dict:
    articles = collect_articles(limit=limit)
    docs_dir.mkdir(parents=True, exist_ok=True)

    stats = {"added": 0, "updated": 0, "skipped": 0, "count": 0, "files": []}

    for article in articles:
        slug = slug_from_url(article["html_url"])
        path = docs_dir / f"{slug}.md"
        markdown = build_markdown(article)
        new_hash = content_hash(markdown)

        if path.exists():
            old_hash = content_hash(path.read_text(encoding="utf-8"))
            if old_hash == new_hash:
                stats["skipped"] += 1
                continue
            stats["updated"] += 1
        else:
            stats["added"] += 1

        path.write_text(markdown, encoding="utf-8")
        stats["files"].append(path.name)

    stats["count"] = len(articles)
    stats["files"] = sorted(stats["files"])
    stats["article_ids"] = [a["id"] for a in articles]
    return stats


if __name__ == "__main__":
    result = scrape(limit=30)
    print(json.dumps(result, indent=2))