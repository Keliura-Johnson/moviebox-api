import os
import sys
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

APP_VERSION = "1.4.0"
DEFAULT_ALLOWED_HOSTS = {"test-videos.co.uk"}

PREVIOUS_MEDIA_HOST = "bcdnw.hakunaymatata.com"
PREVIOUS_MEDIA_PATH = "/bt/45436569178be1e5aa9f59532b0014f0.mp4"
PREVIOUS_MEDIA_T = "1791832425"

ALLOWED_MEDIA_HOSTS = {
    host.strip().lower()
    for host in os.getenv(
        "MEDIA_ALLOWED_HOSTS", ",".join(DEFAULT_ALLOWED_HOSTS)
    ).split(",")
    if host.strip()
}

app = FastAPI(
    title="MovieBox API Web Service",
    description="MovieBox download API with movie and TV episode resolution.",
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
        "tv_episode_metadata": "/api/v1/diagnostics/tv-episode-metadata",
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
            return parsed.path.lower().startswith("/other/")

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
                "No verified TV-series result was found.",
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
            f"No MovieBox {media_type} result was found for this title.",
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
            "MovieBox v2 returned no TV-series search results.",
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
            "MovieBox v2 did not return a matching series with the identifiers required for episode lookup.",
        )

    candidates.sort(key=lambda row: row[0], reverse=True)
    best = candidates[0][1]

    return SearchResultsItem.model_construct(
        subjectId=str(best.get("subjectId") or best.get("subject_id")),
        subjectType=SubjectType.TV_SERIES,
        title=str(best.get("title") or clean_title),
        detailPath=str(best.get("detailPath") or best.get("detail_path")),
    )


def inspect_metadata_url(url):
    parsed = urlparse(str(url or ""))
    parameters = parse_qs(parsed.query)

    timestamp_value = (parameters.get("t") or [None])[0]
    timestamp_number = None
    timestamp_iso = None
    timestamp_delta = None

    if timestamp_value:
        try:
            timestamp_number = int(timestamp_value)
            timestamp_iso = datetime.fromtimestamp(
                timestamp_number, tz=timezone.utc
            ).isoformat()
            timestamp_delta = timestamp_number - int(time.time())
        except (ValueError, OverflowError, OSError):
            pass

    fingerprint = hashlib.sha256(
        str(url).encode("utf-8")
    ).hexdigest()[:16]

    return {
        "host": (parsed.hostname or "").lower() or None,
        "path": parsed.path or None,
        "file_name": parsed.path.rsplit("/", 1)[-1] or None,
        "query_parameter_names": sorted(parameters.keys()),
        "t_parameter": timestamp_value,
        "t_as_utc_if_unix_timestamp": timestamp_iso,
        "t_minus_current_unix_time_seconds": timestamp_delta,
        "matches_last_observed_host_and_path": (
            (parsed.hostname or "").lower() == PREVIOUS_MEDIA_HOST
            and parsed.path == PREVIOUS_MEDIA_PATH
        ),
        "matches_last_observed_t_parameter": (
            timestamp_value == PREVIOUS_MEDIA_T
        ),
        "url_fingerprint": fingerprint,
    }


@app.get("/api/v1/diagnostics/tv-episode-metadata")
async def diagnose_tv_episode_metadata(
    title: str = Query(..., min_length=1, max_length=200),
    year: int | None = Query(default=None, ge=1888, le=2100),
    season: int = Query(default=1, ge=1, le=100),
    episode: int = Query(default=1, ge=1, le=1000),
):
    """Retrieve episode download metadata without requesting any video URL."""
    setup_source_path()
    requested_at = datetime.now(timezone.utc).isoformat()

    try:
        from moviebox_api.v2.requests import Session
        from moviebox_api.v2.download import DownloadableTVSeriesFilesDetail

        session = Session(timeout=30)

        async with session._client:
            item = await find_moviebox_v2_series_item(
                session, title, year
            )

            downloader = DownloadableTVSeriesFilesDetail(
                session=session,
                item=item,
            )

            raw_metadata = await downloader.get_content(
                int(season), int(episode)
            )

        if not isinstance(raw_metadata, dict):
            return {
                "result": "unexpected_metadata_response",
                "stage": "moviebox_episode_metadata",
                "requested_at_utc": requested_at,
                "media_server_request_made": False,
                "response_type": type(raw_metadata).__name__,
            }

        raw_downloads = raw_metadata.get("downloads") or []

        if not isinstance(raw_downloads, list):
            return {
                "result": "invalid_downloads_field",
                "stage": "moviebox_episode_metadata",
                "requested_at_utc": requested_at,
                "media_server_request_made": False,
                "metadata_keys": sorted(raw_metadata.keys()),
                "downloads_field_type": type(raw_downloads).__name__,
            }

        resources = []

        for entry in raw_downloads:
            if not isinstance(entry, dict):
                resources.append({
                    "result": "ignored",
                    "reason": "resource_entry_is_not_an_object",
                })
                continue

            url = str(
                entry.get("url")
                or entry.get("resource_link")
                or entry.get("resourceLink")
                or ""
            ).strip()

            try:
                resolution = int(entry.get("resolution") or 0)
            except (TypeError, ValueError):
                resolution = 0

            try:
                size = int(entry.get("size") or 0)
            except (TypeError, ValueError):
                size = 0

            resource_info = {
                "resolution": resolution or None,
                "reported_size_bytes": size or None,
                "resource_id": str(entry.get("id") or "") or None,
                "url_present": bool(url),
            }

            if url:
                resource_info.update(inspect_metadata_url(url))

                resource_info["is_known_placeholder_pattern"] = (
                    is_placeholder_resource(url)
                )

            resources.append(resource_info)

        return {
            "result": (
                "metadata_found" if resources else "no_resource_records"
            ),
            "stage": "moviebox_episode_metadata",
            "requested_at_utc": requested_at,
            "title": str(getattr(item, "title", None) or title),
            "subject_id": str(getattr(item, "subjectId", "") or ""),
            "detail_path_present": bool(
                getattr(item, "detailPath", None)
            ),
            "season": season,
            "episode": episode,
            "metadata_keys": sorted(raw_metadata.keys()),
            "resource_count": len(resources),
            "resources": resources,
            "media_server_request_made": False,
            "signed_urls_included": False,
            "timestamp_note": (
                "The meaning of the URL's t parameter has not been confirmed "
                "from the library source. Its value and time difference are "
                "reported for comparison only, not as a guaranteed expiry time."
            ),
            "previous_observation": {
                "host": PREVIOUS_MEDIA_HOST,
                "path": PREVIOUS_MEDIA_PATH,
                "t_parameter": PREVIOUS_MEDIA_T,
            },
        }

    except HTTPException:
        raise
    except Exception as exc:
        return {
            "result": "error",
            "stage": "moviebox_episode_metadata",
            "requested_at_utc": requested_at,
            "media_server_request_made": False,
            "error_type": type(exc).__name__,
            "message": str(exc)[:500],
        }


async def verify_episode_video_url(url, expected_size=None):
    parsed = urlparse(str(url or ""))

    if parsed.scheme != "https" or not parsed.hostname:
        return False, "invalid_https_url"

    if parsed.username or parsed.password:
        return False, "embedded_credentials"

    if is_placeholder_resource(str(url)):
        return False, "known_placeholder_path"

    try:
        from moviebox_api.v1.constants import DOWNLOAD_REQUEST_HEADERS

        headers = dict(DOWNLOAD_REQUEST_HEADERS)
        headers["Range"] = "bytes=0-63"

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(
                connect=12, read=15, write=10, pool=10
            ),
        ) as media_client:
            async with media_client.stream(
                "GET", str(url), headers=headers
            ) as response:
                if response.status_code not in (200, 206):
                    return False, f"http_{response.status_code}"

                if is_placeholder_resource(str(response.url)):
                    return False, "redirected_to_placeholder"

                content_type = response.headers.get(
                    "content-type", ""
                ).split(";")[0].lower()

                if content_type in {
                    "text/html", "application/json", "text/plain"
                }:
                    return False, f"unexpected_content_type:{content_type}"

                content_range = response.headers.get("content-range", "")
                content_length = response.headers.get("content-length")

                actual_total = None
                if "/" in content_range:
                    total_text = content_range.rsplit("/", 1)[-1].strip()
                    if total_text.isdigit():
                        actual_total = int(total_text)

                if expected_size and expected_size > 0:
                    if actual_total is not None and actual_total != expected_size:
                        return False, "size_mismatch"

                    if response.status_code == 200 and content_length:
                        if content_length.isdigit() and int(content_length) != expected_size:
                            return False, "size_mismatch"

                sample = await response.aread()

                if len(sample) < 8 or sample[4:8] != b"ftyp":
                    return False, "not_mp4_ftyp"

                return True, "verified"

    except Exception as exc:
        return False, f"{type(exc).__name__}:{str(exc)[:150]}"


async def get_verified_tv_episode_v2(
    title, year, season, episode
):
    from moviebox_api.v2.requests import Session
    from moviebox_api.v2.download import DownloadableTVSeriesFilesDetail

    session = Session(timeout=30)

    async with session._client:
        item = await find_moviebox_v2_series_item(
            session, title, year
        )

        downloader = DownloadableTVSeriesFilesDetail(
            session=session,
            item=item,
        )

        raw = await downloader.get_content(
            int(season), int(episode)
        )

        downloads = (
            raw.get("downloads", [])
            if isinstance(raw, dict)
            else []
        )

        resources = []

        for entry in downloads if isinstance(downloads, list) else []:
            if not isinstance(entry, dict):
                continue

            url = str(
                entry.get("url")
                or entry.get("resource_link")
                or entry.get("resourceLink")
                or ""
            ).strip()

            try:
                resolution = int(entry.get("resolution") or 0)
                size = int(entry.get("size") or 0)
            except (TypeError, ValueError):
                continue

            if not url or resolution <= 0 or size <= 0:
                continue

            valid, _reason = await verify_episode_video_url(url, size)

            if valid:
                resources.append({
                    "resolution": resolution,
                    "size": size,
                    "resource_link": url,
                    "title": f"Episode #{season}.{episode}",
                    "season": int(season),
                    "episode": int(episode),
                    "api_version": "v2",
                })

        return {
            "title": str(getattr(item, "title", None) or title),
            "subject_id": str(getattr(item, "subjectId", "") or ""),
            "resources": resources,
        }


async def collect_v3_tv_episode(title, year, season, episode):
    result = {
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

            target = normalize_title(title)
            matching = []

            for item in search_items:
                item_title = normalize_title(
                    getattr(item, "title", "")
                )

                if item_title and (
                    item_title == target
                    or target in item_title
                    or item_title in target
                ):
                    matching.append(item)

            if not matching:
                raise RuntimeError("v3 search returned no matching series")

            series_item = matching[0]

            result["title"] = str(
                getattr(series_item, "title", None) or title
            )
            result["subject_id"] = str(
                getattr(series_item, "subject_id", "") or ""
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
                            item for item in dubs
                            if str(
                                getattr(item, "lan_name", "")
                            ).lower() == "original"
                        ),
                        dubs[0],
                    )

                download_subject_id = str(
                    getattr(dub, "subject_id", None)
                    or series_item.subject_id
                )
                result["dub"] = str(
                    getattr(dub, "lan_name", None) or "selected"
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
                raise RuntimeError("v3 season-details returned no seasons")

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
                            resolution=CustomResolutionType(f"{quality}P"),
                        )

                        file_details = await request.get_content_model(
                            download_subject_id,
                            release_date=str(
                                getattr(series_item, "release_date", "") or ""
                            ),
                        )
                    except Exception as exc:
                        result["checks"].append({
                            "resolution": quality,
                            "result": "request_error",
                            "reason": f"{type(exc).__name__}:{str(exc)[:150]}",
                        })
                        continue

                    entries = list(
                        getattr(file_details, "list", None) or []
                    )[offset:][:limit]

                    matched = [
                        entry for entry in entries
                        if int(getattr(entry, "season", 0) or 0) == int(season)
                        and int(getattr(entry, "episode", 0) or 0) == int(episode)
                    ]

                    if not matched:
                        result["checks"].append({
                            "resolution": quality,
                            "result": "no_episode_record",
                        })
                        continue

                    for video in matched:
                        actual_quality = get_option_resolution(video) or quality
                        size = int(getattr(video, "size", 0) or 0)
                        url = str(getattr(video, "resource_link", "") or "")
                        valid, reason = await verify_episode_video_url(url, size)

                        result["checks"].append({
                            "resolution": actual_quality,
                            "reported_size_bytes": size or None,
                            "result": "verified" if valid else "rejected",
                            "reason": reason,
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
                                "api_version": "v3",
                            })

    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:250]}"

    return result


async def get_verified_tv_episode(title, year, season, episode):
    v3 = await collect_v3_tv_episode(
        title, year, season, episode
    )

    try:
        v2_result = await get_verified_tv_episode_v2(
            title, year, season, episode
        )
        v2 = {
            "title": v2_result["title"],
            "subject_id": v2_result["subject_id"],
            "resources": v2_result["resources"],
            "error": None,
        }
    except Exception as exc:
        v2 = {
            "title": str(title),
            "subject_id": None,
            "resources": [],
            "error": f"{type(exc).__name__}: {str(exc)[:250]}",
        }

    combined = {}

    for version, data in (("v3", v3), ("v2", v2)):
        for resource in data.get("resources", []):
            resource["api_version"] = version
            combined[
                (resource["resolution"], resource["resource_link"])
            ] = resource

    resources = sorted(
        combined.values(),
        key=lambda item: item["resolution"],
    )

    selected = next(
        (data for data in (v3, v2) if data.get("resources")),
        v3,
    )

    return {
        "title": str(selected.get("title") or title),
        "subject_id": str(selected.get("subject_id") or ""),
        "resources": resources,
        "versions": {
            "v3": {
                "subject_id": v3.get("subject_id"),
                "download_subject_id": v3.get("download_subject_id"),
                "dub": v3.get("dub"),
                "verified_qualities": sorted({
                    item["resolution"]
                    for item in v3.get("resources", [])
                }),
                "checks": v3.get("checks", []),
                "error": v3.get("error"),
            },
            "v2": {
                "subject_id": v2.get("subject_id"),
                "verified_qualities": sorted({
                    item["resolution"]
                    for item in v2.get("resources", [])
                }),
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

            qualities = sorted({
                item["resolution"]
                for item in episode_data["resources"]
            })

            if not qualities:
                raise HTTPException(
                    404,
                    "No verified episode download resource was found. "
                    "Use /api/v1/diagnostics/tv-episode-metadata to inspect "
                    "MovieBox metadata without contacting the media server.",
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
            f"MovieBox quality lookup failed ({type(exc).__name__}): {str(exc)[:500]}",
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
                    "No verified episode resource was found. Inspect "
                    "/api/v1/diagnostics/tv-episode-metadata first.",
                )

            eligible = [
                item for item in resources
                if item["resolution"] <= resolution
            ]

            chosen = (
                max(eligible, key=lambda item: item["resolution"])
                if eligible
                else min(resources, key=lambda item: item["resolution"])
            )

            return {
                "result": "ok",
                "title": chosen["title"],
                "subject_id": episode_data["subject_id"],
                "media_type": "tv",
                "season": season,
                "episode": episode,
                "resolution": chosen["resolution"],
                "size_bytes": chosen["size"],
                "download_url": chosen["resource_link"],
                "api_version": chosen.get("api_version"),
                "url_expires_note": (
                    "Start downloading promptly; the source URL may expire."
                ),
            }

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
            f"MovieBox download resolution failed ({type(exc).__name__}): {str(exc)[:500]}",
        ) from exc
