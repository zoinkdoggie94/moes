#!/usr/bin/env python3

import hashlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag


BASE_URL = "https://moe.mohkg1017.pro"

OUTPUT_FILE = Path("apps.json")

MAX_PAGES = 100
MAX_WORKERS = 8
REQUEST_TIMEOUT = 35
MAX_VERSIONS_PER_APP = 10


session = requests.Session()

session.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0 Safari/537.36 "
            "ShrubMoeSource/1.0"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "application/json;q=0.8,*/*;q=0.7"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }
)


APP_PATH_RE = re.compile(
    r"^/app/(app_\d+_\d+)(?:/)?$",
    re.IGNORECASE,
)

APPLE_ID_RE = re.compile(
    r"/id(\d+)(?:[/?#]|$)",
    re.IGNORECASE,
)

SIZE_RE = re.compile(
    r"([0-9]+(?:\.[0-9]+)?)\s*(KB|MB|GB|TB)",
    re.IGNORECASE,
)

UPDATED_RE = re.compile(
    r"\bUpdated\s+"
    r"([A-Z][a-z]{2,8})\s+"
    r"(\d{1,2})"
    r"(?:,?\s+(\d{4}))?",
    re.IGNORECASE,
)


def log(message: str) -> None:
    print(message, flush=True)


def fetch(
    url: str,
    tries: int = 4,
) -> requests.Response:
    last_error = None

    for attempt in range(tries):
        try:
            response = session.get(
                url,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )

            if response.status_code == 429:
                raise RuntimeError(
                    f"Rate limited by {url}"
                )

            if response.status_code >= 500:
                raise RuntimeError(
                    f"Server returned HTTP "
                    f"{response.status_code}: {url}"
                )

            response.raise_for_status()

            return response

        except Exception as exc:
            last_error = exc

            if attempt + 1 >= tries:
                break

            delay = min(
                8,
                1.25 * (2 ** attempt),
            )

            time.sleep(delay)

    raise RuntimeError(
        f"Failed to fetch {url}: "
        f"{last_error}"
    )


def clean_text(
    value: Any,
) -> str:
    if value is None:
        return ""

    return re.sub(
        r"\s+",
        " ",
        str(value),
    ).strip()


def absolute_url(
    url: str,
) -> str:
    if not url:
        return ""

    return urljoin(
        BASE_URL + "/",
        url.strip(),
    )


def get_host(
    url: str,
) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def parse_size_bytes(
    text: str,
) -> int:
    match = SIZE_RE.search(
        text or ""
    )

    if not match:
        return 0

    amount = float(
        match.group(1)
    )

    unit = (
        match.group(2)
        .upper()
    )

    multipliers = {
        "KB": 1024,
        "MB": 1024 ** 2,
        "GB": 1024 ** 3,
        "TB": 1024 ** 4,
    }

    return int(
        amount *
        multipliers[unit]
    )


def parse_update_date(
    text: str,
) -> str:
    today = (
        datetime
        .now(timezone.utc)
        .date()
    )

    match = UPDATED_RE.search(
        text or ""
    )

    if not match:
        return today.isoformat()

    month_name = match.group(1)
    day = match.group(2)
    year_text = match.group(3)

    year = (
        int(year_text)
        if year_text
        else today.year
    )

    parsed = None

    for fmt in (
        "%b %d %Y",
        "%B %d %Y",
    ):
        try:
            parsed = datetime.strptime(
                f"{month_name} {day} {year}",
                fmt,
            ).date()

            break

        except ValueError:
            continue

    if parsed is None:
        return today.isoformat()

    if (
        not year_text
        and parsed > today
        and (parsed - today).days > 45
    ):
        parsed = parsed.replace(
            year=year - 1
        )

    return parsed.isoformat()


def google_drive_file_id(
    url: str,
) -> str | None:
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

    match = re.search(
        r"/file/d/([^/?#]+)",
        parsed.path,
    )

    if match:
        return match.group(1)

    match = re.search(
        r"/d/([^/?#]+)",
        parsed.path,
    )

    if match:
        return match.group(1)

    query = parse_qs(
        parsed.query
    )

    ids = query.get("id")

    if ids:
        return ids[0]

    return None


def normalize_download_url(
    url: str,
) -> str:
    if not url:
        return ""

    url = absolute_url(url)

    file_id = google_drive_file_id(
        url
    )

    if file_id:
        return (
            "https://drive.usercontent.google.com/"
            f"download?id={file_id}"
            "&export=download"
            "&confirm=t"
        )

    return url


def is_download_url(
    url: str,
) -> bool:
    if not url:
        return False

    host = get_host(url)

    lower_url = url.lower()

    return (
        lower_url.endswith(".ipa")
        or
        ".ipa?" in lower_url
        or
        "drive.google.com" in host
        or
        "drive.usercontent.google.com" in host
        or
        "drive.proton.me" in host
    )


def is_app_store_url(
    url: str,
) -> bool:
    return (
        "apps.apple.com"
        in get_host(url)
    )


def find_card(
    detail_link: Tag,
) -> Tag | None:
    node = detail_link

    for _ in range(10):
        parent = node.parent

        if not isinstance(
            parent,
            Tag,
        ):
            break

        node = parent

        anchors = node.find_all(
            "a",
            href=True,
        )

        has_detail = False
        has_download = False

        for anchor in anchors:
            href = absolute_url(
                str(
                    anchor.get(
                        "href",
                        "",
                    )
                )
            )

            path = urlparse(
                href
            ).path

            if APP_PATH_RE.match(
                path
            ):
                has_detail = True

            if is_download_url(
                href
            ):
                has_download = True

        if (
            has_detail
            and has_download
        ):
            return node

    return None


def discover_catalog():
    discovered = {}

    empty_pages = 0

    for page in range(
        1,
        MAX_PAGES + 1,
    ):
        if page == 1:
            url = BASE_URL + "/"
        else:
            url = (
                BASE_URL +
                f"/?page={page}"
            )

        response = fetch(url)

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        page_app_ids = set()

        anchors = soup.find_all(
            "a",
            href=True,
        )

        for anchor in anchors:
            href = absolute_url(
                str(
                    anchor.get(
                        "href",
                        "",
                    )
                )
            )

            parsed = urlparse(
                href
            )

            match = APP_PATH_RE.match(
                parsed.path
            )

            if not match:
                continue

            app_id = match.group(1)

            page_app_ids.add(
                app_id
            )

            record = discovered.setdefault(
                app_id,
                {
                    "app_id": app_id,
                    "detail_url": (
                        f"{BASE_URL}/"
                        f"app/{app_id}"
                    ),
                    "app_store_url": "",
                    "download_url": "",
                    "icon_url": "",
                },
            )

            card = find_card(
                anchor
            )

            if card is None:
                continue

            for card_anchor in card.find_all(
                "a",
                href=True,
            ):
                candidate = absolute_url(
                    str(
                        card_anchor.get(
                            "href",
                            "",
                        )
                    )
                )

                if (
                    is_app_store_url(candidate)
                    and
                    not record["app_store_url"]
                ):
                    record[
                        "app_store_url"
                    ] = candidate

                if (
                    is_download_url(candidate)
                    and
                    not record["download_url"]
                ):
                    record[
                        "download_url"
                    ] = candidate

            image = card.find(
                "img",
                src=True,
            )

            if (
                image
                and
                not record["icon_url"]
            ):
                record["icon_url"] = (
                    absolute_url(
                        str(
                            image.get(
                                "src",
                                "",
                            )
                        )
                    )
                )

        log(
            f"Page {page}: "
            f"{len(page_app_ids)} apps "
            f"| total {len(discovered)}"
        )

        if page_app_ids:
            empty_pages = 0
        else:
            empty_pages += 1

        next_page = page + 1

        has_next = False

        for anchor in anchors:
            href = str(
                anchor.get(
                    "href",
                    "",
                )
            )

            if (
                f"page={next_page}"
                in href
            ):
                has_next = True
                break

        if (
            page > 1
            and page_app_ids
            and not has_next
        ):
            break

        if empty_pages >= 2:
            break

    if not discovered:
        raise RuntimeError(
            "Found zero apps. "
            "Refusing to overwrite apps.json."
        )

    return discovered


def meta_content(
    soup: BeautifulSoup,
    *selectors,
) -> str:
    for attribute, value in selectors:
        tag = soup.find(
            "meta",
            attrs={
                attribute: value,
            },
        )

        if (
            tag
            and
            tag.get("content")
        ):
            return clean_text(
                tag.get("content")
            )

    return ""


def extract_name(
    soup: BeautifulSoup,
    fallback: str,
) -> str:
    h1 = soup.find("h1")

    if h1:
        name = clean_text(
            h1.get_text(
                " ",
                strip=True,
            )
        )

        if name:
            return name

    og_title = meta_content(
        soup,
        (
            "property",
            "og:title",
        ),
    )

    if og_title:
        return og_title

    title = soup.find(
        "title"
    )

    if title:
        text = clean_text(
            title.get_text(
                " ",
                strip=True,
            )
        )

        if text:
            return text

    return fallback


def extract_description(
    soup: BeautifulSoup,
    name: str,
) -> str:
    headings = soup.find_all(
        re.compile(
            r"^h[1-6]$"
        )
    )

    for heading in headings:
        heading_text = clean_text(
            heading.get_text(
                " ",
                strip=True,
            )
        ).lower()

        if heading_text != "about":
            continue

        siblings = heading.find_all_next(
            limit=12
        )

        for sibling in siblings:
            if sibling is heading:
                continue

            if (
                sibling.name
                and
                re.match(
                    r"^h[1-6]$",
                    sibling.name,
                )
            ):
                break

            if sibling.name not in {
                "p",
                "div",
                "span",
            }:
                continue

            text = clean_text(
                sibling.get_text(
                    " ",
                    strip=True,
                )
            )

            if (
                len(text) >= 8
                and
                "all rights reserved"
                not in text.lower()
            ):
                return text

    description = meta_content(
        soup,
        (
            "name",
            "description",
        ),
        (
            "property",
            "og:description",
        ),
    )

    if description:
        return description

    return (
        f"{name} from "
        "Moe's App Hub."
    )


def extract_version(
    soup: BeautifulSoup,
) -> str:
    for item in soup.find_all(
        [
            "li",
            "span",
            "div",
            "p",
        ]
    ):
        text = clean_text(
            item.get_text(
                " ",
                strip=True,
            )
        )

        if (
            not text
            or
            len(text) > 80
        ):
            continue

        match = re.search(
            r"(?:version\s*:?\s*|^v)"
            r"([0-9]+(?:\.[0-9A-Za-z_-]+)+)",
            text,
            re.IGNORECASE,
        )

        if match:
            return match.group(1)

    body = clean_text(
        soup.get_text(
            " ",
            strip=True,
        )
    )

    match = re.search(
        r"\bv"
        r"([0-9]+(?:\.[0-9A-Za-z_-]+)+)",
        body,
        re.IGNORECASE,
    )

    if match:
        return match.group(1)

    return "0"


def extract_detail(
    record,
):
    response = fetch(
        record["detail_url"]
    )

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    app_id = record[
        "app_id"
    ]

    name = extract_name(
        soup,
        app_id,
    )

    description = (
        extract_description(
            soup,
            name,
        )
    )

    version = extract_version(
        soup
    )

    page_text = clean_text(
        soup.get_text(
            " ",
            strip=True,
        )
    )

    size = parse_size_bytes(
        page_text
    )

    release_date = (
        parse_update_date(
            page_text
        )
    )

    download_url = record.get(
        "download_url",
        "",
    )

    app_store_url = record.get(
        "app_store_url",
        "",
    )

    icon_url = record.get(
        "icon_url",
        "",
    )

    anchors = soup.find_all(
        "a",
        href=True,
    )

    for anchor in anchors:
        href = absolute_url(
            str(
                anchor.get(
                    "href",
                    "",
                )
            )
        )

        text = clean_text(
            anchor.get_text(
                " ",
                strip=True,
            )
        ).lower()

        if (
            is_download_url(href)
            and
            (
                not download_url
                or
                "download" in text
                or
                "ipa" in text
            )
        ):
            download_url = href

            if (
                "download" in text
                or
                "ipa" in text
            ):
                break

    if not app_store_url:
        for anchor in anchors:
            href = absolute_url(
                str(
                    anchor.get(
                        "href",
                        "",
                    )
                )
            )

            if is_app_store_url(
                href
            ):
                app_store_url = href
                break

    og_image = meta_content(
        soup,
        (
            "property",
            "og:image",
        ),
        (
            "name",
            "twitter:image",
        ),
    )

    if og_image:
        icon_url = absolute_url(
            og_image
        )

    if not icon_url:
        image = soup.find(
            "img",
            src=True,
        )

        if image:
            icon_url = absolute_url(
                str(
                    image.get(
                        "src",
                        "",
                    )
                )
            )

    return {
        "app_id": app_id,
        "name": name,
        "description": description,
        "version": version,
        "date": release_date,
        "size": size,
        "download_url": (
            normalize_download_url(
                download_url
            )
        ),
        "original_download_url": (
            absolute_url(
                download_url
            )
        ),
        "icon_url": icon_url,
        "app_store_url": (
            app_store_url
        ),
        "detail_url": (
            record["detail_url"]
        ),
    }


def apple_id_from_url(
    url: str,
) -> str | None:
    if not url:
        return None

    match = APPLE_ID_RE.search(
        url
    )

    if not match:
        return None

    return match.group(1)


@lru_cache(maxsize=512)
def apple_lookup(
    app_store_url: str,
) -> dict[str, Any]:
    apple_id = apple_id_from_url(
        app_store_url
    )

    if not apple_id:
        return {}

    try:
        response = fetch(
            "https://itunes.apple.com/"
            f"lookup?id={apple_id}",
            tries=3,
        )

        payload = response.json()

        results = payload.get(
            "results"
        ) or []

        if not results:
            return {}

        app = results[0]

        return {
            "bundleIdentifier": (
                clean_text(
                    app.get(
                        "bundleId"
                    )
                )
            ),
            "developerName": (
                clean_text(
                    app.get(
                        "sellerName"
                    )
                    or
                    app.get(
                        "artistName"
                    )
                )
            ),
            "iconURL": (
                app.get(
                    "artworkUrl512"
                )
                or
                app.get(
                    "artworkUrl100"
                )
                or
                ""
            ),
            "minOSVersion": (
                clean_text(
                    app.get(
                        "minimumOsVersion"
                    )
                )
            ),
            "category": (
                clean_text(
                    app.get(
                        "primaryGenreName"
                    )
                ).lower()
            ),
        }

    except Exception as exc:
        log(
            "::warning::"
            "Apple lookup failed for "
            f"{app_store_url}: {exc}"
        )

        return {}


def fallback_bundle_id(
    app_id: str,
) -> str:
    digest = hashlib.sha1(
        app_id.encode(
            "utf-8"
        )
    ).hexdigest()[:16]

    return (
        "pro.mohkg1017."
        f"moe.{digest}"
    )


def load_previous():
    if not OUTPUT_FILE.exists():
        return {}

    try:
        payload = json.loads(
            OUTPUT_FILE.read_text(
                encoding="utf-8"
            )
        )

    except Exception as exc:
        log(
            "::warning::"
            "Could not read old "
            f"apps.json: {exc}"
        )

        return {}

    previous = {}

    apps = payload.get(
        "apps",
        [],
    )

    if not isinstance(
        apps,
        list,
    ):
        return {}

    for app in apps:
        if not isinstance(
            app,
            dict,
        ):
            continue

        app_id = clean_text(
            app.get(
                "moeAppID"
            )
        )

        if app_id:
            previous[
                app_id
            ] = app

    return previous


def version_key(
    version,
):
    return (
        clean_text(
            version.get(
                "version"
            )
        ),
        clean_text(
            version.get(
                "date"
            )
        ),
        clean_text(
            version.get(
                "downloadURL"
            )
        ),
    )


def merge_versions(
    old_app,
    current,
    min_os,
):
    versions = []

    if old_app:
        old_versions = (
            old_app.get(
                "versions"
            )
            or
            []
        )

        for version in old_versions:
            if isinstance(
                version,
                dict,
            ):
                versions.append(
                    dict(version)
                )

    new_version = {
        "version": (
            current["version"]
        ),
        "date": (
            current["date"]
        ),
        "size": (
            current["size"]
        ),
        "downloadURL": (
            current[
                "download_url"
            ]
        ),
        "localizedDescription": (
            current[
                "description"
            ]
        ),
        "minOSVersion": (
            min_os
        ),
    }

    versions = [
        version
        for version in versions
        if not (
            clean_text(
                version.get(
                    "version"
                )
            )
            ==
            clean_text(
                new_version[
                    "version"
                ]
            )
            and
            clean_text(
                version.get(
                    "date"
                )
            )
            ==
            clean_text(
                new_version[
                    "date"
                ]
            )
        )
    ]

    versions.insert(
        0,
        new_version,
    )

    deduped = []

    seen = set()

    for version in versions:
        version.setdefault(
            "minOSVersion",
            min_os,
        )

        key = version_key(
            version
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


def build_app(
    current,
    old_app,
):
    apple = apple_lookup(
        current[
            "app_store_url"
        ]
    )

    bundle_identifier = (
        apple.get(
            "bundleIdentifier"
        )
        or
        (
            old_app
            or {}
        ).get(
            "bundleIdentifier"
        )
        or
        fallback_bundle_id(
            current[
                "app_id"
            ]
        )
    )

    developer_name = (
        apple.get(
            "developerName"
        )
        or
        (
            old_app
            or {}
        ).get(
            "developerName"
        )
        or
        "Moe's App Hub"
    )

    icon_url = (
        current.get(
            "icon_url"
        )
        or
        apple.get(
            "iconURL"
        )
        or
        (
            old_app
            or {}
        ).get(
            "iconURL"
        )
        or
        f"{BASE_URL}/favicon.ico"
    )

    min_os = (
        apple.get(
            "minOSVersion"
        )
        or
        "12.0"
    )

    category = (
        apple.get(
            "category"
        )
        or
        "utilities"
    )

    versions = merge_versions(
        old_app,
        current,
        min_os,
    )

    return {
        "name": (
            current["name"]
        ),
        "bundleIdentifier": (
            bundle_identifier
        ),
        "developerName": (
            developer_name
        ),
        "subtitle": (
            "Moe's App Hub build"
        ),
        "localizedDescription": (
            current[
                "description"
            ]
        ),
        "iconURL": (
            icon_url
        ),
        "category": (
            category
        ),
        "versions": (
            versions
        ),
        "appPermissions": {
            "entitlements": [],
            "privacy": {},
        },
        "moeAppID": (
            current["app_id"]
        ),
        "moeURL": (
            current[
                "detail_url"
            ]
        ),
        "appStoreURL": (
            current[
                "app_store_url"
            ]
        ),
        "originalDownloadURL": (
            current[
                "original_download_url"
            ]
        ),
    }


def main():
    old_apps = load_previous()

    catalog = discover_catalog()

    records = list(
        catalog.values()
    )

    log(
        f"Discovered "
        f"{len(records)} "
        "unique Moe listings."
    )

    log(
        f"Fetching detail pages "
        f"with {MAX_WORKERS} workers..."
    )

    details = []

    failures = []

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:
        future_map = {
            executor.submit(
                extract_detail,
                record,
            ): record
            for record
            in records
        }

        completed = 0

        for future in as_completed(
            future_map
        ):
            record = future_map[
                future
            ]

            completed += 1

            try:
                detail = (
                    future.result()
                )

                if not detail[
                    "download_url"
                ]:
                    raise RuntimeError(
                        "No IPA/download "
                        "URL found"
                    )

                details.append(
                    detail
                )

            except Exception as exc:
                failures.append(
                    (
                        record[
                            "app_id"
                        ],
                        str(exc),
                    )
                )

                log(
                    "::warning::"
                    f"{record['app_id']} "
                    f"failed: {exc}"
                )

            if (
                completed % 20 == 0
                or
                completed == len(records)
            ):
                log(
                    f"Fetched "
                    f"{completed}/"
                    f"{len(records)} "
                    f"| failed "
                    f"{len(failures)}"
                )

    success_ratio = (
        len(details)
        /
        max(
            1,
            len(records),
        )
    )

    if success_ratio < 0.70:
        raise RuntimeError(
            "Too many Moe pages failed. "
            f"Only {len(details)}/"
            f"{len(records)} succeeded. "
            "Existing apps.json will "
            "not be overwritten."
        )

    apps = []

    details.sort(
        key=lambda item: (
            item.get(
                "date",
                "",
            ),
            item.get(
                "name",
                "",
            ),
        ),
        reverse=True,
    )

    for number, detail in enumerate(
        details,
        start=1,
    ):
        try:
            app = build_app(
                detail,
                old_apps.get(
                    detail[
                        "app_id"
                    ]
                ),
            )

            apps.append(
                app
            )

        except Exception as exc:
            log(
                "::warning::"
                f"Could not build "
                f"{detail['app_id']}: "
                f"{exc}"
            )

        if (
            number % 25 == 0
            or
            number == len(details)
        ):
            log(
                f"Built "
                f"{number}/"
                f"{len(details)}"
            )

    if not apps:
        raise RuntimeError(
            "Generated zero apps. "
            "Refusing to write "
            "an empty source."
        )

    now = (
        datetime
        .now(timezone.utc)
        .isoformat()
    )

    source = {
        "name": (
            "Moe's App Hub "
            "— Full Source"
        ),
        "identifier": (
            "pro.shrubhub."
            "moe-full"
        ),
        "subtitle": (
            f"Full automatic Moe "
            f"catalog — {len(apps)} apps"
        ),
        "description": (
            "Automatically generated "
            "AltStore-compatible source "
            "containing Moe's full public "
            "app catalog."
        ),
        "website": (
            BASE_URL
        ),
        "sourceURL": (
            "apps.json"
        ),
        "tintColor": (
            "#34C759"
        ),
        "apps": (
            apps
        ),
        "news": [],
        "generatedAt": (
            now
        ),
        "stats": {
            "discovered": (
                len(records)
            ),
            "published": (
                len(apps)
            ),
            "failed": (
                len(failures)
            ),
        },
    }

    temp_file = Path(
        "apps.json.tmp"
    )

    temp_file.write_text(
        json.dumps(
            source,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    temp_file.replace(
        OUTPUT_FILE
    )

    log(
        f"DONE: wrote "
        f"{len(apps)} apps "
        "to apps.json"
    )

    if failures:
        log(
            "Failed IDs: "
            +
            ", ".join(
                app_id
                for app_id, _
                in failures
            )
        )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        raise

    except Exception as exc:
        print(
            f"::error::{exc}",
            file=sys.stderr,
        )

        raise
