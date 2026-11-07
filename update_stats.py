
import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://nesirdergisi.com/index.php/nesir"

IDS_FILE = Path("article_ids.json")
OUTPUT_FILE = Path("most_read.json")
HISTORY_FILE = Path("statistics_history.json")
BASELINE_FILE = Path("nesir_daily_baseline_2026-11-07.json")

REQUEST_DELAY = 1.5
TIMEOUT = 25
MAX_ATTEMPTS = 3
TOP_N = 30

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "NesirMostReadUpdater/1.2 "
        "(public journal statistics; scheduled check)"
    ),
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
            return BeautifulSoup(
                response.text, "html.parser"
            )
        except requests.RequestException as error:
            print(
                f"Fetch {url}, attempt "
                f"{attempt}/{MAX_ATTEMPTS}: {error}",
                flush=True,
            )
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
            match = re.search(
                r"/issue/view/(\d+)(?:[/?#]|$)",
                absolute,
            )
            if match:
                issue_ids.add(int(match.group(1)))

        next_link = soup.select_one(
            'a[rel="next"], '
            '.cmp_pagination .next a, '
            '.pagination .next a'
        )

        if not next_link or not next_link.get("href"):
            break

        next_url = urljoin(
            page_url, next_link["href"]
        )

        if next_url == page_url:
            raise RuntimeError(
                "Archive pagination did not advance"
            )

        page_url = next_url
    else:
        raise RuntimeError(
            "Archive page limit reached"
        )

    if not issue_ids:
        raise RuntimeError(
            "No published issues discovered"
        )

    article_ids = set()

    for issue_id in sorted(issue_ids):
        issue_url = (
            f"{BASE_URL}/issue/view/{issue_id}"
        )
        soup = fetch_html(issue_url)
        found = set()

        for link in soup.select('a[href]'):
            absolute = urljoin(
                issue_url, link["href"]
            )
            match = re.search(
                r"/article/view/(\d+)(?:[/?#]|$)",
                absolute,
            )
            if match:
                found.add(int(match.group(1)))

        if not found:
            raise RuntimeError(
                f"No articles found in issue {issue_id}"
            )

        article_ids.update(found)

        print(
            f"Issue {issue_id}: "
            f"{len(found)} publications",
            flush=True,
        )

        time.sleep(REQUEST_DELAY)

    print(
        f"Discovered {len(article_ids)} "
        f"unique publications across "
        f"{len(issue_ids)} issues",
        flush=True,
    )

    return article_ids


def read_article(article_id):
    url = (
        f"{BASE_URL}/article/view/{article_id}"
    )

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.get(
                url, timeout=TIMEOUT
            )
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
                parse_number(
                    node.get_text(" ", strip=True)
                )
                for node in stats
            ]

            title = get_meta(
                soup, "citation_title"
            )

            if not title:
                raise ValueError(
                    "Article title missing"
                )

            abstract_views = counts[0]
            file_downloads = sum(counts[1:])

            return {
                "id": article_id,
                "title": title,
                "url": url,
                "authors": get_authors(soup),
                "issue": get_meta(
                    soup, "citation_issue"
                ),
                "date": get_meta(
                    soup, "citation_date"
                ),
                "journal": get_meta(
                    soup, "citation_journal_title"
                ),
                "abstract": abstract_views,
                "pdf": file_downloads,
                "total": (
                    abstract_views + file_downloads
                ),
            }

        except (
            requests.RequestException,
            ValueError,
        ) as error:
            print(
                f"Article {article_id}, attempt "
                f"{attempt}/{MAX_ATTEMPTS}: "
                f"{error}",
                flush=True,
            )

            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(
                    f"Could not read article "
                    f"{article_id}"
                ) from error

            time.sleep(3 * attempt)


def update_history(articles, timestamp):
    """Store one cumulative snapshot per UTC day."""

    if HISTORY_FILE.exists():
        history = json.loads(
            HISTORY_FILE.read_text(
                encoding="utf-8"
            )
        )
    else:
        history = {
            "schema_version": 1,
            "snapshots": {},
        }

    if (
        history.get("schema_version") != 1
        or not isinstance(
            history.get("snapshots"), dict
        )
    ):
        raise ValueError(
            "Invalid statistics_history.json format"
        )

    day = timestamp.date().isoformat()
    snapshots = history["snapshots"]

    if snapshots and day < max(snapshots):
        raise ValueError(
            "Refusing to insert a snapshot "
            "before the most recent day"
        )

    snapshots[day] = {
        "captured_at": timestamp.isoformat(
            timespec="seconds"
        ),
        "articles": {
            str(article["id"]): {
                "abstract": article["abstract"],
                "pdf": article["pdf"],
            }
            for article in articles
        },
    }

    return history


def load_daily_baseline():
    """Load historical OJS daily usage events."""

    if not BASELINE_FILE.exists():
        print(
            "Historical baseline missing; "
            "waiting for complete snapshots",
            flush=True,
        )
        return None

    data = json.loads(
        BASELINE_FILE.read_text(
            encoding="utf-8"
        )
    )

    if (
        data.get("schema_version") != 1
        or not isinstance(
            data.get("daily"), dict
        )
    ):
        raise ValueError(
            "Invalid daily baseline format"
        )

    source_range = data.get(
        "source_date_range", {}
    )

    if (
        source_range.get("from")
        != "2025-11-08"
    ):
        raise ValueError(
            "Unexpected historical "
            "baseline start date"
        )

    if (
        source_range.get("through")
        != "2026-11-07"
    ):
        raise ValueError(
            "Unexpected historical "
            "baseline end date"
        )

    if (
        data.get("latest_observed_event_date")
        != "20261106"
    ):
        raise ValueError(
            "Unexpected historical "
            "baseline last event date"
        )

    return data


def period_ranking(
    articles,
    history,
    timestamp,
    days,
    baseline=None,
):
    """
    Combine historical daily events with
    differences between cumulative snapshots.
    """

    end = timestamp.date()
    start = end - timedelta(days=days - 1)

    snapshots = history["snapshots"]

    event_end = datetime.strptime(
        "20261106", "%Y%m%d"
    ).date()

    anchor = datetime.strptime(
        "2026-11-07", "%Y-%m-%d"
    ).date()

    available_snapshots = sorted(
        (
            datetime.strptime(
                key, "%Y-%m-%d"
            ).date(),
            value,
        )
        for key, value in snapshots.items()
    )

    if not available_snapshots:
        return None

    if baseline is None:
        cutoff = timestamp - timedelta(
            days=days
        )

        candidates = [
            (
                datetime.fromisoformat(
                    value["captured_at"]
                ),
                value,
            )
            for _, value in available_snapshots
            if datetime.fromisoformat(
                value["captured_at"]
            ) <= cutoff
        ]

        if not candidates:
            return None

        base_time, base_snapshot = max(
            candidates,
            key=lambda pair: pair[0],
        )

        if (
            cutoff - base_time
            > timedelta(hours=36)
        ):
            return None

        current = snapshots[
            end.isoformat()
        ]["articles"]

        prior = base_snapshot["articles"]
        ranked = []

        for article in articles:
            key = str(article["id"])

            if (
                key not in prior
                or key not in current
            ):
                continue

            a = (
                current[key]["abstract"]
                - prior[key]["abstract"]
            )

            p = (
                current[key]["pdf"]
                - prior[key]["pdf"]
            )

            if a >= 0 and p >= 0:
                ranked.append({
                    **article,
                    "abstract": a,
                    "pdf": p,
                    "total": a + p,
                })

        ranked.sort(
            key=lambda item: (
                -item["total"],
                item["id"],
            )
        )

        return {
            "period": f"last_{days}_days",
            "from": start.isoformat(),
            "through": end.isoformat(),
            "method": (
                "cumulative_snapshot_difference"
            ),
            "coverage": "daily_snapshots",
            "articles_with_complete_window": (
                len(ranked)
            ),
            "top_30": ranked[:TOP_N],
        }

    earliest = datetime.strptime(
        "2025-11-08", "%Y-%m-%d"
    ).date()

    if start < earliest:
        return None

    missing_days = []

    if start <= anchor <= end:
        missing_days.append(
            anchor.isoformat()
        )

    deltas = {}

    for (
        (prev_day, prev),
        (day, curr),
    ) in zip(
        available_snapshots,
        available_snapshots[1:],
    ):
        if (
            day <= anchor
            or day < start
            or day > end
        ):
            continue

        gap = (day - prev_day).days

        if gap > 1:
            missing_days.extend(
                (
                    prev_day
                    + timedelta(days=i)
                ).isoformat()
                for i in range(1, gap)
            )

        day_counts = {}

        for key, current in (
            curr["articles"].items()
        ):
            previous = (
                prev["articles"].get(key)
            )

            if previous is None:
                continue

            a = (
                current["abstract"]
                - previous["abstract"]
            )

            p = (
                current["pdf"]
                - previous["pdf"]
            )

            if a < 0 or p < 0:
                continue

            day_counts[key] = {
                "abstract": a,
                "pdf": p,
            }

        deltas[day] = day_counts

    latest_day = available_snapshots[-1][0]

    if latest_day != end:
        return None

    ranked = []

    for article in articles:
        key = str(article["id"])
        a = 0
        p = 0

        if start <= event_end:
            daily_records = (
                baseline["daily"].get(key, {})
            )

            for date_str, counts in (
                daily_records.items()
            ):
                day = datetime.strptime(
                    date_str, "%Y%m%d"
                ).date()

                if (
                    start <= day
                    and day <= min(
                        end, event_end
                    )
                ):
                    a += counts["abstract"]
                    p += counts["pdf"]

        for day, values in deltas.items():
            counts = values.get(key)

            if counts:
                a += counts["abstract"]
                p += counts["pdf"]

        ranked.append({
            **article,
            "abstract": a,
            "pdf": p,
            "total": a + p,
        })

    ranked.sort(
        key=lambda item: (
            -item["total"],
            item["id"],
        )
    )

    return {
        "period": f"last_{days}_days",
        "from": start.isoformat(),
        "through": end.isoformat(),
        "method": (
            "historical_events_plus_snapshot_deltas"
        ),
        "coverage": (
            "partial"
            if missing_days
            else "complete_with_snapshot_estimates"
        ),
        "missing_event_days": sorted(
            set(missing_days)
        ),
        "articles_with_complete_window": (
            len(ranked)
            if not missing_days
            else 0
        ),
        "articles_ranked": len(ranked),
        "top_30": ranked[:TOP_N],
    }


def main():
    existing_ids = json.loads(
        IDS_FILE.read_text(
            encoding="utf-8"
        )
    )

    if (
        not isinstance(existing_ids, list)
        or not existing_ids
        or any(
            type(article_id) is not int
            or article_id <= 0
            for article_id in existing_ids
        )
        or len(existing_ids)
        != len(set(existing_ids))
    ):
        raise ValueError(
            "article_ids.json must contain "
            "unique positive integer IDs"
        )

    discovered_ids = (
        discover_article_ids()
    )

    ids = sorted(
        set(existing_ids)
        | discovered_ids
    )

    new_ids = sorted(
        discovered_ids
        - set(existing_ids)
    )

    print(
        f"New publications: {len(new_ids)}",
        flush=True,
    )

    if new_ids:
        print(
            f"New article IDs: {new_ids}",
            flush=True,
        )

    print(
        f"Reading {len(ids)} articles",
        flush=True,
    )

    articles = []

    for index, article_id in enumerate(
        ids, start=1
    ):
        article = read_article(
            article_id
        )

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
        key=lambda item: (
            -item["total"],
            item["id"],
        )
    )

    now = datetime.now(timezone.utc)

    history = update_history(
        articles, now
    )

    output = {
        "updated_at": now.isoformat(
            timespec="seconds"
        ),
        "period": "all_time",
        "metric": (
            "abstract_views_plus_file_downloads"
        ),
        "articles_checked": len(articles),
        "top_30": articles[:TOP_N],
    }

    baseline = load_daily_baseline()

    for days, key in (
        (90, "last_90_days"),
        (365, "last_365_days"),
    ):
        result = period_ranking(
            articles,
            history,
            now,
            days,
            baseline,
        )

        if result is not None:
            output[key] = result
        else:
            print(
                f"{key}: collecting history; "
                "no full daily window yet",
                flush=True,
            )

    temporary = OUTPUT_FILE.with_suffix(
        ".json.tmp"
    )

    temporary.write_text(
        json.dumps(
            output,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    temporary.replace(
        OUTPUT_FILE
    )

    history_tmp = HISTORY_FILE.with_suffix(
        ".json.tmp"
    )

    history_tmp.write_text(
        json.dumps(
            history,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    history_tmp.replace(
        HISTORY_FILE
    )

    IDS_FILE.write_text(
        json.dumps(
            ids, indent=2
        ) + "\n",
        encoding="utf-8",
    )

    print(
        f"Success: {len(articles)} "
        f"articles checked. "
        f"Saved {OUTPUT_FILE}",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            f"UPDATE FAILED: {error}",
            file=sys.stderr,
        )
        sys.exit(1)
