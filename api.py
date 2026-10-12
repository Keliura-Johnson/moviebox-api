
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
    description=(
        "Browser-accessible API wrapper with health checks and a restricted "
        "media-response inspector."
    ),
    version=APP_VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/", tags=["service"])
async def root():
    return {
        "name": "MovieBox API Web Service",
        "version": APP_VERSION,
        "status": "ok",
        "health": "/health",
        "docs": "/docs",
        "media_inspector": "/api/v1/media/inspect",
        "demo_player": "/watch/demo",
        "note": (
            "The media inspector only checks HTTPS hosts listed in "
            "MEDIA_ALLOWED_HOSTS. /watch/demo is a public sample player."
        ),
    }


@app.get("/health", tags=["service"])
async def health():
    return {"status": "ok", "version": APP_VERSION}


@app.get("/api/v1/media/inspect", tags=["media"])
async def inspect_media(
    url: str = Query(
        ...,
        description=(
            "HTTPS URL on a host configured in MEDIA_ALLOWED_HOSTS. "
            "Only the first 64 bytes are requested."
        ),
        max_length=2048,
    )
):
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()

    if parsed.scheme != "https" or not hostname:
        raise HTTPException(
            status_code=400,
            detail="A valid HTTPS media URL is required.",
        )

    if hostname not in ALLOWED_MEDIA_HOSTS:
        raise HTTPException(
            status_code=403,
            detail=(
                "Host is not allowed. Add an authorized host to the "
                "MEDIA_ALLOWED_HOSTS environment variable."
            ),
        )

    if parsed.username or parsed.password:
        raise HTTPException(
            status_code=400,
            detail="URLs containing embedded credentials are not allowed.",
        )

    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=15.0,
        ) as client:
            async with client.stream(
                "GET",
                url,
                headers={
                    "Range": "bytes=0-63",
                    "User-Agent": "MovieBox-API-media-check/1.0",
                },
            ) as response:
                sample = await response.aread()
                content_type = response.headers.get(
                    "content-type", "unknown"
                ).split(";")[0]
                content_range = response.headers.get(
                    "content-range", "not provided"
                )
                has_ftyp = len(sample) >= 8 and sample[4:8] == b"ftyp"
                looks_like_mp4 = has_ftyp or (
                    content_type.lower() == "video/mp4"
                    and sample[:1] != b"<"
                )

                return {
                    "http_status": response.status_code,
                    "host": hostname,
                    "content_type": content_type,
                    "content_range": content_range,
                    "sample_bytes": len(sample),
                    "mp4_ftyp_signature": has_ftyp,
                    "looks_like_mp4": looks_like_mp4,
                    "result": (
                        "pass"
                        if response.status_code in (200, 206)
                        and looks_like_mp4
                        else "fail"
                    ),
                    "note": (
                        "This checks a small response sample only; it does "
                        "not guarantee full-file integrity or playback."
                    ),
                }
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Media host request failed: {type(exc).__name__}.",
        ) from exc


@app.get("/watch/demo", response_class=HTMLResponse, tags=["playback"])
async def demo_player():
    """Browser player for a public sample video."""
    video_url = (
        "https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/720/"
        "Big_Buck_Bunny_720_10s_1MB.mp4"
    )
    return HTMLResponse(
        content=f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MovieBox API — Playback Test</title>
  <style>
    body {{ margin: 0; padding: 24px; background: #0b0712; color: #fff;
           font: 16px system-ui, sans-serif; }}
    main {{ max-width: 960px; margin: 0 auto; }}
    video {{ display: block; width: 100%; max-height: 75vh; background: #000;
             border-radius: 12px; }}
    p {{ color: #c8bfd8; line-height: 1.5; }}
  </style>
</head>
<body>
  <main>
    <h1>Playback test</h1>
    <p>Public sample clip used to verify browser playback.</p>
    <video controls playsinline preload="metadata">
      <source src="{video_url}" type="video/mp4">
      Your browser does not support HTML video playback.
    </video>
  </main>
</body>
</html>""",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/v1/diagnostics/moviebox-download", tags=["diagnostics"])
async def diagnose_moviebox_download():
    """Test the existing v2 download metadata for Sintel."""
    source_dir = str(Path(__file__).resolve().parent / "src")
    if source_dir not in sys.path:
        sys.path.insert(0, source_dir)

    try:
        from moviebox_api.v2.core import Search
        from moviebox_api.v2.download import DownloadableMovieFilesDetail
        from moviebox_api.v2.requests import Session
        from moviebox_api.v1.models import DownloadableFilesMetadata
        from moviebox_api.v1.constants import DOWNLOAD_REQUEST_HEADERS

        session = Session(timeout=20.0)
        async with session._client:
            results = await Search(
                session=session, query="Sintel", per_page=10
            ).get_content_model()

            candidates = [
                item for item in results.items
                if "sintel" in item.title.lower()
            ]
            if not candidates:
                return {
                    "stage": "search",
                    "result": "no_match",
                    "query": "Sintel",
                }

            item = candidates[0]
            downloader = DownloadableMovieFilesDetail(
                session=session, item=item
            )
            raw = await downloader.get_content()
            downloads = [
                entry for entry in (raw.get("downloads") or [])
                if isinstance(entry, dict)
                and isinstance(entry.get("url"), str)
                and entry["url"].strip()
            ]
            metadata = DownloadableFilesMetadata(
                **{**raw, "downloads": downloads}
            )

            if not metadata.downloads:
                return {
                    "stage": "download_metadata",
                    "result": "no_downloads",
                    "title": item.title,
                    "hasResource": metadata.hasResource,
                }

            checks = []
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=20.0
            ) as client:
                for media in metadata.downloads:
                    parsed = urlparse(str(media.url))
                    if parsed.scheme != "https" or not parsed.hostname:
                        checks.append({
                            "resolution": media.resolution,
                            "result": "invalid_url",
                        })
                        continue

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
                            content_type = response.headers.get(
                                "content-type", "unknown"
                            ).split(";")[0].lower()
                            has_ftyp = (
                                len(sample) >= 8 and sample[4:8] == b"ftyp"
                            )
                            passed = (
                                response.status_code in (200, 206)
                                and content_type == "video/mp4"
                                and has_ftyp
                            )
                            checks.append({
                                "resolution": media.resolution,
                                "declared_size_bytes": media.size,
                                "http_status": response.status_code,
                                "content_type": content_type,
                                "sample_bytes": len(sample),
                                "mp4_ftyp_signature": has_ftyp,
                                "result": "pass" if passed else "fail",
                            })
                    except Exception as exc:
                        checks.append({
                            "resolution": media.resolution,
                            "result": "error",
                            "error_type": type(exc).__name__,
                        })

            passed = any(c.get("result") == "pass" for c in checks)
            return {
                "stage": "download_url_check",
                "result": "pass" if passed else "fail",
                "title": item.title,
                "download_options_checked": len(checks),
                "checks": checks,
            }
    except Exception as exc:
        return {
            "stage": "exception",
            "result": "error",
            "error_type": type(exc).__name__,
            "message": str(exc)[:300],
        }


@app.get("/api/v1/diagnostics/moviebox-v3-download", tags=["diagnostics"])
async def diagnose_moviebox_v3_download():
    """Inspect Sintel v3 resource URLs and validate small MP4 samples."""
    source_dir = str(Path(__file__).resolve().parent / "src")
    if source_dir not in sys.path:
        sys.path.insert(0, source_dir)

    try:
        from moviebox_api.v3.http_client import MovieBoxHttpClient
        from moviebox_api.v3.core import (
            Search,
            ItemDetails,
            DownloadableVideoFilesDetail,
        )
        from moviebox_api.v3.constants import ResolutionType

        async with MovieBoxHttpClient(timeout=25.0) as client:
            results = await Search(
                client_session=client, query="Sintel", per_page=10
            ).get_content_model()
            candidates = [
                item for item in results.items
                if "sintel" in item.title.lower() and item.has_resource
            ]
            if not candidates:
                return {"stage": "v3_search", "result": "no_match"}

            item = candidates[0]
            details = await ItemDetails(
                client_session=client
            ).get_content_model(item.subject_id)

            candidates_to_check = []
            detector_summary = []

            for index, detector in enumerate(details.resource_detectors or []):
                summary = {
                    "index": index,
                    "source": detector.source,
                    "type": detector.type,
                    "first_size_bytes": detector.first_size,
                    "download_url_host": (
                        urlparse(str(detector.download_url)).hostname
                        if detector.download_url else None
                    ),
                    "resolution_list": [],
                }

                if detector.download_url:
                    candidates_to_check.append({
                        "origin": "detail.downloadUrl",
                        "resolution": None,
                        "declared_size_bytes": detector.first_size or None,
                        "url": str(detector.download_url),
                    })

                for option in detector.resolution_list or []:
                    summary["resolution_list"].append({
                        "resolution": int(option.resolution),
                        "size_bytes": option.size,
                        "host": urlparse(str(option.resource_link)).hostname,
                    })
                    candidates_to_check.append({
                        "origin": "detail.resolutionList",
                        "resolution": int(option.resolution),
                        "declared_size_bytes": option.size or None,
                        "url": str(option.resource_link),
                    })

                detector_summary.append(summary)

            for resolution in (
                ResolutionType._360P,
                ResolutionType._720P,
                ResolutionType._1080P,
            ):
                metadata = await DownloadableVideoFilesDetail(
                    client_session=client,
                    per_page=10,
                    resolution=resolution,
                ).get_content_model(
                    item.subject_id,
                    release_date=str(item.release_date),
                )
                for media in metadata.list:
                    candidates_to_check.append({
                        "origin": "resource_endpoint",
                        "resolution": media.resolution,
                        "declared_size_bytes": media.size or None,
                        "url": str(media.resource_link),
                    })

            checks = []
            seen = set()
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=25.0
            ) as media_client:
                for candidate in candidates_to_check:
                    url = candidate["url"]
                    fingerprint = hashlib.sha256(
                        url.encode()
                    ).hexdigest()[:12]
                    if fingerprint in seen:
                        continue
                    seen.add(fingerprint)
                    parsed = urlparse(url)
                    check = {
                        "origin": candidate["origin"],
                        "resolution": candidate["resolution"],
                        "declared_size_bytes": candidate["declared_size_bytes"],
                        "url_fingerprint": fingerprint,
                        "url_host": parsed.hostname,
                    }

                    if parsed.scheme != "https" or not parsed.hostname:
                        check["result"] = "invalid_url"
                        checks.append(check)
                        continue

                    try:
                        async with media_client.stream(
                            "GET",
                            url,
                            headers={
                                "User-Agent": "MovieBox-Licensed-Test/1.0",
                                "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
                                "Range": "bytes=0-1023",
                            },
                        ) as response:
                            sample = await response.aread()
                            ctype = response.headers.get(
                                "content-type", "unknown"
                            ).split(";")[0].lower()
                            content_range = response.headers.get(
                                "content-range", ""
                            )
                            total_size = None
                            if "/" in content_range:
                                try:
                                    total_size = int(
                                        content_range.rsplit("/", 1)[1]
                                    )
                                except ValueError:
                                    pass

                            has_ftyp = (
                                len(sample) >= 8 and sample[4:8] == b"ftyp"
                            )
                            declared = candidate["declared_size_bytes"]
                            size_matches = bool(
                                declared and total_size is not None
                                and abs(total_size - declared) <= max(
                                    1024 * 1024, int(declared * 0.02)
                                )
                            )

                            check.update({
                                "final_cdn_host": urlparse(
                                    str(response.url)
                                ).hostname,
                                "http_status": response.status_code,
                                "content_type": ctype,
                                "content_range": content_range or None,
                                "sample_bytes": len(sample),
                                "mp4_ftyp_signature": has_ftyp,
                                "actual_total_bytes": total_size,
                                "size_matches_metadata": (
                                    size_matches if declared else None
                                ),
                                "result": (
                                    "pass"
                                    if response.status_code in (200, 206)
                                    and ctype == "video/mp4"
                                    and has_ftyp
                                    and (size_matches or not declared)
                                    else "fail"
                                ),
                            })
                    except Exception as exc:
                        check.update({
                            "result": "error",
                            "error_type": type(exc).__name__,
                            "message": str(exc)[:180],
                        })
                    checks.append(check)

            passed = any(c.get("result") == "pass" for c in checks)
            return {
                "stage": "v3_detail_resource_inspection",
                "result": "pass" if passed else "fail",
                "title": details.title,
                "subject_id": item.subject_id,
                "resource_detectors": detector_summary,
                "unique_urls_tested": len(checks),
                "checks": checks,
            }
    except Exception as exc:
        return {
            "stage": "v3_exception",
            "result": "error",
            "error_type": type(exc).__name__,
            "message": str(exc)[:300],
        }


@app.get("/api/v1/diagnostics/sintel-full-download", tags=["diagnostics"])
async def diagnose_sintel_full_download():
    """Stream the licensed Sintel 360p test asset and validate full transfer."""
    started = time.monotonic()
    source_dir = str(Path(__file__).resolve().parent / "src")
    if source_dir not in sys.path:
        sys.path.insert(0, source_dir)

    try:
        from moviebox_api.v3.http_client import MovieBoxHttpClient
        from moviebox_api.v3.core import Search, ItemDetails

        async with MovieBoxHttpClient(timeout=30.0) as api:
            results = await Search(
                client_session=api, query="Sintel", per_page=10
            ).get_content_model()

            items = [
                item for item in results.items
                if "sintel" in item.title.lower() and item.has_resource
            ]
            if not items:
                return {"stage": "search", "result": "no_match"}

            item = items[0]
            details = await ItemDetails(
                client_session=api
            ).get_content_model(item.subject_id)

            options = [
                option
                for detector in (details.resource_detectors or [])
                for option in (detector.resolution_list or [])
                if int(option.resolution) == 360 and option.resource_link
            ]
            if not options:
                return {
                    "stage": "metadata",
                    "result": "no_360p_source",
                    "title": details.title,
                }

            option = options[0]
            digest = hashlib.sha256()
            total = 0
            prefix = b""

            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(
                    connect=15, read=45, write=15, pool=15
                ),
            ) as media:
                async with media.stream(
                    "GET",
                    str(option.resource_link),
                    headers={
                        "Accept": "video/mp4,*/*;q=0.8",
                        "User-Agent": "MovieBox-Licensed-Test/1.0",
                    },
                ) as response:
                    content_type = response.headers.get(
                        "content-type", ""
                    ).split(";")[0].lower()

                    if (
                        response.status_code != 200
                        or content_type != "video/mp4"
                    ):
                        return {
                            "stage": "transfer_start",
                            "result": "fail",
                            "http_status": response.status_code,
                            "content_type": content_type,
                        }

                    async for chunk in response.aiter_bytes(262144):
                        if not chunk:
                            continue

                        if len(prefix) < 16:
                            prefix += chunk[:16 - len(prefix)]

                        total += len(chunk)
                        digest.update(chunk)

                        if time.monotonic() - started > 240:
                            return {
                                "stage": "full_transfer",
                                "result": "timeout",
                                "bytes_received": total,
                            }

            expected = (
                int(option.size) if option.size is not None else None
            )
            valid_mp4 = len(prefix) >= 8 and prefix[4:8] == b"ftyp"
            size_matches = expected is not None and total == expected
            passed = valid_mp4 and size_matches

            return {
                "stage": "full_transfer",
                "result": "pass" if passed else "fail",
                "title": details.title,
                "resolution": 360,
                "http_status": 200,
                "content_type": content_type,
                "bytes_received": total,
                "expected_bytes": expected,
                "size_matches_metadata": size_matches,
                "mp4_ftyp_signature": valid_mp4,
                "sha256": digest.hexdigest(),
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "license": "Creative Commons Attribution 3.0",
            }

    except Exception as exc:
        return {
            "stage": "full_transfer",
            "result": "error",
            "error_type": type(exc).__name__,
            "message": str(exc)[:250],
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
