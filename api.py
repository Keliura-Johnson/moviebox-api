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

APP_VERSION = "1.0.0"
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
    description="MovieBox v3 download API with movie and episode resolution.",
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
        filename = parsed.path.rsplit("/", 1)[-1].lower()

        return (
            (parsed.hostname or "").lower() == "macdn.aoneroom.com"
            and filename.startswith("b164fbfb43477929")
            and filename.endswith(".mp4")
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
                "No verified TV-series result was found. "
                "MovieBox did not return a result identifiable as a TV series.",
            )

        typed_items = [
            item for item in all_items
            if getattr(item, "has_resource", False)
        ]

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


async def get_episode_resources(
    client,
    subject_id,
    season,
    episode,
    requested_resolution=None,
):
    """Return direct resource links for one specific TV episode."""
    from moviebox_api.v3.core import ItemDetails

    if season < 1 or episode < 1:
        raise HTTPException(
            400, "Season and episode must be positive numbers."
        )

    # Use the same MovieBox detail-resource pipeline as movie downloads.
    details = await ItemDetails(
        client_session=client
    ).get_content_model(str(subject_id))

    found = []

    detectors = list(
        getattr(details, "resource_detectors", None) or []
    )

    for detector in detectors:
        entries = list(
            getattr(detector, "resolution_list", None) or []
        )

        for entry in entries:
            # Match the exact requested episode using MovieBox's
            # season ("se") and episode ("ep") fields.
            try:
                entry_season = int(
                    getattr(entry, "se", 0) or 0
                )
                entry_episode = int(
                    getattr(entry, "ep", 0) or 0
                )
            except (TypeError, ValueError):
                continue

            if (
                entry_season != int(season)
                or entry_episode != int(episode)
            ):
                continue

            resource_link = getattr(entry, "resource_link", None)

            if not resource_link:
                continue

            url = str(resource_link)
            parsed = urlparse(url)

            if parsed.scheme != "https" or not parsed.hostname:
                continue

            # Reject the known placeholder file.
            if (
                is_placeholder_resource(entry)
                or is_placeholder_resource(url)
            ):
                continue

            actual_resolution = get_option_resolution(entry)

            if actual_resolution <= 0:
                continue

            found.append({
                "resolution": actual_resolution,
                "size": getattr(entry, "size", None),
                "resource_link": url,
                "title": getattr(entry, "title", None),
                "season": entry_season,
                "episode": entry_episode,
            })

    # Remove duplicate quality/URL combinations.
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
        from moviebox_api.v3.http_client import MovieBoxHttpClient
        from moviebox_api.v3.core import ItemDetails

        async with MovieBoxHttpClient(timeout=30) as client:
            item = await find_moviebox_item(
                client, title, year, media_type=media_type
            )

            if media_type == "tv":
                resources = await get_episode_resources(
                    client, item.subject_id, season, episode
                )

                qualities = sorted({
                    resource["resolution"]
                    for resource in resources
                    if resource["resolution"] > 0
                })

                if not qualities:
                    raise HTTPException(
                        404,
                        f"No verified download was found for "
                        f"Season {season}, Episode {episode}.",
                    )

                return {
                    "result": "ok",
                    "title": str(item.title),
                    "subject_id": str(item.subject_id),
                    "media_type": "tv",
                    "season": season,
                    "episode": episode,
                    "qualities": qualities,
                }

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
            f"MovieBox quality lookup failed ({type(exc).__name__}).",
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
        from moviebox_api.v3.http_client import MovieBoxHttpClient
        from moviebox_api.v3.core import ItemDetails

        async with MovieBoxHttpClient(timeout=30) as client:
            item = await find_moviebox_item(
                client, title, year, media_type=media_type
            )

            if media_type == "tv":
                resources = await get_episode_resources(
                    client,
                    item.subject_id,
                    season,
                    episode,
                    requested_resolution=resolution,
                )

                if not resources:
                    raise HTTPException(
                        404,
                        f"No verified download was found for "
                        f"Season {season}, Episode {episode}.",
                    )

                resources.sort(
                    key=lambda resource: resource["resolution"]
                )

                eligible = [
                    resource for resource in resources
                    if resource["resolution"] <= resolution
                ]

                chosen = eligible[-1] if eligible else resources[0]
                url = chosen["resource_link"]

                return {
                    "result": "ok",
                    "title": str(
                        chosen.get("title") or item.title
                    ),
                    "subject_id": str(item.subject_id),
                    "media_type": "tv",
                    "season": season,
                    "episode": episode,
                    "resolution": chosen["resolution"],
                    "size_bytes": chosen.get("size"),
                    "download_url": url,
                    "url_expires_note": (
                        "Start downloading promptly; "
                        "the source URL may expire."
                    ),
                }

            # Existing movie resolution logic is unchanged.
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
                    "Start downloading promptly; "
                    "the source URL may expire."
                ),
            }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            502,
            f"MovieBox download resolution failed ({type(exc).__name__}).",
        ) from exc


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
                session=session, query="Sintel", per_page=10
            ).get_content_model()

            candidates = [
                item for item in results.items
                if "sintel" in item.title.lower()
            ]

            if not candidates:
                return {"stage": "search", "result": "no_match"}

            item = candidates[0]

            raw = await DownloadableMovieFilesDetail(
                session=session, item=item
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
                follow_redirects=True, timeout=20
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
                follow_redirects=True, timeout=25
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
                    connect=15, read=45, write=15, pool=15
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
