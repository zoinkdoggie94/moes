#!/usr/bin/env python3

import json
import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests


BASE_URL = "https://moe.mohkg1017.pro"
SOURCE_URL = "https://raw.githubusercontent.com/zoinkdoggie94/moes/main/apps.json"

OUTPUT_FILE = Path("apps.json")

REQUEST_TIMEOUT = 40
PAGE_DELAY_SECONDS = 1.25
MAX_RETRIES = 5
MAX_PAGES = 100
MAX_VERSIONS_PER_APP = 5

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)

APP_ID_RE = re.compile(
    r"/app/(app_(\d+)_(\d+))",
    re.IGNORECASE,
)

COUNT_RE = re.compile(
    r"Showing\s+(\d+)\s+of\s+(\d+)\s+apps",
    re.IGNORECASE,
)

SIZE_RE = re.compile(
    r"([0-9]+(?:\.[0-9]+)?)\s*(B|KB|MB|GB|TB)",
    re.IGNORECASE,
)


def log(message: str) -> None:
    print(message, flush=True)


def make_session():
    session = requests.Session(
        impersonate="chrome"
    )

    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": BASE_URL + "/",
        }
    )

    return session


session = make_session()


def fetch_page(page: int) -> str:
    global session

    last_error = None

    for attempt in range(
        1,
        MAX_RETRIES + 1,
    ):
        try:
            response = session.get(
                BASE_URL + "/",
                params={"page": page},
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code == 403:
                log(
                    f"Page {page}: got HTTP 403 "
                    f"(attempt {attempt}/{MAX_RETRIES})"
                )

                session = make_session()

                if attempt < MAX_RETRIES:
                    time.sleep(
                        min(
                            10,
                            1.5 * attempt,
                        )
                    )
                    continue

            if response.status_code == 429:
                log(
                    f"Page {page}: rate limited "
                    f"(attempt {attempt}/{MAX_RETRIES})"
                )

                if attempt < MAX_RETRIES:
                    time.sleep(
                        min(
                            20,
                            3 * attempt,
                        )
                    )
                    continue

            response.raise_for_status()

            if not response.text.strip():
                raise RuntimeError(
                    "Moe returned an empty page"
                )

            return response.text

        except Exception as exc:
            last_error = exc

            if attempt >= MAX_RETRIES:
                break

            delay = min(
                15,
                1.5 * (2 ** (attempt - 1)),
            )

            log(
                f"Page {page}: request failed: {exc}"
            )

            log(
                f"Retrying in {delay:.1f}s..."
            )

            session = make_session()

            time.sleep(delay)

    raise RuntimeError(
        f"Failed to fetch Moe page {page} "
        f"after {MAX_RETRIES} attempts: "
        f"{last_error}"
    )


def clean_text(value) -> str:
    if value is None:
        return ""

    return re.sub(
        r"\s+",
        " ",
        str(value),
    ).strip()


def absolute_url(url: str) -> str:
    if not url:
        return ""

    return urljoin(
        BASE_URL + "/",
        url,
    )


def parse_size_bytes(text: str) -> int:
    if not text:
        return 0

    match = SIZE_RE.search(text)

    if not match:
        return 0

    value = float(
        match.group(1)
    )

    unit = (
        match.group(2)
        .upper()
    )

    multipliers = {
        "B": 1,
        "KB": 1024,
        "MB": 1024 ** 2,
        "GB": 1024 ** 3,
        "TB": 1024 ** 4,
    }

    return int(
        value *
        multipliers[unit]
    )


def extract_google_drive_id(
    url: str,
):
    if not url:
        return None

    parsed = urlparse(url)

    host = parsed.netloc.lower()

    if (
        "drive.google.com" not in host
        and
        "drive.usercontent.google.com" not in host
    ):
        return None

    query = parse_qs(
        parsed.query
    )

    if query.get("id"):
        return query["id"][0]

    match = re.search(
        r"/(?:file/)?d/([^/?#]+)",
        parsed.path,
    )

    if match:
        return match.group(1)

    return None


def normalize_download_url(
    url: str,
) -> str:
    url = absolute_url(url)

    drive_id = extract_google_drive_id(
        url
    )

    if drive_id:
        return (
            "https://drive.usercontent.google.com/"
            "download"
            f"?id={drive_id}"
            "&export=download"
            "&confirm=t"
        )

    return url


def parse_catalog_count(
    html: str,
):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    text = soup.get_text(
        " ",
        strip=True,
    )

    match = COUNT_RE.search(text)

    if not match:
        return None, None

    per_page = int(
        match.group(1)
    )

    total = int(
        match.group(2)
    )

    if per_page <= 0:
        return total, None

    return total, per_page


def max_pagination_page(
    html: str,
) -> int:
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    maximum = 1

    for anchor in soup.find_all(
        "a",
        href=True,
    ):
        href = str(
            anchor.get(
                "href",
                "",
            )
        )

        try:
            parsed = urlparse(
                absolute_url(href)
            )

            query = parse_qs(
                parsed.query
            )

            values = query.get(
                "page"
            )

            if not values:
                continue

            page = int(
                values[0]
            )

            maximum = max(
                maximum,
                page,
            )

        except Exception:
            continue

    return maximum


def make_bundle_identifier(
    app_id: str,
) -> str:
    match = re.fullmatch(
        r"app_(\d+)_(\d+)",
        app_id,
    )

    if not match:
        safe = re.sub(
            r"[^A-Za-z0-9.-]",
            "-",
            app_id,
        )

        return (
            "com.zoinkdoggie94."
            f"moes.{safe}"
        )

    return (
        "com.zoinkdoggie94.moes."
        f"a{match.group(1)}."
        f"a{match.group(2)}"
    )


def timestamp_to_date(
    timestamp: int,
) -> str:
    try:
        return (
            datetime
            .fromtimestamp(
                timestamp,
                tz=timezone.utc,
            )
            .date()
            .isoformat()
        )

    except Exception:
        return (
            datetime
            .now(timezone.utc)
            .date()
            .isoformat()
        )


def parse_app_cards(
    html: str,
):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    cards = []

    for article in soup.select(
        "article.app-card"
    ):
        detail_link = article.select_one(
            "a.app-card-open-link"
        )

        if detail_link is None:
            continue

        detail_href = str(
            detail_link.get(
                "href",
                "",
            )
        )

        id_match = APP_ID_RE.search(
            detail_href
        )

        if not id_match:
            continue

        app_id = id_match.group(1)

        download_link = article.select_one(
            "a.download-link"
        )

        if download_link is None:
            continue

        download_url = clean_text(
            download_link.get(
                "href",
                "",
            )
        )

        if not download_url:
            continue

        name = clean_text(
            article.get(
                "data-name",
                "",
            )
        )

        if not name:
            heading = article.select_one(
                "h2, h3, .app-name, .app-title"
            )

            if heading:
                name = clean_text(
                    heading.get_text(
                        " ",
                        strip=True,
                    )
                )

        if not name:
            name = app_id

        try:
            data_modified = int(
                article.get(
                    "data-modified",
                    "0",
                )
                or 0
            )

        except Exception:
            data_modified = 0

        icon_url = ""

        icon = article.select_one(
            ".app-icon img"
        )

        if icon is None:
            icon = article.find(
                "img",
                src=True,
            )

        if icon is not None:
            icon_url = absolute_url(
                clean_text(
                    icon.get(
                        "src",
                        "",
                    )
                )
            )

        description = ""

        description_element = (
            article.select_one(
                "p.app-description"
            )
        )

        if description_element:
            description = clean_text(
                description_element.get_text(
                    " ",
                    strip=True,
                )
            )

        changelog = ""

        changelog_element = (
            article.select_one(
                ".app-changelog-preview "
                ".changelog-text"
            )
        )

        if changelog_element:
            changelog = clean_text(
                changelog_element.get_text(
                    " ",
                    strip=True,
                )
            )

        meta_spans = article.select(
            ".app-meta-row span"
        )

        version_text = ""

        size_text = ""

        if len(meta_spans) >= 1:
            version_text = clean_text(
                meta_spans[0].get_text(
                    " ",
                    strip=True,
                )
            )

        if len(meta_spans) >= 2:
            size_text = clean_text(
                meta_spans[1].get_text(
                    " ",
                    strip=True,
                )
            )

        version = re.sub(
            r"^[vV]\s*",
            "",
            version_text,
        ).strip()

        if not version:
            version = "1.0"

        app_store_url = ""

        app_store_link = (
            article.select_one(
                'a[href*="apps.apple.com"]'
            )
        )

        if app_store_link is not None:
            app_store_url = absolute_url(
                clean_text(
                    app_store_link.get(
                        "href",
                        "",
                    )
                )
            )

        detail_url = absolute_url(
            detail_href
        )

        if not description:
            description = (
                f"{name} from Moe's App Hub."
            )

        cards.append(
            {
                "app_id": app_id,
                "name": name,
                "version": version,
                "size": parse_size_bytes(
                    size_text
                ),
                "date": timestamp_to_date(
                    data_modified
                ),
                "data_modified": (
                    data_modified
                ),
                "description": (
                    description
                ),
                "changelog": (
                    changelog
                ),
                "icon_url": (
                    icon_url
                    or
                    BASE_URL + "/favicon.ico"
                ),
                "download_url": (
                    normalize_download_url(
                        download_url
                    )
                ),
                "detail_url": detail_url,
                "app_store_url": (
                    app_store_url
                ),
            }
        )

    return cards


def load_previous_apps():
    if not OUTPUT_FILE.exists():
        return {}

    try:
        data = json.loads(
            OUTPUT_FILE.read_text(
                encoding="utf-8"
            )
        )

    except Exception as exc:
        log(
            f"Warning: couldn't read old "
            f"apps.json: {exc}"
        )

        return {}

    previous = {}

    for app in data.get(
        "apps",
        [],
    ):
        if not isinstance(
            app,
            dict,
        ):
            continue

        bundle_id = clean_text(
            app.get(
                "bundleIdentifier",
                "",
            )
        )

        if bundle_id:
            previous[
                bundle_id
            ] = app

    return previous


def merge_versions(
    old_app,
    card,
):
    new_version = {
        "version": card["version"],
        "date": card["date"],
        "localizedDescription": (
            card["changelog"]
            or
            card["description"]
        ),
        "downloadURL": (
            card["download_url"]
        ),
        "size": card["size"],
    }

    existing = []

    if isinstance(
        old_app,
        dict,
    ):
        old_versions = old_app.get(
            "versions",
            [],
        )

        if isinstance(
            old_versions,
            list,
        ):
            for version in old_versions:
                if isinstance(
                    version,
                    dict,
                ):
                    existing.append(
                        dict(version)
                    )

    merged = [
        new_version
    ]

    for version in existing:
        same_version = (
            clean_text(
                version.get(
                    "version",
                    "",
                )
            )
            ==
            new_version["version"]
        )

        same_download = (
            clean_text(
                version.get(
                    "downloadURL",
                    "",
                )
            )
            ==
            new_version[
                "downloadURL"
            ]
        )

        if (
            same_version
            and
            same_download
        ):
            continue

        merged.append(
            version
        )

    deduped = []

    seen = set()

    for version in merged:
        key = (
            clean_text(
                version.get(
                    "version",
                    "",
                )
            ),
            clean_text(
                version.get(
                    "downloadURL",
                    "",
                )
            ),
        )

        if key in seen:
            continue

        seen.add(key)

        deduped.append(
            version
        )

    return deduped[
        :MAX_VERSIONS_PER_APP
    ]


def build_source_app(
    card,
    previous_apps,
):
    bundle_identifier = (
        make_bundle_identifier(
            card["app_id"]
        )
    )

    old_app = previous_apps.get(
        bundle_identifier
    )

    versions = merge_versions(
        old_app,
        card,
    )

    return {
        "name": card["name"],
        "bundleIdentifier": (
            bundle_identifier
        ),
        "developerName": (
            "Moe's App Hub"
        ),
        "subtitle": (
            "Moe's App Hub"
        ),
        "localizedDescription": (
            card["description"]
        ),
        "iconURL": (
            card["icon_url"]
        ),
        "versions": versions,
    }


def main():
    log(
        "Fetching Moe's App Hub..."
    )

    first_html = fetch_page(1)

    total_apps, per_page = (
        parse_catalog_count(
            first_html
        )
    )

    if (
        total_apps
        and
        per_page
    ):
        total_pages = math.ceil(
            total_apps
            /
            per_page
        )

        total_pages = min(
            total_pages,
            MAX_PAGES,
        )

        log(
            f"Moe reports "
            f"{total_apps} apps, "
            f"{per_page} per page, "
            f"{total_pages} pages."
        )

    else:
        total_pages = (
            max_pagination_page(
                first_html
            )
        )

        total_pages = max(
            1,
            min(
                total_pages,
                MAX_PAGES,
            ),
        )

        log(
            "Couldn't read the displayed "
            "app count; using pagination. "
            f"Detected {total_pages} pages."
        )

    discovered = {}

    first_cards = parse_app_cards(
        first_html
    )

    for card in first_cards:
        discovered[
            card["app_id"]
        ] = card

    log(
        f"Page 1/{total_pages}: "
        f"{len(first_cards)} cards, "
        f"{len(discovered)} unique apps."
    )

    for page in range(
        2,
        total_pages + 1,
    ):
        time.sleep(
            PAGE_DELAY_SECONDS
        )

        html = fetch_page(
            page
        )

        cards = parse_app_cards(
            html
        )

        before = len(
            discovered
        )

        for card in cards:
            old = discovered.get(
                card["app_id"]
            )

            if (
                old is None
                or
                card[
                    "data_modified"
                ]
                >=
                old.get(
                    "data_modified",
                    0,
                )
            ):
                discovered[
                    card["app_id"]
                ] = card

        added = (
            len(discovered)
            -
            before
        )

        log(
            f"Page {page}/{total_pages}: "
            f"{len(cards)} cards, "
            f"+{added} new, "
            f"{len(discovered)} unique."
        )

    if not discovered:
        raise RuntimeError(
            "Found zero apps. "
            "Refusing to overwrite "
            "apps.json."
        )

    if total_apps:
        coverage = (
            len(discovered)
            /
            total_apps
        )

        if coverage < 0.90:
            raise RuntimeError(
                "Catalog scrape looks "
                "incomplete. "
                f"Moe reports {total_apps} "
                f"apps but only "
                f"{len(discovered)} unique "
                "apps were discovered. "
                "Refusing to overwrite "
                "apps.json."
            )

    previous_apps = (
        load_previous_apps()
    )

    cards = list(
        discovered.values()
    )

    cards.sort(
        key=lambda item: (
            item[
                "data_modified"
            ],
            item[
                "name"
            ].lower(),
        ),
        reverse=True,
    )

    apps = []

    for card in cards:
        apps.append(
            build_source_app(
                card,
                previous_apps,
            )
        )

    now = (
        datetime
        .now(timezone.utc)
        .replace(
            microsecond=0
        )
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )

    source = {
        "name": (
            "Moe's App Hub "
            "Full Library"
        ),
        "identifier": (
            "com.zoinkdoggie94.moes"
        ),
        "apiVersion": "v2",
        "subtitle": (
            f"{len(apps)} apps "
            "automatically synced "
            "from Moe's App Hub"
        ),
        "description": (
            "Automatically generated "
            "full Moe's App Hub catalog."
        ),
        "sourceURL": SOURCE_URL,
        "website": BASE_URL,
        "tintColor": "#34C759",
        "apps": apps,
        "news": [],
    }

    temp_file = Path(
        "apps.json.tmp"
    )

    temp_file.write_text(
        json.dumps(
            source,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    temp_file.replace(
        OUTPUT_FILE
    )

    log("")
    log(
        "================================"
    )
    log(
        f"DONE: published "
        f"{len(apps)} apps."
    )

    if total_apps:
        log(
            f"Moe reported: "
            f"{total_apps}"
        )

    log(
        f"Generated at: {now}"
    )

    log(
        f"Source URL: {SOURCE_URL}"
    )

    log(
        "================================"
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        print(
            f"::error::{exc}",
            file=sys.stderr,
        )

        sys.exit(1)
