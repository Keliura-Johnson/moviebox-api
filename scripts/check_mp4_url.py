import argparse
import asyncio
from urllib.parse import urlparse

import httpx


async def inspect_media(url: str) -> int:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        print("RESULT: FAIL")
        print("Reason: provide a valid HTTPS media URL.")
        return 2

    headers = {"Range": "bytes=0-63", "User-Agent": "MovieBox-API-media-check/1.0"}
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=25.0
        ) as client:
            async with client.stream("GET", url, headers=headers) as response:
                sample = await response.aread()
                content_type = response.headers.get("content-type", "unknown").split(";")[0]
                content_range = response.headers.get("content-range", "not provided")
                final_host = urlparse(str(response.url)).hostname or "unknown"

                # ISO Base Media File Format files commonly contain 'ftyp'
                # at byte offset 4; do not print the URL or the media bytes.
                has_ftyp = len(sample) >= 8 and sample[4:8] == b"ftyp"
                looks_like_mp4 = has_ftyp or (
                    "video/mp4" in content_type.lower()
                    and sample[:1] != b"<"
                )

                print(f"HTTP status: {response.status_code}")
                print(f"Final host: {final_host}")
                print(f"Content-Type: {content_type}")
                print(f"Content-Range: {content_range}")
                print(f"Received sample bytes: {len(sample)}")
                print(f'MP4 "ftyp" signature: {"YES" if has_ftyp else "NO"}')
                print(f"Looks like MP4: {'YES' if looks_like_mp4 else 'NO'}")

                if response.status_code not in (200, 206):
                    print("RESULT: FAIL (server did not return media successfully)")
                    return 1
                if not looks_like_mp4:
                    print("RESULT: FAIL (response does not look like an MP4)")
                    return 1
                print("RESULT: PASS (response looks like an MP4; only a small sample was read)")
                return 0
    except httpx.HTTPError as exc:
        print(f"Request failed: {type(exc).__name__}: {exc}")
        print("RESULT: FAIL")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check an HTTPS media URL's response type and MP4 signature without downloading the full file."
    )
    parser.add_argument("url", help="HTTPS URL to media you own or are authorized to access")
    args = parser.parse_args()
    return asyncio.run(inspect_media(args.url))


if __name__ == "__main__":
    raise SystemExit(main())
