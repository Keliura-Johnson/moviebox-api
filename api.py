import os
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query
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
        "note": (
            "The media inspector only checks HTTPS hosts listed in "
            "MEDIA_ALLOWED_HOSTS. It does not resolve or provide movie streams."
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
