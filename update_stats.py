
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

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
    "User-Agent": (
        "NesirMostReadUpdater/1.0 "
        "(public journal statistics; daily scheduled check)"
    ),
    "Accept": "text/html",
})


def parse_number(value):
    cleaned = re.sub(r"[\s.,\u00a0]", "", value.strip())
    if not re.fullmatch(r"\d+", cleaned):
        raise ValueError(f"Invalid statistic: {value!r}")
    return int(cleaned)


def read_article(article_id):
    url = f"{BASE_URL}/article/view/{article_id}"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.get(url, timeout=TIMEOUT)
            response.raise_for_status()

            soup = BeautifulSoup(
                response.text, "html.parser"
            )

            stats = soup.select(
                ".asd-stats-detail .asd-count"
            )

            if not stats:
                raise ValueError(
                    "Public statistics not found"
                )

            counts = [
                parse_number(node.get_text(" ", strip=True))
                for node in stats
            ]

            title_tag = soup.select_one(
                'meta[name="citation_title"]'
            )

            if not title_tag:
                raise ValueError(
                    "Article title metadata not found"
                )

            title = title_tag.get("content", "").strip()

            if not title:
                raise ValueError("Empty article title")

            abstract_views = counts[0]
            file_downloads = sum(counts[1:])

            return {
                "id": article_id,
                "title": title,
                "url": url,
                "abstract": abstract_views,
                "pdf": file_downloads,
                "total": abstract_views + file_downloads,
            }

        except (
            requests.RequestException,
            ValueError,
        ) as error:
            print(
                f"Article {article_id}, "
                f"attempt {attempt}/{MAX_ATTEMPTS}: "
                f"{error}",
                flush=True,
            )

            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(
                    f"Could not read article {article_id}"
                ) from error

            time.sleep(3 * attempt)


def main():
    ids = json.loads(
        IDS_FILE.read_text(encoding="utf-8")
    )

    if (
        not isinstance(ids, list)
        or not ids
        or any(
            type(article_id) is not int
            or article_id <= 0
            for article_id in ids
        )
        or len(ids) != len(set(ids))
    ):
        raise ValueError(
            "article_ids.json must contain "
            "unique positive integer IDs"
        )

    print(
        f"Reading statistics for {len(ids)} articles",
        flush=True,
    )

    articles = []

    for index, article_id in enumerate(ids, start=1):
        article = read_article(article_id)
        articles.append(article)

        print(
            f"{index}/{len(ids)}: "
            f"{article['title'][:55]} "
            f"— {article['total']}",
            flush=True,
        )

        if index < len(ids):
            time.sleep(REQUEST_DELAY)

    articles.sort(
        key=lambda item: (-item["total"], item["id"])
    )

    output = {
        "updated_at": datetime.now(
            timezone.utc
        ).isoformat(timespec="seconds"),
        "period": "all_time",
        "metric": "abstract_views_plus_file_downloads",
        "articles_checked": len(articles),
        "top_30": articles[:TOP_N],
    }

    # Write only after every article succeeds.
    temporary = OUTPUT_FILE.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            output,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    temporary.replace(OUTPUT_FILE)

    print(
        f"Success: {len(articles)} articles checked. "
        f"Saved {OUTPUT_FILE}",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"UPDATE FAILED: {error}", file=sys.stderr)
        sys.exit(1)
