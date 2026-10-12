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
                return {"stage": "metadata", "result": "no_360p_source"}

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

            valid_mp4 = len(prefix) >= 8 and prefix[4:8] == b"ftyp"
            expected = (
                int(option.size) if option.size is not None else None
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
                "elapsed_seconds": round(time.monotonic() - started, 2),
            }

    except Exception as exc:
        return {
            "stage": "full_transfer",
            "result": "error",
            "error_type": type(exc).__name__,
            "message": str(exc)[:250],
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
