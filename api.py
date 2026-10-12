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

APP_VERSION = "1.6.0"

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
    description="MovieBox API with movie downloads and TV episode diagnostics.",
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
        "tv_v1_metadata": "/api/v1/diagnostics/tv-episode-v1-metadata",
        "tv_upstream_cache_test": "/api/v1/diagnostics/tv-episode-upstream-cache",
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

        return (
            hostname == "macdn.aoneroom.com"
            and parsed.path.lower().startswith("/other/")
            and parsed.path.lower().endswith(".mp4")
        )

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
                "No verified TV-series search result was found.",
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


def inspect_metadata_url(url):
    parsed = urlparse(str(url or ""))
    parameters = parse_qs(parsed.query)

    timestamp_value = (parameters.get("t") or [None])[0]
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
        "url_fingerprint": fingerprint,
        "is_known_placeholder_pattern": is_placeholder_resource(url),
    }


def extract_download_records(raw):
    if not isinstance(raw, dict):
        return []

    downloads = raw.get("downloads") or []

    if not isinstance(downloads, list):
        return []

    records = []

    for entry in downloads:
        if not isinstance(entry, dict):
            records.append({
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

        info = {
            "resolution": resolution or None,
            "reported_size_bytes": size or None,
            "resource_id": str(entry.get("id") or "") or None,
            "url_present": bool(url),
        }

        if url:
            info.update(inspect_metadata_url(url))

        records.append(info)

    return records


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
            "MovieBox V2 returned no TV-series search results.",
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
            subject_type_value = int(
                getattr(subject_type, "value", subject_type)
            )
        except (TypeError, ValueError):
            continue

        if subject_type_value != SubjectType.TV_SERIES.value:
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
            "MovieBox V2 did not return a matching series with episode lookup identifiers.",
        )

    candidates.sort(key=lambda row: row[0], reverse=True)
    best = candidates[0][1]

    return SearchResultsItem.model_construct(
        subjectId=str(best.get("subjectId") or best.get("subject_id")),
        subjectType=SubjectType.TV_SERIES,
        title=str(best.get("title") or clean_title),
        detailPath=str(best.get("detailPath") or best.get("detail_path")),
    )


@app.get("/api/v1/diagnostics/tv-episode-v1-metadata")
async def diagnose_tv_episode_v1_metadata(
    title: str = Query(..., min_length=1, max_length=200),
    year: int | None = Query(default=None, ge=1888, le=2100),
    season: int = Query(default=1, ge=1, le=100),
    episode: int = Query(default=1, ge=1, le=1000),
):
    """
    Test the documented V1 TV-series metadata workflow.

    This performs a search and episode metadata request. It does not request
    any returned video URL from the CDN.
    """
    setup_source_path()
    started = datetime.now(timezone.utc).isoformat()

    session = None

    try:
        from moviebox_api.v1.constants import SubjectType
        from moviebox_api.v1.requests import Session
        from moviebox_api.v1.core import Search
        from moviebox_api.v1.download import DownloadableTVSeriesFilesDetail

        session = Session(timeout=30)

        search = Search(
            session=session,
            query=str(title).strip(),
            subject_type=SubjectType.TV_SERIES,
            page=1,
            per_page=20,
        )

        results = await search.get_content_model()
        items = list(getattr(results, "items", None) or [])

        target = normalize_title(title)

        candidates = []

        for item in items:
            item_title = normalize_title(getattr(item, "title", ""))

            if not item_title or not (
                item_title == target
                or target in item_title
                or item_title in target
            ):
                continue

            release_date = getattr(item, "releaseDate", None)
            release_year = getattr(release_date, "year", None)

            score = (
                int(item_title == target),
                int(bool(year) and str(release_year) == str(year)),
            )

            candidates.append((score, item))

        if not candidates:
            return {
                "result": "no_matching_v1_series",
                "stage": "v1_search",
                "requested_at_utc": started,
                "title": title,
                "season": season,
                "episode": episode,
                "media_server_request_made": False,
                "search_result_count": len(items),
                "message": (
                    "V1 did not return a sufficiently matching TV-series "
                    "result. No video URL was requested."
                ),
            }

        candidates.sort(key=lambda entry: entry[0], reverse=True)
        item = candidates[0][1]

        # This is the documented V1 TV-series resource class.
        # Its get_content() method uses V1's episode endpoint and the
        # matching detail-page Referer and cookie-aware session flow.
        downloader = DownloadableTVSeriesFilesDetail(
            session=session,
            item=item,
        )

        raw = await downloader.get_content(
            int(season), int(episode)
        )

        if not isinstance(raw, dict):
            return {
                "result": "unexpected_v1_metadata_response",
                "stage": "v1_episode_metadata",
                "requested_at_utc": started,
                "title": str(getattr(item, "title", None) or title),
                "subject_id": str(getattr(item, "subjectId", "") or ""),
                "season": season,
                "episode": episode,
                "response_type": type(raw).__name__,
                "media_server_request_made": False,
            }

        resources = extract_download_records(raw)

        return {
            "result": "metadata_found" if resources else "no_resource_records",
            "stage": "v1_episode_metadata",
            "requested_at_utc": started,
            "api_version": "v1",
            "title": str(getattr(item, "title", None) or title),
            "subject_id": str(getattr(item, "subjectId", "") or ""),
            "detail_path_present": bool(
                getattr(item, "detailPath", None)
            ),
            "season": season,
            "episode": episode,
            "metadata_keys": sorted(raw.keys()),
            "resource_count": len(resources),
            "resources": resources,
            "media_server_request_made": False,
            "signed_urls_included": False,
            "request_workflow": (
                "V1 search and cookie-aware episode metadata request"
            ),
            "note": (
                "This tests whether V1 supplies different episode metadata. "
                "It does not verify that any returned video URL serves the "
                "full episode."
            ),
        }

    except HTTPException:
        raise

    except Exception as exc:
        return {
            "result": "error",
            "stage": "v1_episode_metadata",
            "requested_at_utc": started,
            "media_server_request_made": False,
            "error_type": type(exc).__name__,
            "message": str(exc)[:500],
        }

    finally:
        if session is not None:
            try:
                await session._client.aclose()
            except Exception:
                pass


@app.get("/api/v1/diagnostics/tv-episode-metadata")
async def diagnose_tv_episode_metadata(
    title: str = Query(..., min_length=1, max_length=200),
    year: int | None = Query(default=None, ge=1888, le=2100),
    season: int = Query(default=1, ge=1, le=100),
    episode: int = Query(default=1, ge=1, le=1000),
):
    """Retrieve V2 episode metadata without requesting video URLs."""
    setup_source_path()
    started = datetime.now(timezone.utc).isoformat()
    session = None

    try:
        from moviebox_api.v2.requests import Session
        from moviebox_api.v2.download import DownloadableTVSeriesFilesDetail

        session = Session(timeout=30)

        item = await find_moviebox_v2_series_item(
            session, title, year
        )

        raw = await DownloadableTVSeriesFilesDetail(
            session=session,
            item=item,
        ).get_content(int(season), int(episode))

        if not isinstance(raw, dict):
            return {
                "result": "unexpected_metadata_response",
                "requested_at_utc": started,
                "media_server_request_made": False,
                "response_type": type(raw).__name__,
            }

        resources = extract_download_records(raw)

        return {
            "result": "metadata_found" if resources else "no_resource_records",
            "stage": "v2_episode_metadata",
            "requested_at_utc": started,
            "api_version": "v2",
            "title": str(getattr(item, "title", None) or title),
            "subject_id": str(getattr(item, "subjectId", "") or ""),
            "season": season,
            "episode": episode,
            "metadata_keys": sorted(raw.keys()),
            "resource_count": len(resources),
            "resources": resources,
            "media_server_request_made": False,
            "signed_urls_included": False,
        }

    except HTTPException:
        raise

    except Exception as exc:
        return {
            "result": "error",
            "stage": "v2_episode_metadata",
            "requested_at_utc": started,
            "media_server_request_made": False,
            "error_type": type(exc).__name__,
            "message": str(exc)[:500],
        }

    finally:
        if session is not None:
            try:
                await session._client.aclose()
            except Exception:
                pass


@app.get("/api/v1/diagnostics/tv-episode-upstream-cache")
async def diagnose_tv_episode_upstream_cache(
    title: str = Query(..., min_length=1, max_length=200),
    year: int | None = Query(default=None, ge=1888, le=2100),
    season: int = Query(default=1, ge=1, le=100),
    episode: int = Query(default=1, ge=1, le=1000),
):
    """Compare ordinary and no-cache V2 metadata responses without media requests."""
    setup_source_path()
    started = datetime.now(timezone.utc).isoformat()
    session = None

    try:
        from moviebox_api.v2.requests import Session
        from moviebox_api.v2.helpers import get_absolute_url

        session = Session(timeout=25)

        item = await find_moviebox_v2_series_item(
            session, title, year
        )

        endpoint = get_absolute_url(
            "/wefeed-h5api-bff/subject/download"
        )

        params = {
            "subjectId": str(item.subjectId),
            "se": int(season),
            "ep": int(episode),
            "detailPath": str(item.detailPath),
        }

        headers = dict(session._headers or {})

        async with httpx.AsyncClient(
            headers=headers,
            cookies=session._cookies,
            timeout=25,
            follow_redirects=True,
        ) as client:
            normal_response = await client.get(
                endpoint, params=params
            )
            normal_summary = {
                "http_status": normal_response.status_code,
                "date": normal_response.headers.get("date"),
                "age": normal_response.headers.get("age"),
                "cache_control": normal_response.headers.get("cache-control"),
                "etag": normal_response.headers.get("etag"),
                "server": normal_response.headers.get("server"),
                "via": normal_response.headers.get("via"),
                "x_cache": normal_response.headers.get("x-cache"),
                "body": summarize_metadata_for_comparison(
                    normal_response
                ),
            }

        no_cache_headers = {
            **headers,
            "Cache-Control": "no-cache, max-age=0",
            "Pragma": "no-cache",
        }

        async with httpx.AsyncClient(
            headers=no_cache_headers,
            cookies=session._cookies,
            timeout=25,
            follow_redirects=True,
        ) as client:
            bypass_response = await client.get(
                endpoint, params=params
            )
            bypass_summary = {
                "http_status": bypass_response.status_code,
                "date": bypass_response.headers.get("date"),
                "age": bypass_response.headers.get("age"),
                "cache_control": bypass_response.headers.get("cache-control"),
                "etag": bypass_response.headers.get("etag"),
                "server": bypass_response.headers.get("server"),
                "via": bypass_response.headers.get("via"),
                "x_cache": bypass_response.headers.get("x-cache"),
                "body": summarize_metadata_for_comparison(
                    bypass_response
                ),
            }

        normal_map = {
            str(resource.get("resolution")): resource
            for resource in normal_summary["body"].get("resources", [])
            if resource.get("resolution") is not None
        }

        bypass_map = {
            str(resource.get("resolution")): resource
            for resource in bypass_summary["body"].get("resources", [])
            if resource.get("resolution") is not None
        }

        common = sorted(
            set(normal_map) & set(bypass_map),
            key=lambda quality: int(quality),
        )

        comparison = {
            quality: {
                "same_fingerprint": (
                    normal_map[quality].get("url_fingerprint")
                    == bypass_map[quality].get("url_fingerprint")
                ),
                "normal_t": normal_map[quality].get("t_parameter"),
                "no_cache_t": bypass_map[quality].get("t_parameter"),
            }
            for quality in common
        }

        return {
            "result": "comparison_complete",
            "requested_at_utc": started,
            "title": str(getattr(item, "title", None) or title),
            "season": season,
            "episode": episode,
            "media_server_request_made": False,
            "ordinary_request": normal_summary,
            "no_cache_request": bypass_summary,
            "comparison": comparison,
            "interpretation": (
                "Different fingerprints mean the returned metadata differs. "
                "Missing cache headers do not prove there is no application "
                "cache; this test does not establish the meaning of the t "
                "parameter or whether a URL contains the real episode."
            ),
        }

    except Exception as exc:
        return {
            "result": "error",
            "stage": "metadata_upstream_cache_comparison",
            "requested_at_utc": started,
            "media_server_request_made": False,
            "error_type": type(exc).__name__,
            "message": str(exc)[:500],
        }

    finally:
        if session is not None:
            try:
                await session._client.aclose()
            except Exception:
                pass


def summarize_metadata_for_comparison(response):
    try:
        payload = response.json()
    except Exception:
        return {"json_response": False, "resources": []}

    if not isinstance(payload, dict):
        return {"json_response": True, "resources": []}

    raw = payload.get("data", payload)

    if not isinstance(raw, dict):
        raw = {}

    return {
        "json_response": True,
        "api_code": payload.get("code"),
        "api_message": payload.get("message"),
        "resources": extract_download_records(raw),
    }


async def verify_episode_video_url(url, expected_size=None):
    parsed = urlparse(str(url or ""))

    if parsed.scheme != "https" or not parsed.hostname:
        return False, "invalid_https_url"

    if parsed.username or parsed.password:
        return False, "embedded_credentials"

    if is_placeholder_resource(url):
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
        ) as client:
            async with client.stream(
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

                    if (
                        response.status_code == 200
                        and content_length
                        and content_length.isdigit()
                        and int(content_length) != expected_size
                    ):
                        return False, "size_mismatch"

                sample = await response.aread()

                if len(sample) < 8 or sample[4:8] != b"ftyp":
                    return False, "not_mp4_ftyp"

                return True, "verified"

    except Exception as exc:
        return False, f"{type(exc).__name__}:{str(exc)[:150]}"


async def get_verified_tv_episode_v2(title, year, season, episode):
    from moviebox_api.v2.requests import Session
    from moviebox_api.v2.download import DownloadableTVSeriesFilesDetail

    session = Session(timeout=30)

    try:
        item = await find_moviebox_v2_series_item(
            session, title, year
        )

        raw = await DownloadableTVSeriesFilesDetail(
            session=session, item=item
        ).get_content(int(season), int(episode))

        resources = []

        for entry in raw.get("downloads", []) if isinstance(raw, dict) else []:
            if not isinstance(entry, dict):
                continue

            url = str(entry.get("url") or "").strip()

            try:
                resolution = int(entry.get("resolution") or 0)
                size = int(entry.get("size") or 0)
            except (TypeError, ValueError):
                continue

            if not url or resolution <= 0 or size <= 0:
                continue

            valid, _ = await verify_episode_video_url(url, size)

            if valid:
                resources.append({
                    "resolution": resolution,
                    "size": size,
                    "resource_link": url,
                    "title": f"Episode #{season}.{episode}",
                    "season": season,
                    "episode": episode,
                    "api_version": "v2",
                })

        return {
            "title": str(getattr(item, "title", None) or title),
            "subject_id": str(getattr(item, "subjectId", "") or ""),
            "resources": resources,
        }

    finally:
        await session._client.aclose()


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
            results = await Search(
                client_session=client,
                query=str(title).strip(),
                subject_type=SubjectType.TV_SERIES,
                page=1,
                per_page=20,
            ).get_content_model()

            target = normalize_title(title)
            candidates = [
                item for item in (getattr(results, "items", None) or [])
                if normalize_title(getattr(item, "title", ""))
                and (
                    normalize_title(getattr(item, "title", "")) == target
                    or target in normalize_title(getattr(item, "title", ""))
                    or normalize_title(getattr(item, "title", "")) in target
                )
            ]

            if not candidates:
                raise RuntimeError("V3 search returned no matching series")

            item = candidates[0]

            result["title"] = str(getattr(item, "title", None) or title)
            result["subject_id"] = str(getattr(item, "subject_id", "") or "")

            details = await ItemDetails(
                client_session=client
            ).get_content_model(item.subject_id)

            dubs = list(getattr(details, "dubs", None) or [])

            if dubs:
                try:
                    dub = get_dub_or_raise(details)
                except Exception:
                    dub = next(
                        (
                            entry for entry in dubs
                            if str(getattr(entry, "lan_name", "")).lower()
                            == "original"
                        ),
                        dubs[0],
                    )

                subject_id = str(
                    getattr(dub, "subject_id", None) or item.subject_id
                )

                result["dub"] = str(
                    getattr(dub, "lan_name", None) or "selected"
                )
            else:
                subject_id = str(item.subject_id)

            result["download_subject_id"] = subject_id

            season_model = await SeasonDetails(
                client_session=client
            ).get_content_model(subject_id)

            seasons = list(
                getattr(season_model, "seasons", None) or []
            )

            if not seasons:
                raise RuntimeError("V3 season-details returned no seasons")

            plan = get_download_tv_series_request_params(
                seasons=seasons,
                season=int(season),
                episode=int(episode),
                limit=1,
            )

            for params in list(getattr(plan, "request_params", None) or []):
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
                            subject_id,
                            release_date=str(
                                getattr(item, "release_date", "") or ""
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

                        valid, reason = await verify_episode_video_url(
                            url, size
                        )

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
                                "season": season,
                                "episode": episode,
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
        v2 = await get_verified_tv_episode_v2(
            title, year, season, episode
        )
        v2_data = {
            "title": v2["title"],
            "subject_id": v2["subject_id"],
            "resources": v2["resources"],
            "error": None,
        }
    except Exception as exc:
        v2_data = {
            "title": str(title),
            "subject_id": None,
            "resources": [],
            "error": f"{type(exc).__name__}: {str(exc)[:250]}",
        }

    combined = {}

    for version, data in (("v3", v3), ("v2", v2_data)):
        for resource in data.get("resources", []):
            resource = {**resource, "api_version": version}
            combined[
                (resource["resolution"], resource["resource_link"])
            ] = resource

    resources = sorted(
        combined.values(), key=lambda entry: entry["resolution"]
    )

    selected = next(
        (data for data in (v3, v2_data) if data.get("resources")),
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
                    entry["resolution"]
                    for entry in v3.get("resources", [])
                }),
                "checks": v3.get("checks", []),
                "error": v3.get("error"),
            },
            "v2": {
                "subject_id": v2_data.get("subject_id"),
                "verified_qualities": sorted({
                    entry["resolution"]
                    for entry in v2_data.get("resources", [])
                }),
                "error": v2_data.get("error"),
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
                entry["resolution"]
                for entry in episode_data["resources"]
            })

            if not qualities:
                raise HTTPException(
                    404,
                    "No verified episode resources were found. "
                    "Test the V1 flow at /api/v1/diagnostics/tv-episode-v1-metadata.",
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

        # The existing movie quality-selection path is unchanged.
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
                    "No verified episode resources were found. "
                    "Test the V1 flow at /api/v1/diagnostics/tv-episode-v1-metadata.",
                )

            eligible = [
                entry for entry in resources
                if entry["resolution"] <= resolution
            ]

            chosen = (
                max(eligible, key=lambda entry: entry["resolution"])
                if eligible
                else min(resources, key=lambda entry: entry["resolution"])
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

        # The existing movie resolution-selection path is unchanged.
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


@app.get("/api/v1/diagnostics/tv-episode-versions")
async def diagnose_tv_episode_versions(
    title: str = Query(..., min_length=1, max_length=200),
    year: int | None = Query(default=None, ge=1888, le=2100),
    season: int = Query(default=1, ge=1, le=100),
    episode: int = Query(default=1, ge=1, le=1000),
):
    setup_source_path()

    data = await get_verified_tv_episode(
        title, year, season, episode
    )

    versions = data["versions"]

    found = [
        version for version in ("v3", "v2")
        if versions[version].get("verified_qualities")
    ]

    return {
        "result": "verified_episode_found" if found else "no_verified_episode",
        "title": data["title"],
        "season": season,
        "episode": episode,
        "versions_with_verified_resources": found,
        "versions": versions,
    }
