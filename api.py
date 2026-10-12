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

            media = metadata.best_media_file
            media_url = str(media.url)
            parsed_media = urlparse(media_url)
            if parsed_media.scheme != "https" or not parsed_media.hostname:
                return {
                    "stage": "url_validation",
                    "result": "fail",
                    "title": item.title,
                    "extension": media.ext,
                    "message": "Returned media URL was not a valid HTTPS URL.",
                }

            async with httpx.AsyncClient(
                follow_redirects=True, timeout=20.0
            ) as client:
                async with client.stream(
                    "GET",
                    media_url,
                    headers={
                        **__import__(
                            "moviebox_api.v1.constants",
                            fromlist=["DOWNLOAD_REQUEST_HEADERS"],
                        ).DOWNLOAD_REQUEST_HEADERS,
                        "Range": "bytes=0-63",
                    },
                ) as response:
                    sample = await response.aread()
                    content_type = response.headers.get(
                        "content-type", "unknown"
                    ).split(";")[0].lower()
                    has_ftyp = len(sample) >= 8 and sample[4:8] == b"ftyp"
                    is_mp4 = has_ftyp or (
                        content_type == "video/mp4" and sample[:1] != b"<"
                    )
                    return {
                        "stage": "download_url_check",
                        "result": (
                            "pass"
                            if response.status_code in (200, 206) and is_mp4
                            else "fail"
                        ),
                        "title": item.title,
                        "extension": media.ext,
                        "resolution": media.resolution,
                        "declared_size_bytes": media.size,
                        "http_status": response.status_code,
                        "final_host": urlparse(str(response.url)).hostname,
                        "content_type": content_type,
                        "sample_bytes": len(sample),
                        "mp4_ftyp_signature": has_ftyp,
                        "looks_like_mp4": is_mp4,
                        "message": (
                            "A real MP4-like response was received."
                            if response.status_code in (200, 206) and is_mp4
                            else "The returned URL did not verify as an MP4 response."
                        ),
                    }
    except Exception as exc:
        return {
            "stage": "exception",
            "result": "error",
            "error_type": type(exc).__name__,
            "message": str(exc)[:500],
        }

