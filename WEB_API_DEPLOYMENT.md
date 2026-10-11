# Browser API deployment

This repository is a Python package/CLI. The web layer in `api.py` adds HTTP
routes so Render can host a browser-accessible service.

## Current routes

- `GET /` — service information
- `GET /health` — health check for Render
- `GET /docs` — interactive FastAPI documentation
- `GET /api/v1/media/inspect?url=...` — checks a small MP4 response sample
  from an HTTPS host explicitly allowed by `MEDIA_ALLOWED_HOSTS`

The inspector is a diagnostic endpoint, not a stream resolver. It does not
extract or distribute third-party movie streams, and an MP4-looking response
does not guarantee that the full video will play.

## Deploy through Render

The repository includes `render.yaml`. In Render:

1. Open https://dashboard.render.com/.
2. Choose **New +** → **Web Service**.
3. Connect GitHub and select `Keliura-Johnson/moviebox-api`.
4. Choose branch `feature/browser-api` for this initial deployment.
5. If Render asks for service settings, use:
   - Runtime: Python
   - Build command: `pip install -e .`
   - Start command: `uvicorn api:app --host 0.0.0.0 --port $PORT`
6. Choose the Free plan if it is available to your account.
7. Click **Create Web Service** and wait for the deployment to finish.

If using Render's Blueprint flow instead, choose the repository and let Render
read `render.yaml`.

## Browser tests

Replace `YOUR-SERVICE.onrender.com` with the hostname Render assigns:

- `https://YOUR-SERVICE.onrender.com/`
- `https://YOUR-SERVICE.onrender.com/health`
- `https://YOUR-SERVICE.onrender.com/docs`

The default media-inspector allowlist is `test-videos.co.uk`. For a different
media host that you own or are authorized to test, set Render's
`MEDIA_ALLOWED_HOSTS` environment variable to the exact hostname, then redeploy.
For multiple hosts, separate the hostnames with commas.

Example encoded query for the public sample:

`https://YOUR-SERVICE.onrender.com/api/v1/media/inspect?url=https%3A%2F%2Ftest-videos.co.uk%2Fvids%2Fbigbuckbunny%2Fmp4%2Fh264%2F720%2FBig_Buck_Bunny_720_10s_1MB.mp4`

## Important limitation

This deployment makes the project callable over HTTP; it does not turn the
existing MovieBox CLI into a browser streaming API. Actual app streaming or
downloads require a separate, lawful media-source integration and playback
testing.
