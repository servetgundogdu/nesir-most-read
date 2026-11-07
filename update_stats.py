import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://nesirdergisi.com/index.php/nesir"
IDS_FILE = Path("article_ids.json")
OUTPUT_FILE = Path("most_read.json")
REQUEST_DELAY = 1.5
TIMEOUT = 25
MAX_ATTEMPTS = 3
TOP_N = 30

session = requests.Session()
session.headers.update({
    "User-Agent": "NesirMostReadUpdater/1.2 (public journal statistics; scheduled check)",
    "Accept": "text/html",
})


def parse_number(value):
    cleaned = re.sub(r"[\s.,\u00a0]", "", value.strip())
    if not re.fullmatch(r"\d+", cleaned):
        raise ValueError(f"Invalid statistic: {value!r}")
    return int(cleaned)


def get_meta(soup, name):
    tag = soup.select_one(f'meta[name="{name}"]')
    return tag.get("content", "").strip() if tag else ""


def get_authors(soup):
    return [
        tag.get("content", "").strip()
        for tag in soup.select('meta[name="citation_author"]')
        if tag.get("content", "").strip()
    ]


def fetch_html(url):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.get(url, timeout=TIMEOUT)
            response.raise_for_status()
            return BeautifulSoup(response.text, "html.parser")
        except requests.RequestException as error:
            print(f"Fetch {url}, attempt {attempt}/{MAX_ATTEMPTS}: {error}", flush=True)
            if attempt == MAX_ATTEMPTS:
                raise
            time.sleep(3 * attempt)


def discover_article_ids():
    archive_url = f"{BASE_URL}/issue/archive"
    issue_ids = set()
    visited = set()
    page_url = archive_url

    for _ in range(100):
        if page_url in visited:
            raise RuntimeError("Archive pagination loop")
        visited.add(page_url)
        soup = fetch_html(page_url)

        for link in soup.select('a[href]'):
            absolute = urljoin(page_url, link["href"])
            match = re.search(r"/issue/view/(\d+)(?:[/?#]|$)", absolute)
            if match:
                issue_ids.add(int(match.group(1)))

        next_link = soup.select_one(
            'a[rel="next"], .cmp_pagination .next a, .pagination .next a'
        )
        if not next_link or not next_link.get("href"):
            break
        next_url = urljoin(page_url, next_link["href"])
        if next_url == page_url:
            raise RuntimeError("Archive pagination did not advance")
        page_url = next_url
    else:
        raise RuntimeError("Archive page limit reached")

    if not issue_ids:
        raise RuntimeError("No published issues discovered")

    article_ids = set()
    for issue_id in sorted(issue_ids):
        issue_url = f"{BASE_URL}/issue/view/{issue_id}"
        soup = fetch_html(issue_url)
        found = set()
        for link in soup.select('a[href]'):
            absolute = urljoin(issue_url, link["href"])
            match = re.search(r"/article/view/(\d+)(?:[/?#]|$)", absolute)
            if match:
                found.add(int(match.group(1)))
        if not found:
            raise RuntimeError(f"No articles found in issue {issue_id}")
        article_ids.update(found)
        print(f"Issue {issue_id}: {len(found)} publications", flush=True)
        time.sleep(REQUEST_DELAY)

    print(
        f"Discovered {len(article_ids)} unique publications across {len(issue_ids)} issues",
        flush=True,
    )
    return article_ids


def read_article(article_id):
    url = f"{BASE_URL}/article/view/{article_id}"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.get(url, timeout=TIMEOUT)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            stats = soup.select(".asd-stats-detail .asd-count")
            if not stats:
                raise ValueError("Public statistics not found")
            counts = [parse_number(node.get_text(" ", strip=True)) for node in stats]
            title = get_meta(soup, "citation_title")
            if not title:
                raise ValueError("Article title missing")
            abstract_views = counts[0]
            file_downloads = sum(counts[1:])
            return {
                "id": article_id,
                "title": title,
                "url": url,
                "authors": get_authors(soup),
                "issue": get_meta(soup, "citation_issue"),
                "date": get_meta(soup, "citation_date"),
                "journal": get_meta(soup, "citation_journal_title"),
                "abstract": abstract_views,
                "pdf": file_downloads,
                "total": abstract_views + file_downloads,
            }
        except (requests.RequestException, ValueError) as error:
            print(
                f"Article {article_id}, attempt {attempt}/{MAX_ATTEMPTS}: {error}",
                flush=True,
            )
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(f"Could not read article {article_id}") from error
            time.sleep(3 * attempt)


def main():
    existing_ids = json.loads(IDS_FILE.read_text(encoding="utf-8"))
    if (
        not isinstance(existing_ids, list)
        or not existing_ids
        or any(type(article_id) is not int or article_id <= 0 for article_id in existing_ids)
        or len(existing_ids) != len(set(existing_ids))
    ):
        raise ValueError("article_ids.json must contain unique positive integer IDs")

    discovered_ids = discover_article_ids()
    ids = sorted(set(existing_ids) | discovered_ids)
    new_ids = sorted(discovered_ids - set(existing_ids))
    print(f"New publications: {len(new_ids)}", flush=True)
    if new_ids:
        print(f"New article IDs: {new_ids}", flush=True)
    print(f"Reading {len(ids)} articles", flush=True)

    articles = []
    for index, article_id in enumerate(ids, start=1):
        article = read_article(article_id)
        articles.append(article)
        print(
            f"{index}/{len(ids)}: {article['title'][:55]} — {article['total']}",
            flush=True,
        )
        if index < len(ids):
            time.sleep(REQUEST_DELAY)

    articles.sort(key=lambda item: (-item["total"], item["id"]))
    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "period": "all_time",
        "metric": "abstract_views_plus_file_downloads",
        "articles_checked": len(articles),
        "top_30": articles[:TOP_N],
    }

    temporary = OUTPUT_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(OUTPUT_FILE)
    IDS_FILE.write_text(json.dumps(ids, indent=2) + "\n", encoding="utf-8")
    print(f"Success: {len(articles)} articles checked. Saved {OUTPUT_FILE}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"UPDATE FAILED: {error}", file=sys.stderr)
        sys.exit(1)
