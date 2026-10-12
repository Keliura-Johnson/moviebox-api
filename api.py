import os
import sys
import time
import hashlib
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

APP_VERSION = "1.2.0"
DEFAULT_ALLOWED_HOSTS = {"test-videos.co.uk"}

ALLOWED_MEDIA_HOSTS = {
    host.strip().lower()
    for host in os.getenv(
        "MEDIA_ALLOWED_HOSTS", ",".join(DEFAULT_ALLOWED_HOSTS)
    ).split(",")
    if host.strip()
}

app = FastAPI(
    title="MovieBox API Web Service",
    description="MovieBox download API with verified movie and TV episode resolution.",
    version=APP_VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {
        "name": "MovieBox API Web Service",
        "version": APP_VERSION,
        "status": "ok",
        "health": "/health",
        "docs": "/docs",
        "media_inspector": "/api/v1/media/inspect",
        "tv_episode_version_diagnostic": "/api/v1/diagnostics/tv-episode-versions",
        "demo_player": "/watch/demo",
    }


@app.get("/health")
async def health():
    return {"status": "ok", "version": APP_VERSION}


@app.get("/api/v1/media/inspect")
async def inspect_media(url: str = Query(..., max_length=2048)):
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()

    if parsed.scheme != "https" or not hostname:
        raise HTTPException(400, "A valid HTTPS media URL is required.")

    if hostname not in ALLOWED_MEDIA_HOSTS:
        raise HTTPException(403, "Host is not allowed.")

    if parsed.username or parsed.password:
        raise HTTPException(400, "Embedded URL credentials are not allowed.")

    try:
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=15
        ) as client:
            async with client.stream(
                "GET", url, headers={"Range": "bytes=0-63"}
            ) as response:
                sample = await response.aread()
                content_type = response.headers.get(
                    "content-type", "unknown"
                ).split(";")[0]
                has_ftyp = len(sample) >= 8 and sample[4:8] == b"ftyp"

                return {
                    "http_status": response.status_code,
                    "host": hostname,
                    "content_type": content_type,
                    "sample_bytes": len(sample),
                    "mp4_ftyp_signature": has_ftyp,
                    "looks_like_mp4": has_ftyp,
                    "result": (
                        "pass"
                        if response.status_code in (200, 206) and has_ftyp
                        else "fail"
                    ),
                }
    except httpx.HTTPError as exc:
        raise HTTPException(
            502, f"Media host request failed: {type(exc).__name__}"
        ) from exc


@app.get("/watch/demo", response_class=HTMLResponse)
async def demo_player():
    video_url = (
        "https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/720/"
        "Big_Buck_Bunny_720_10s_1MB.mp4"
    )

    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Playback Test</title>
<style>
body{{margin:0;padding:24px;background:#0b0712;color:white;
font:16px system-ui,sans-serif}}
main{{max-width:960px;margin:auto}}
video{{width:100%;max-height:75vh;background:#000;border-radius:12px}}
</style>
</head>
<body>
<main>
<h1>Playback test</h1>
<video controls playsinline preload="metadata">
<source src="{video_url}" type="video/mp4">
</video>
</main>
</body>
</html>""",
        headers={"Cache-Control": "no-store"},
    )


def setup_source_path():
    source_dir = str(Path(__file__).resolve().parent / "src")
    if source_dir not in sys.path:
        sys.path.insert(0, source_dir)


def is_placeholder_resource(resource):
    if isinstance(resource, str):
        url = resource
    elif isinstance(resource, dict):
        url = (
            resource.get("resource_link")
            or resource.get("resourceLink")
            or resource.get("url")
            or ""
        )
    else:
        url = (
            getattr(resource, "resource_link", None)
            or getattr(resource, "resourceLink", None)
            or getattr(resource, "url", None)
            or ""
        )

    try:
        parsed = urlparse(str(url))
        hostname = (parsed.hostname or "").lower()
        filename = parsed.path.rsplit("/", 1)[-1].lower()

        if hostname == "macdn.aoneroom.com" and filename.endswith(".mp4"):
            if parsed.path.lower().startswith("/other/"):
                return True
            if filename.startswith("b164fbfb43477929"):
                return True

        return False
    except (TypeError, ValueError):
        return False


def normalize_title(value):
    return "".join(
        character.lower()
        for character in str(value or "")
        if character.isalnum()
    )


def get_subject_type(item):
    value = getattr(
        item,
        "subject_type",
        getattr(item, "subjectType", None),
    )

    if value is None:
        return None

    value = getattr(value, "value", value)

    try:
        return int(value)
    except (TypeError, ValueError):
        text = str(value).strip().lower()

        if text in {"tv", "series", "tv_series", "tv series"}:
            return 2

        if text in {"movie", "film"}:
            return 1

        return None


async def find_moviebox_item(
    client, title, year=None, media_type="movie"
):
    from moviebox_api.v3.core import Search

    clean_title = str(title or "").strip()

    if not clean_title:
        raise HTTPException(400, "A title is required.")

    results = await Search(
        client_session=client,
        query=clean_title,
        per_page=20,
    ).get_content_model()

    all_items = list(getattr(results, "items", []) or [])
    target_type = 2 if media_type == "tv" else 1

    typed_items = [
        item for item in all_items
        if get_subject_type(item) == target_type
    ]

    if not typed_items:
        if media_type == "tv":
            raise HTTPException(
                404,
                "No verified TV-series result was found. "
                "MovieBox did not return a result identifiable as a TV series.",
            )

        typed_items = [
            item for item in all_items
            if getattr(item, "has_resource", False)
        ]

    if media_type == "tv":
        items = typed_items
    else:
        items = [
            item for item in typed_items
            if getattr(item, "has_resource", False)
        ]

    if not items:
        raise HTTPException(
            404,
            f"No downloadable MovieBox {media_type} result was found for this title.",
        )

    target = normalize_title(clean_title)

    def score(item):
        item_title = normalize_title(getattr(item, "title", ""))
        item_year = str(getattr(item, "release_date", "") or "")[:4]

        return (
            int(item_title == target),
            int(bool(target) and (
                target in item_title or item_title in target
            )),
            int(bool(year and item_year == str(year))),
        )

    items.sort(key=score, reverse=True)

    best = items[0]
    best_title = normalize_title(getattr(best, "title", ""))

    if target and target not in best_title and best_title not in target:
        raise HTTPException(
            404,
            f"No sufficiently matching MovieBox {media_type} was found.",
        )

    return best


def get_option_resolution(option):
    value = getattr(option, "resolution", 0)
    value = getattr(value, "value", value)

    try:
        return int(value)
    except (TypeError, ValueError):
        digits = "".join(
            character for character in str(value or "")
            if character.isdigit()
        )
        return int(digits) if digits else 0


def get_movie_options(details):
    return [
        option
        for detector in (
            getattr(details, "resource_detectors", None) or []
        )
        for option in (
            getattr(detector, "resolution_list", None) or []
        )
        if getattr(option, "resource_link", None)
    ]


async def find_moviebox_v2_series_item(session, title, year=None):
    from moviebox_api.v2.constants import SubjectType
    from moviebox_api.v2.core import Search
    from moviebox_api.v2.models import SearchResultsItem

    clean_title = str(title or "").strip()
    if not clean_title:
        raise HTTPException(400, "A title is required.")

    search = Search(
        session=session,
        query=clean_title,
        subject_type=SubjectType.TV_SERIES,
        page=1,
        per_page=20,
    )

    raw_results = await search.get_content()
    raw_items = (
        raw_results.get("items", [])
        if isinstance(raw_results, dict)
        else []
    )

    if not isinstance(raw_items, list) or not raw_items:
        raise HTTPException(
            404,
            "MovieBox v2 returned no TV-series search results for this title.",
        )

    target = normalize_title(clean_title)
    candidates = []

    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue

        item_title = str(raw_item.get("title") or "")
        normalized = normalize_title(item_title)

        if not normalized or not (
            normalized == target
            or target in normalized
            or normalized in target
        ):
            continue

        subject_type = raw_item.get(
            "subjectType", SubjectType.TV_SERIES.value
        )

        try:
            if int(getattr(subject_type, "value", subject_type)) != SubjectType.TV_SERIES.value:
                continue
        except (TypeError, ValueError):
            continue

        subject_id = raw_item.get("subjectId") or raw_item.get("subject_id")
        detail_path = raw_item.get("detailPath") or raw_item.get("detail_path")

        if not subject_id or not detail_path:
            continue

        release_date = str(
            raw_item.get("releaseDate") or raw_item.get("release_date") or ""
        )[:4]

        score = (
            int(normalized == target),
            int(bool(year) and release_date == str(year)),
            int(target in normalized or normalized in target),
        )
        candidates.append((score, raw_item))

    if not candidates:
        raise HTTPException(
            404,
            "MovieBox v2 did not return a matching TV series with the identifiers needed to look up episodes.",
        )

    candidates.sort(key=lambda row: row[0], reverse=True)
    best = candidates[0][1]

    return SearchResultsItem.model_construct(
        subjectId=str(best.get("subjectId") or best.get("subject_id")),
        subjectType=SubjectType.TV_SERIES,
        title=str(best.get("title") or clean_title),
        detailPath=str(best.get("detailPath") or best.get("detail_path")),
    )


async def verify_episode_video_url(
    url, expected_size=None, header_profile="v1_download"
):
    """Inspect a candidate using a documented media-header profile."""
    parsed = urlparse(str(url or ""))

    if parsed.scheme != "https" or not parsed.hostname:
        return False, "invalid_https_url"

    if parsed.username or parsed.password:
        return False, "embedded_credentials"

    if is_placeholder_resource(str(url)):
        return False, "known_placeholder_path"

    try:
        if header_profile == "v1_download":
            from moviebox_api.v1.constants import DOWNLOAD_REQUEST_HEADERS
        elif header_profile == "v2_download":
            from moviebox_api.v2.constants import DOWNLOAD_REQUEST_HEADERS
        else:
            return False, f"unknown_header_profile:{header_profile}"

        timeout = httpx.Timeout(
            connect=12, read=15, write=10, pool=10
        )

        headers = dict(DOWNLOAD_REQUEST_HEADERS)
        headers["Range"] = "bytes=0-63"

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
        ) as media_client:
            async with media_client.stream(
                "GET", str(url), headers=headers
            ) as response:
                status = response.status_code
                final_host = (response.url.host or "").lower()
                content_type = response.headers.get(
                    "content-type", ""
                ).split(";")[0].strip().lower()
                upgrade = response.headers.get("upgrade")
                content_range = response.headers.get("content-range", "")
                length_text = response.headers.get("content-length")

                content_length = (
                    int(length_text)
                    if length_text and length_text.isdigit()
                    else None
                )

                if status not in (200, 206):
                    body_prefix = b""

                    try:
                        async for chunk in response.aiter_bytes():
                            body_prefix = chunk[:256]
                            break
                    except Exception:
                        pass

                    sample = " ".join(
                        body_prefix.decode(
                            "utf-8", errors="replace"
                        ).split()
                    )[:180]

                    detail = (
                        f"http_{status};profile={header_profile};host={final_host};"
                        f"content_type={content_type or 'unknown'};"
                        f"upgrade={upgrade or 'none'};"
                        f"content_range={content_range or 'none'};"
                        f"content_length={content_length if content_length is not None else 'unknown'};"
                        f"sample={sample or 'empty'}"
                    )

                    return False, detail

                if is_placeholder_resource(str(response.url)):
                    return False, (
                        f"redirected_to_placeholder;profile={header_profile};"
                        f"host={final_host}"
                    )

                if content_type in {
                    "text/html", "application/json", "text/plain"
                }:
                    body_prefix = b""

                    try:
                        async for chunk in response.aiter_bytes():
                            body_prefix = chunk[:256]
                            break
                    except Exception:
                        pass

                    sample = " ".join(
                        body_prefix.decode(
                            "utf-8", errors="replace"
                        ).split()
                    )[:180]

                    return False, (
                        f"unexpected_content_type:{content_type};"
                        f"profile={header_profile};host={final_host};"
                        f"sample={sample or 'empty'}"
                    )

                actual_total = None

                if "/" in content_range:
                    total_text = content_range.rsplit("/", 1)[-1].strip()

                    if total_text.isdigit():
                        actual_total = int(total_text)

                if expected_size and expected_size > 0:
                    if (
                        actual_total is not None
                        and actual_total != expected_size
                    ):
                        return False, (
                            f"size_mismatch:reported={expected_size},actual={actual_total};"
                            f"profile={header_profile};host={final_host}"
                        )

                    if status == 200 and content_length is not None:
                        if content_length != expected_size:
                            return False, (
                                f"size_mismatch:reported={expected_size},actual={content_length};"
                                f"profile={header_profile};host={final_host}"
                            )

                    if status == 206 and actual_total is None:
                        return False, (
                            f"missing_total_content_range;profile={header_profile};"
                            f"host={final_host};content_range={content_range or 'none'}"
                        )

                    if status == 200 and content_length is None:
                        return False, (
                            f"cannot_verify_total_size;profile={header_profile};"
                            f"host={final_host}"
                        )

                sample = b""

                try:
                    async for chunk in response.aiter_bytes():
                        sample = chunk[:64]
                        break
                except Exception:
                    pass

                if len(sample) < 8 or sample[4:8] != b"ftyp":
                    decoded_sample = " ".join(
                        sample[:128].decode(
                            "utf-8", errors="replace"
                        ).split()
                    )[:100]

                    return False, (
                        f"not_mp4_ftyp;profile={header_profile};host={final_host};"
                        f"content_type={content_type or 'unknown'};"
                        f"sample={decoded_sample or 'binary_or_empty'}"
                    )

                return True, (
                    f"verified;profile={header_profile};host={final_host};"
                    f"content_type={content_type or 'unknown'};"
                    f"actual_size={actual_total or content_length or 'unknown'}"
                )

    except Exception as exc:
        return False, (
            f"verification_error;profile={header_profile};"
            f"type={type(exc).__name__};message={str(exc)[:160]}"
        )


async def get_episode_resources(
    session, item, season, episode, checks=None
):
    """Fetch v2 episode resources and verify them before exposing their URLs."""
    from moviebox_api.v2.download import DownloadableTVSeriesFilesDetail

    if season < 1 or episode < 1:
        raise HTTPException(
            400, "Season and episode must be positive numbers."
        )

    downloader = DownloadableTVSeriesFilesDetail(
        session=session,
        item=item,
    )

    raw_metadata = await downloader.get_content(
        int(season), int(episode)
    )

    if not isinstance(raw_metadata, dict):
        raise HTTPException(
            502,
            "MovieBox returned an unexpected episode-resource response.",
        )

    downloads = raw_metadata.get("downloads") or []

    if not isinstance(downloads, list):
        raise HTTPException(
            502,
            "MovieBox returned an invalid episode downloads list.",
        )

    if not downloads and checks is not None:
        checks.append({
            "result": "no_candidate_records",
            "reason": "v2_episode_endpoint_returned_an_empty_downloads_list",
        })

    found = []

    for media_file in downloads:
        if not isinstance(media_file, dict):
            continue

        url = str(
            media_file.get("url")
            or media_file.get("resource_link")
            or media_file.get("resourceLink")
            or ""
        ).strip()

        if not url:
            if checks is not None:
                checks.append({
                    "result": "rejected",
                    "reason": "missing_resource_url",
                })
            continue

        try:
            resolution = int(media_file.get("resolution") or 0)
            size = int(media_file.get("size") or 0)
        except (TypeError, ValueError):
            resolution, size = 0, 0

        if resolution <= 0 or size <= 0:
            if checks is not None:
                checks.append({
                    "resolution": resolution or None,
                    "reported_size_bytes": size or None,
                    "result": "rejected",
                    "reason": "missing_or_invalid_resolution_or_size",
                })
            continue

        if checks is not None:
            # Compare the documented downloader headers against the v2
            # metadata client's download headers.
            documented_valid = False

            for profile in ("v1_download", "v2_download"):
                valid, detail = await verify_episode_video_url(
                    url, size, header_profile=profile
                )

                checks.append({
                    "resolution": resolution,
                    "reported_size_bytes": size,
                    "header_profile": profile,
                    "result": "verified" if valid else "rejected",
                    "reason": detail,
                })

                if profile == "v1_download":
                    documented_valid = valid

            is_valid = documented_valid

        else:
            is_valid, _detail = await verify_episode_video_url(
                url, size, header_profile="v1_download"
            )

        if not is_valid:
            continue

        found.append({
            "resolution": resolution,
            "size": size,
            "resource_link": url,
            "title": f"Episode #{season}.{episode}",
            "season": int(season),
            "episode": int(episode),
            "resource_id": str(media_file.get("id") or ""),
            "api_version": "v2",
        })

    unique = {}

    for resource in found:
        key = (
            resource["season"],
            resource["episode"],
            resource["resolution"],
            resource["resource_link"],
        )
        unique[key] = resource

    resources = list(unique.values())
    resources.sort(key=lambda resource: resource["resolution"])

    return resources


async def get_verified_tv_episode_v2(
    title, year, season, episode, checks=None
):
    """Resolve an episode through the documented v2 series endpoint."""
    from moviebox_api.v2.requests import Session

    session = Session(timeout=30)

    async with session._client:
        item = await find_moviebox_v2_series_item(
            session, title, year
        )

        resources = await get_episode_resources(
            session, item, season, episode, checks=checks
        )

        return {
            "title": str(getattr(item, "title", None) or title),
            "subject_id": str(getattr(item, "subjectId", "") or ""),
            "resources": resources,
        }


def _as_positive_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        digits = "".join(
            character for character in str(value or "")
            if character.isdigit()
        )
        return int(digits) if digits else default


def _title_candidate_score(item, title, year=None):
    raw_title = getattr(item, "title", "")
    release_date = str(getattr(item, "release_date", "") or "")[:4]
    normalized = normalize_title(raw_title)
    target = normalize_title(title)

    if not normalized or not target:
        return (-1, -1, -1)

    return (
        int(normalized == target),
        int(bool(year) and release_date == str(year)),
        int(target in normalized or normalized in target),
    )


async def collect_v3_tv_episode(title, year, season, episode):
    """Follow the library's v3 series flow and verify the resulting resources."""
    result = {
        "api_version": "v3",
        "title": str(title),
        "subject_id": None,
        "download_subject_id": None,
        "dub": None,
        "resources": [],
        "checks": [],
        "error": None,
    }

    try:
        from moviebox_api.v3.http_client import MovieBoxHttpClient
        from moviebox_api.v3.core import (
            Search,
            ItemDetails,
            SeasonDetails,
            DownloadableFilesDetail,
        )
        from moviebox_api.v3.constants import (
            SubjectType,
            CustomResolutionType,
        )
        from moviebox_api.v3.helpers import (
            get_download_tv_series_request_params,
            get_dub_or_raise,
        )

        async with MovieBoxHttpClient(timeout=30) as client:
            search = Search(
                client_session=client,
                query=str(title).strip(),
                subject_type=SubjectType.TV_SERIES,
                page=1,
                per_page=20,
            )

            search_results = await search.get_content_model()
            search_items = list(
                getattr(search_results, "items", None) or []
            )

            matching = [
                (_title_candidate_score(item, title, year), item)
                for item in search_items
                if _title_candidate_score(item, title, year)[2] == 1
            ]

            if not matching:
                raise RuntimeError(
                    "v3 search returned no matching TV-series result"
                )

            matching.sort(key=lambda entry: entry[0], reverse=True)
            series_item = matching[0][1]

            result["title"] = str(
                getattr(series_item, "title", None) or title
            )
            result["subject_id"] = str(
                getattr(series_item, "subject_id", "") or ""
            )

            release_date = str(
                getattr(series_item, "release_date", "") or ""
            )

            details = await ItemDetails(
                client_session=client
            ).get_content_model(series_item.subject_id)

            dubs = list(getattr(details, "dubs", None) or [])

            if dubs:
                try:
                    dub = get_dub_or_raise(details)
                except Exception:
                    dub = next(
                        (
                            entry for entry in dubs
                            if str(
                                getattr(entry, "lan_name", "")
                            ).lower() == "original"
                            or str(
                                getattr(entry, "lan_code", "")
                            ).lower() == "original"
                            or bool(getattr(entry, "original", False))
                        ),
                        dubs[0],
                    )

                download_subject_id = str(
                    getattr(dub, "subject_id", None)
                    or series_item.subject_id
                )

                result["dub"] = str(
                    getattr(dub, "lan_name", None)
                    or getattr(dub, "lan_code", None)
                    or "selected"
                )

            else:
                download_subject_id = str(series_item.subject_id)

            result["download_subject_id"] = download_subject_id

            seasons_model = await SeasonDetails(
                client_session=client
            ).get_content_model(download_subject_id)

            seasons = list(
                getattr(seasons_model, "seasons", None) or []
            )

            if not seasons:
                raise RuntimeError(
                    "v3 season-details response contained no seasons"
                )

            plan = get_download_tv_series_request_params(
                seasons=seasons,
                season=int(season),
                episode=int(episode),
                limit=1,
            )

            for params in list(
                getattr(plan, "request_params", None) or []
            ):
                page = int(getattr(params, "page", 1) or 1)
                per_page = int(getattr(params, "per_page", 20) or 20)
                offset = int(getattr(params, "offset", 0) or 0)
                limit = int(getattr(params, "limit", 1) or 1)

                for quality in (360, 480, 720, 1080):
                    try:
                        request = DownloadableFilesDetail(
                            client,
                            page=page,
                            per_page=per_page,
                            resolution=CustomResolutionType(
                                f"{quality}P"
                            ),
                        )

                        file_details = await request.get_content_model(
                            download_subject_id,
                            release_date=release_date,
                        )

                    except Exception as exc:
                        result["checks"].append({
                            "resolution": quality,
                            "result": "request_error",
                            "reason": (
                                f"{type(exc).__name__}:{str(exc)[:180]}"
                            ),
                        })
                        continue

                    entries = list(
                        getattr(file_details, "list", None) or []
                    )

                    entries = entries[offset:][:limit]

                    matched = [
                        entry for entry in entries
                        if _as_positive_int(
                            getattr(entry, "season", 0)
                        ) == int(season)
                        and _as_positive_int(
                            getattr(entry, "episode", 0)
                        ) == int(episode)
                    ]

                    if not matched:
                        result["checks"].append({
                            "resolution": quality,
                            "result": "no_episode_record",
                            "reason": (
                                "requested_episode_not_in_v3_resource_list"
                            ),
                        })
                        continue

                    for video in matched:
                        actual_quality = (
                            get_option_resolution(video) or quality
                        )
                        size = _as_positive_int(
                            getattr(video, "size", 0)
                        )
                        duration = getattr(video, "duration", None)
                        url = str(
                            getattr(video, "resource_link", "") or ""
                        )

                        if duration is not None:
                            duration_int = _as_positive_int(duration)

                            if 0 < duration_int < 60:
                                result["checks"].append({
                                    "resolution": actual_quality,
                                    "reported_size_bytes": size or None,
                                    "duration": duration_int,
                                    "result": "rejected",
                                    "reason": (
                                        f"duration_too_short:{duration_int}s"
                                    ),
                                })
                                continue

                        valid, detail = await verify_episode_video_url(
                            url,
                            size,
                            header_profile="v1_download",
                        )

                        actual_size = None

                        if detail and "size_mismatch:" in detail:
                            try:
                                actual_size = int(
                                    detail.split("actual=", 1)[1]
                                    .split(";", 1)[0]
                                )
                            except (IndexError, ValueError):
                                pass

                        result["checks"].append({
                            "resolution": actual_quality,
                            "reported_size_bytes": size or None,
                            "actual_size_bytes": actual_size,
                            "duration": duration,
                            "result": "verified" if valid else "rejected",
                            "reason": detail,
                        })

                        if valid:
                            result["resources"].append({
                                "resolution": actual_quality,
                                "size": size,
                                "resource_link": url,
                                "title": str(
                                    getattr(video, "title", None)
                                    or f"Episode #{season}.{episode}"
                                ),
                                "season": int(season),
                                "episode": int(episode),
                                "duration": duration,
                                "api_version": "v3",
                            })

    except Exception as exc:
        result["error"] = (
            f"{type(exc).__name__}: {str(exc)[:300]}"
        )

    unique = {}

    for resource in result["resources"]:
        unique[(
            resource["resolution"],
            resource["resource_link"],
        )] = resource

    result["resources"] = sorted(
        unique.values(),
        key=lambda entry: entry["resolution"],
    )

    return result


async def get_verified_tv_episode(
    title, year, season, episode, diagnostic=False
):
    v3 = await collect_v3_tv_episode(
        title, year, season, episode
    )

    v2_checks = [] if diagnostic else None

    try:
        v2 = await get_verified_tv_episode_v2(
            title,
            year,
            season,
            episode,
            checks=v2_checks,
        )

        v2 = {
            "api_version": "v2",
            "title": v2.get("title", title),
            "subject_id": v2.get("subject_id"),
            "resources": [
                {**resource, "api_version": "v2"}
                for resource in v2.get("resources", [])
            ],
            "checks": v2_checks or [],
            "error": None,
        }

    except Exception as exc:
        v2 = {
            "api_version": "v2",
            "title": str(title),
            "subject_id": None,
            "resources": [],
            "checks": v2_checks or [],
            "error": (
                f"{type(exc).__name__}: {str(exc)[:300]}"
            ),
        }

    combined = {}

    for version in (v3, v2):
        for resource in version.get("resources", []):
            key = (
                resource["resolution"],
                resource["resource_link"],
            )

            if (
                key not in combined
                or resource.get("api_version") == "v3"
            ):
                combined[key] = resource

    resources = sorted(
        combined.values(),
        key=lambda entry: (
            entry["resolution"],
            entry.get("api_version") == "v3",
        ),
    )

    selected = next(
        (
            version for version in (v3, v2)
            if version.get("resources")
        ),
        v3,
    )

    return {
        "title": str(selected.get("title") or title),
        "subject_id": str(selected.get("subject_id") or ""),
        "resources": resources,
        "versions": {
            "v3": {
                "title": v3.get("title"),
                "subject_id": v3.get("subject_id"),
                "download_subject_id": v3.get("download_subject_id"),
                "dub": v3.get("dub"),
                "verified_qualities": sorted({
                    entry["resolution"]
                    for entry in v3.get("resources", [])
                }),
                "checks": v3.get("checks", []),
                "error": v3.get("error"),
            },
            "v2": {
                "title": v2.get("title"),
                "subject_id": v2.get("subject_id"),
                "verified_qualities": sorted({
                    entry["resolution"]
                    for entry in v2.get("resources", [])
                }),
                "checks": v2.get("checks", []),
                "error": v2.get("error"),
            },
        },
    }


@app.get("/api/v1/download/qualities")
async def get_download_qualities(
    title: str = Query(..., min_length=1, max_length=200),
    year: int | None = Query(default=None, ge=1888, le=2100),
    media_type: str = Query(default="movie", pattern="^(movie|tv)$"),
    season: int = Query(default=1, ge=1, le=100),
    episode: int = Query(default=1, ge=1, le=1000),
):
    setup_source_path()

    try:
        if media_type == "tv":
            episode_data = await get_verified_tv_episode(
                title, year, season, episode
            )

            resources = episode_data["resources"]

            qualities = sorted({
                resource["resolution"]
                for resource in resources
                if resource["resolution"] > 0
            })

            if not qualities:
                raise HTTPException(
                    404,
                    "MovieBox did not provide a verified full-length video "
                    f"for Season {season}, Episode {episode}. Placeholder "
                    "ads and files whose actual size disagrees with metadata "
                    "are rejected. Inspect /api/v1/diagnostics/tv-episode-versions "
                    "to compare V2 header profiles and V3 results.",
                )

            return {
                "result": "ok",
                "title": episode_data["title"],
                "subject_id": episode_data["subject_id"],
                "media_type": "tv",
                "season": season,
                "episode": episode,
                "qualities": qualities,
            }

        # Existing movie quality-selection logic is preserved.
        from moviebox_api.v3.http_client import MovieBoxHttpClient
        from moviebox_api.v3.core import ItemDetails

        async with MovieBoxHttpClient(timeout=30) as client:
            item = await find_moviebox_item(
                client, title, year, media_type="movie"
            )

            details = await ItemDetails(
                client_session=client
            ).get_content_model(item.subject_id)

            qualities = sorted({
                get_option_resolution(option)
                for option in get_movie_options(details)
                if get_option_resolution(option) > 0
            })

            if not qualities:
                raise HTTPException(
                    404,
                    "MovieBox returned no downloadable video qualities.",
                )

            return {
                "result": "ok",
                "title": str(
                    getattr(details, "title", None) or item.title
                ),
                "subject_id": str(item.subject_id),
                "media_type": "movie",
                "qualities": qualities,
            }

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            502,
            f"MovieBox quality lookup failed ({type(exc).__name__}): {str(exc)[:900]}",
        ) from exc


@app.get("/api/v1/download/resolve")
async def resolve_download(
    title: str = Query(..., min_length=1, max_length=200),
    year: int | None = Query(default=None, ge=1888, le=2100),
    resolution: int = Query(default=360, ge=144, le=2160),
    media_type: str = Query(default="movie", pattern="^(movie|tv)$"),
    season: int = Query(default=1, ge=1, le=100),
    episode: int = Query(default=1, ge=1, le=1000),
):
    setup_source_path()

    try:
        if media_type == "tv":
            episode_data = await get_verified_tv_episode(
                title, year, season, episode
            )

            resources = episode_data["resources"]

            if not resources:
                raise HTTPException(
                    404,
                    "MovieBox did not provide a verified full-length video "
                    f"for Season {season}, Episode {episode}. Placeholder "
                    "ads and files whose actual size disagrees with metadata "
                    "are rejected. Inspect /api/v1/diagnostics/tv-episode-versions "
                    "to compare V2 header profiles and V3 results.",
                )

            eligible = [
                resource for resource in resources
                if resource["resolution"] <= resolution
            ]

            chosen = (
                max(
                    eligible,
                    key=lambda resource: resource["resolution"],
                )
                if eligible
                else min(
                    resources,
                    key=lambda resource: resource["resolution"],
                )
            )

            return {
                "result": "ok",
                "title": chosen["title"],
                "subject_id": episode_data["subject_id"],
                "media_type": "tv",
                "season": season,
                "episode": episode,
                "resolution": chosen["resolution"],
                "api_version": chosen.get("api_version"),
                "size_bytes": chosen["size"],
                "download_url": chosen["resource_link"],
                "url_expires_note": (
                    "This URL passed a response-size and MP4-signature check. "
                    "Start downloading promptly; the source URL may expire."
                ),
            }

        # Existing movie-resolution logic is preserved.
        from moviebox_api.v3.http_client import MovieBoxHttpClient
        from moviebox_api.v3.core import ItemDetails

        async with MovieBoxHttpClient(timeout=30) as client:
            item = await find_moviebox_item(
                client, title, year, media_type="movie"
            )

            details = await ItemDetails(
                client_session=client
            ).get_content_model(item.subject_id)

            options = get_movie_options(details)

            if not options:
                raise HTTPException(
                    404,
                    "MovieBox returned no downloadable video resources.",
                )

            options.sort(key=get_option_resolution)

            eligible = [
                option for option in options
                if get_option_resolution(option) <= resolution
            ]

            chosen = eligible[-1] if eligible else options[0]
            url = str(chosen.resource_link)
            parsed = urlparse(url)

            if parsed.scheme != "https" or not parsed.hostname:
                raise HTTPException(
                    502,
                    "MovieBox returned an invalid download URL.",
                )

            return {
                "result": "ok",
                "title": str(
                    getattr(details, "title", None) or item.title
                ),
                "subject_id": str(item.subject_id),
                "media_type": "movie",
                "resolution": get_option_resolution(chosen),
                "size_bytes": getattr(chosen, "size", None),
                "download_url": url,
                "url_expires_note": (
                    "Start downloading promptly; the source URL may expire."
                ),
            }

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            502,
            f"MovieBox download resolution failed ({type(exc).__name__}): {str(exc)[:900]}",
        ) from exc


@app.get("/api/v1/diagnostics/tv-episode-versions")
async def diagnose_tv_episode_versions(
    title: str = Query(..., min_length=1, max_length=200),
    year: int | None = Query(default=None, ge=1888, le=2100),
    season: int = Query(default=1, ge=1, le=100),
    episode: int = Query(default=1, ge=1, le=1000),
):
    """Compare V2 and V3 series lookup with detailed media-response diagnostics."""
    setup_source_path()

    data = await get_verified_tv_episode(
        title, year, season, episode, diagnostic=True
    )

    versions = data["versions"]

    found = [
        version for version in ("v3", "v2")
        if versions[version].get("verified_qualities")
    ]

    v2_profiles_that_passed = sorted({
        check.get("header_profile")
        for check in versions["v2"].get("checks", [])
        if (
            check.get("result") == "verified"
            and check.get("header_profile")
        )
    })

    return {
        "result": (
            "verified_episode_found"
            if found
            else "no_verified_episode"
        ),
        "title": data["title"],
        "season": season,
        "episode": episode,
        "versions_with_verified_resources": found,
        "v2_header_profiles_that_passed_media_verification": (
            v2_profiles_that_passed
        ),
        "note": (
            "A header profile passing the small media probe is not by itself "
            "proof that the entire episode downloads. The normal resolver "
            "trusts the documented v1 MediaFileDownloader headers."
        ),
        "versions": versions,
        "next_step": (
            "The listed version returned a verified episode resource."
            if found
            else (
                "No candidate passed using the documented downloader profile. "
                "Compare the two V2 header profiles and their HTTP response details."
            )
        ),
    }


@app.get("/api/v1/diagnostics/moviebox-download")
async def diagnose_moviebox_download():
    setup_source_path()

    try:
        from moviebox_api.v2.core import Search
        from moviebox_api.v2.download import DownloadableMovieFilesDetail
        from moviebox_api.v2.requests import Session
        from moviebox_api.v1.models import DownloadableFilesMetadata
        from moviebox_api.v1.constants import DOWNLOAD_REQUEST_HEADERS

        session = Session(timeout=20)

        async with session._client:
            results = await Search(
                session=session,
                query="Sintel",
                per_page=10,
            ).get_content_model()

            candidates = [
                item for item in results.items
                if "sintel" in item.title.lower()
            ]

            if not candidates:
                return {"stage": "search", "result": "no_match"}

            item = candidates[0]

            raw = await DownloadableMovieFilesDetail(
                session=session,
                item=item,
            ).get_content()

            downloads = [
                entry for entry in (raw.get("downloads") or [])
                if isinstance(entry, dict)
                and isinstance(entry.get("url"), str)
                and entry["url"].strip()
            ]

            metadata = DownloadableFilesMetadata(
                **{**raw, "downloads": downloads}
            )

            checks = []

            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=20,
            ) as client:
                for media in metadata.downloads:
                    try:
                        async with client.stream(
                            "GET",
                            str(media.url),
                            headers={
                                **DOWNLOAD_REQUEST_HEADERS,
                                "Range": "bytes=0-63",
                            },
                        ) as response:
                            sample = await response.aread()

                            has_ftyp = (
                                len(sample) >= 8
                                and sample[4:8] == b"ftyp"
                            )

                            checks.append({
                                "resolution": media.resolution,
                                "http_status": response.status_code,
                                "mp4_ftyp_signature": has_ftyp,
                                "result": (
                                    "pass"
                                    if response.status_code in (200, 206)
                                    and has_ftyp
                                    else "fail"
                                ),
                            })

                    except Exception as exc:
                        checks.append({
                            "resolution": media.resolution,
                            "result": "error",
                            "error_type": type(exc).__name__,
                        })

            return {
                "stage": "download_url_check",
                "result": (
                    "pass"
                    if any(
                        check.get("result") == "pass"
                        for check in checks
                    )
                    else "fail"
                ),
                "title": item.title,
                "checks": checks,
            }

    except Exception as exc:
        return {
            "stage": "exception",
            "result": "error",
            "error_type": type(exc).__name__,
            "message": str(exc)[:300],
        }


@app.get("/api/v1/diagnostics/moviebox-v3-download")
async def diagnose_moviebox_v3_download():
    setup_source_path()

    try:
        from moviebox_api.v3.http_client import MovieBoxHttpClient
        from moviebox_api.v3.core import ItemDetails

        async with MovieBoxHttpClient(timeout=25) as client:
            item = await find_moviebox_item(
                client, "Sintel", media_type="movie"
            )

            details = await ItemDetails(
                client_session=client
            ).get_content_model(item.subject_id)

            checks = []

            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=25,
            ) as media_client:
                for option in get_movie_options(details):
                    url = str(option.resource_link)

                    try:
                        async with media_client.stream(
                            "GET",
                            url,
                            headers={"Range": "bytes=0-1023"},
                        ) as response:
                            sample = await response.aread()

                            content_type = response.headers.get(
                                "content-type", ""
                            ).split(";")[0].lower()

                            checks.append({
                                "resolution": get_option_resolution(option),
                                "http_status": response.status_code,
                                "content_type": content_type,
                                "mp4_ftyp_signature": (
                                    len(sample) >= 8
                                    and sample[4:8] == b"ftyp"
                                ),
                            })

                    except Exception as exc:
                        checks.append({
                            "result": "error",
                            "error_type": type(exc).__name__,
                        })

            return {
                "stage": "v3_detail_resource_inspection",
                "result": "ok",
                "title": details.title,
                "subject_id": str(item.subject_id),
                "checks": checks,
            }

    except Exception as exc:
        return {
            "stage": "v3_exception",
            "result": "error",
            "error_type": type(exc).__name__,
            "message": str(exc)[:300],
        }


@app.get("/api/v1/diagnostics/sintel-full-download")
async def diagnose_sintel_full_download():
    setup_source_path()
    started = time.monotonic()

    try:
        from moviebox_api.v3.http_client import MovieBoxHttpClient
        from moviebox_api.v3.core import ItemDetails

        async with MovieBoxHttpClient(timeout=30) as client:
            item = await find_moviebox_item(
                client, "Sintel", media_type="movie"
            )

            details = await ItemDetails(
                client_session=client
            ).get_content_model(item.subject_id)

            options = [
                option for option in get_movie_options(details)
                if get_option_resolution(option) == 360
            ]

            if not options:
                return {
                    "stage": "metadata",
                    "result": "no_360p_source",
                }

            option = options[0]
            digest = hashlib.sha256()
            total = 0
            prefix = b""
            content_type = ""

            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(
                    connect=15,
                    read=45,
                    write=15,
                    pool=15,
                ),
            ) as media:
                async with media.stream(
                    "GET",
                    str(option.resource_link),
                    headers={"Accept": "video/mp4,*/*;q=0.8"},
                ) as response:
                    content_type = response.headers.get(
                        "content-type", ""
                    ).split(";")[0].lower()

                    if response.status_code != 200:
                        return {
                            "stage": "transfer_start",
                            "result": "fail",
                            "http_status": response.status_code,
                            "content_type": content_type,
                        }

                    async for chunk in response.aiter_bytes(262144):
                        if not chunk:
                            continue

                        prefix += chunk[:max(0, 16 - len(prefix))]
                        total += len(chunk)
                        digest.update(chunk)

                        if time.monotonic() - started > 240:
                            return {
                                "stage": "full_transfer",
                                "result": "timeout",
                                "bytes_received": total,
                            }

            valid_mp4 = (
                len(prefix) >= 8 and prefix[4:8] == b"ftyp"
            )

            expected = (
                int(option.size)
                if option.size is not None
                else None
            )

            return {
                "stage": "full_transfer",
                "result": (
                    "pass"
                    if valid_mp4 and expected == total
                    else "fail"
                ),
                "title": details.title,
                "resolution": 360,
                "content_type": content_type,
                "bytes_received": total,
                "expected_bytes": expected,
                "mp4_ftyp_signature": valid_mp4,
                "sha256": digest.hexdigest(),
                "elapsed_seconds": round(
                    time.monotonic() - started, 2
                ),
            }

    except Exception as exc:
        return {
            "stage": "full_transfer",
            "result": "error",
            "error_type": type(exc).__name__,
            "message": str(exc)[:250],
            "elapsed_seconds": round(
                time.monotonic() - started, 2
            ),
        }
