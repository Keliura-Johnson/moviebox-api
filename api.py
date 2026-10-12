import os
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
        "media-response inspector. It does not extract third-party streams."
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
            "MEDIA_ALLOWED_HOSTS. /watch/demo is a licensed public sample player; "
            "it does not resolve third-party movie streams."
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

    headers = {
        "Range": "bytes=0-63",
        "User-Agent": "MovieBox-API-media-check/1.0",
    }

    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=15.0,
        ) as client:
            async with client.stream("GET", url, headers=headers) as response:
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
    """Browser player for the openly available Sintel sample clip."""
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
    <p>Sintel — a freely available sample clip used to verify browser playback.</p>
    <video controls playsinline preload="metadata">
      <source src="{video_url}" type="video/mp4">
      Your browser does not support HTML video playback.
    </video>
    <p>If the video plays here, your Render deployment can serve a player page. This is a sample clip, not a MovieBox catalogue stream.</p>
  </main>
</body>
</html>""",
        headers={"Cache-Control": "no-store"},
    )

@app.get("/api/v1/diagnostics/moviebox-download", tags=["diagnostics"])
async def diagnose_moviebox_download():
    """
    Tests the repository's real MovieBox search/download functions against the
    public-domain short film Sintel. URLs are deliberately not returned.
    """
    import sys
    from pathlib import Path

    source_dir = str(Path(__file__).resolve().parent / "src")
    if source_dir not in sys.path:
        sys.path.insert(0, source_dir)

    try:
        from moviebox_api.v2.core import Search
        from moviebox_api.v2.download import DownloadableMovieFilesDetail
        from moviebox_api.v2.requests import Session

        session = Session(timeout=20.0)
        async with session._client:
            search = Search(session=session, query="Sintel", per_page=10)
            results = await search.get_content_model()

            candidates = [
                item for item in results.items
                if "sintel" in item.title.lower()
            ]
            if not candidates:
                return {
                    "stage": "search",
                    "result": "no_match",
                    "query": "Sintel",
                    "returned_titles": [item.title for item in results.items[:10]],
                    "message": "MovieBox search worked, but returned no matching movie.",
                }

            item = candidates[0]
            from moviebox_api.v1.models import DownloadableFilesMetadata

            downloader = DownloadableMovieFilesDetail(session=session, item=item)
            raw_metadata = await downloader.get_content()
            raw_downloads = raw_metadata.get("downloads") or []
            valid_downloads = [
                entry for entry in raw_downloads
                if isinstance(entry, dict)
                and isinstance(entry.get("url"), str)
                and entry["url"].strip()
            ]
            metadata = DownloadableFilesMetadata(
                **{**raw_metadata, "downloads": valid_downloads}
            )

            if not metadata.downloads:
                return {
                    "stage": "download_metadata",
                    "result": "no_downloads",
                    "title": item.title,
                    "hasResource": metadata.hasResource,
                    "message": "Search worked, but MovieBox returned no downloadable files.",
                }

            request_headers = {
                **__import__(
                    "moviebox_api.v1.constants",
                    fromlist=["DOWNLOAD_REQUEST_HEADERS"],
                ).DOWNLOAD_REQUEST_HEADERS,
                "Range": "bytes=0-63",
            }
            checks = []
            # Test both the package's documented desktop headers and an Android
            # app-style media request. Some CDN mirrors treat these differently.
            header_profiles = [
                {
                    "profile": "package_firefox",
                    "headers": request_headers,
                },
                {
                    "profile": "android_media",
                    "headers": {
                        "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
                        "User-Agent": (
                            "com.community.oneroom/50020044 (Linux; U; Android 13; "
                            "en_US; 23078RKD5C; Build/TQ2A.230405.003; Cronet/135.0.7012.3)"
                        ),
                        "Referer": "https://www.movieboxpro.app/",
                        "Range": "bytes=0-63",
                    },
                },
            ]
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=20.0
            ) as client:
                for media in metadata.downloads:
                    media_url = str(media.url)
                    parsed_media = urlparse(media_url)
                    if parsed_media.scheme != "https" or not parsed_media.hostname:
                        checks.append({
                            "resolution": media.resolution,
                            "result": "invalid_url",
                        })
                        continue
                    for profile in header_profiles:
                        try:
                            async with client.stream(
                                "GET", media_url, headers=profile["headers"]
                            ) as response:
                                sample = await response.aread()
                                content_type = response.headers.get(
                                    "content-type", "unknown"
                                ).split(";")[0].lower()
                                has_ftyp = len(sample) >= 8 and sample[4:8] == b"ftyp"
                                is_mp4 = has_ftyp or (
                                    content_type == "video/mp4" and sample[:1] != b"<"
                                )
                                passed = response.status_code in (200, 206) and is_mp4
                                diagnostic_headers = {
                                    key: response.headers.get(key)
                                    for key in (
                                        "retry-after", "server", "via",
                                        "content-length", "content-type",
                                        "location", "cf-ray"
                                    )
                                    if response.headers.get(key) is not None
                                }
                                body_preview = ""
                                if not passed:
                                    body_preview = sample[:400].decode(
                                        "utf-8", errors="replace"
                                    )
                                    body_preview = " ".join(body_preview.split())[:400]
                                checks.append({
                                    "resolution": media.resolution,
                                    "request_profile": profile["profile"],
                                    "extension": media.ext,
                                    "declared_size_bytes": media.size,
                                    "http_status": response.status_code,
                                    "final_host": urlparse(str(response.url)).hostname,
                                    "content_type": content_type,
                                    "response_headers": diagnostic_headers,
                                    "error_body_preview": body_preview or None,
                                    "sample_bytes": len(sample),
                                    "mp4_ftyp_signature": has_ftyp,
                                    "looks_like_mp4": is_mp4,
                                    "result": "pass" if passed else "fail",
                                })
                        except Exception as check_error:
                            checks.append({
                                "resolution": media.resolution,
                                "request_profile": profile["profile"],
                                "result": "error",
                                "error_type": type(check_error).__name__,
                                "message": str(check_error)[:200],
                            })

            passed = any(check.get("result") == "pass" for check in checks)
            return {
                "stage": "download_url_check",
                "result": "pass" if passed else "fail",
                "title": item.title,
                "download_options_checked": len(checks),
                "checks": checks,
                "message": (
                    "At least one URL returned MP4-like data."
                    if passed
                    else "No returned download URL passed the MP4 response check."
                ),
            }
    except Exception as exc:
        return {
            "stage": "exception",
            "result": "error",
            "error_type": type(exc).__name__,
            "message": str(exc)[:500],
        }



@app.get("/api/v1/diagnostics/moviebox-v3-download", tags=["diagnostics"])
async def diagnose_moviebox_v3_download():
    """Inspect item-detail resource detectors and compare candidate MP4 sizes."""
    import sys
    import hashlib
    from pathlib import Path

    source_dir = str(Path(__file__).resolve().parent / "src")
    if source_dir not in sys.path:
        sys.path.insert(0, source_dir)

    try:
        from moviebox_api.v3.http_client import MovieBoxHttpClient
        from moviebox_api.v3.core import Search, ItemDetails, DownloadableVideoFilesDetail
        from moviebox_api.v3.constants import ResolutionType

        async with MovieBoxHttpClient(timeout=25.0) as client:
            search = Search(client_session=client, query="Sintel", per_page=10)
            results = await search.get_content_model()
            candidates = [
                item for item in results.items
                if "sintel" in item.title.lower() and item.has_resource
            ]
            if not candidates:
                return {"stage": "v3_search", "result": "no_match"}

            item = candidates[0]
            details = await ItemDetails(client_session=client).get_content_model(item.subject_id)
            candidates_to_check = []
            detector_summary = []
            for index, detector in enumerate(details.resource_detectors or []):
                summary = {
                    "index": index,
                    "source": detector.source,
                    "type": detector.type,
                    "first_size_bytes": detector.first_size,
                    "download_url_host": urlparse(str(detector.download_url)).hostname if detector.download_url else None,
                    "resolution_list": [],
                }
                if detector.download_url:
                    candidates_to_check.append({
                        "origin": "detail.downloadUrl",
                        "resolution": None,
                        "declared_size_bytes": detector.first_size or None,
                        "url": str(detector.download_url),
                        "detector_index": index,
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
                        "detector_index": index,
                    })
                detector_summary.append(summary)

            for resolution in (ResolutionType._360P, ResolutionType._720P, ResolutionType._1080P):
                metadata = await DownloadableVideoFilesDetail(
                    client_session=client, per_page=10, resolution=resolution
                ).get_content_model(item.subject_id, release_date=str(item.release_date))
                for media in metadata.list:
                    candidates_to_check.append({
                        "origin": "resource_endpoint",
                        "resolution": media.resolution,
                        "declared_size_bytes": media.size or None,
                        "url": str(media.resource_link),
                        "detector_index": None,
                    })

            checks = []
            seen = set()
            async with httpx.AsyncClient(follow_redirects=True, timeout=25.0) as media_client:
                for candidate in candidates_to_check:
                    url = candidate["url"]
                    fingerprint = hashlib.sha256(url.encode()).hexdigest()[:12]
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
                        "detector_index": candidate["detector_index"],
                    }
                    if parsed.scheme != "https" or not parsed.hostname:
                        check["result"] = "invalid_url"
                        checks.append(check)
                        continue
                    try:
                        async with media_client.stream(
                            "GET", url,
                            headers={
                                "User-Agent": "com.community.oneroom/50020044 (Linux; U; Android 13; en_US; 23078RKD5C; Build/TQ2A.230405.003; Cronet/135.0.7012.3)",
                                "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
                                "Range": "bytes=0-1023",
                            },
                        ) as response:
                            sample = await response.aread()
                            content_type = response.headers.get("content-type", "unknown").split(";")[0].lower()
                            content_range = response.headers.get("content-range", "")
                            total_size = None
                            if "/" in content_range:
                                try:
                                    total_size = int(content_range.rsplit("/", 1)[1])
                                except ValueError:
                                    pass
                            has_ftyp = len(sample) >= 8 and sample[4:8] == b"ftyp"
                            declared = candidate["declared_size_bytes"]
                            size_matches = bool(
                                declared and total_size is not None and
                                abs(total_size - declared) <= max(1024 * 1024, int(declared * 0.02))
                            )
                            check.update({
                                "final_cdn_host": urlparse(str(response.url)).hostname,
                                "http_status": response.status_code,
                                "content_type": content_type,
                                "content_range": content_range or None,
                                "sample_bytes": len(sample),
                                "mp4_ftyp_signature": has_ftyp,
                                "actual_total_bytes": total_size,
                                "size_matches_metadata": size_matches if declared else None,
                                "result": "pass" if response.status_code in (200, 206) and content_type == "video/mp4" and has_ftyp and (size_matches or not declared) else "fail",
                            })
                    except Exception as exc:
                        check.update({"result": "error", "error_type": type(exc).__name__, "message": str(exc)[:180]})
                    checks.append(check)

            passed = any(check.get("result") == "pass" for check in checks)
            return {
                "stage": "v3_detail_resource_inspection",
                "result": "pass" if passed else "fail",
                "title": details.title,
                "subject_id": item.subject_id,
                "resource_detectors": detector_summary,
                "unique_urls_tested": len(checks),
                "checks": checks,
                "message": "At least one detail or resource URL passed MP4 validation." if passed else "No tested detail/resource URL passed; use detector summaries to identify alternate hosts and metadata.",
            }
    except Exception as exc:
        return {"stage": "v3_exception", "result": "error", "error_type": type(exc).__name__, "message": str(exc)[:400]}
