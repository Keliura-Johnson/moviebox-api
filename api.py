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
    """Browser player for the openly available Big Buck Bunny sample clip."""
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
    <p>Big Buck Bunny — a freely available sample clip used to verify browser playback.</p>
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
