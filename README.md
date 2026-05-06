# YouTube Downloader Backend

Flask API for a Chrome extension that downloads YouTube videos in the best available resolution.

## What it does

- Reads the current YouTube video URL from the extension
- Lists the available resolutions and formats
- Downloads the requested stream
- Falls back to adaptive video + audio streams and merges them with `ffmpeg`
- Supports non-MP4 streams such as WebM, with MKV fallback when needed
- Serves the finished media file back to the extension for saving
- Exposes job status so the extension can show progress while the download runs

## Requirements

- Python 3.10+
- `ffmpeg` available on your `PATH`
- `nodejs-wheel-binaries` installed through `requirements.txt`
- Optional: a proxy URL in `YT_DOWNLOADER_PROXY`, `HTTPS_PROXY`, or `HTTP_PROXY` if YouTube flags the backend IP

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
python main.py
```

The API listens on `http://127.0.0.1:5000` by default.

## Endpoints

- `GET /health`
- `POST /api/video-info`
- `POST /api/available-resolutions`
- `POST /api/download`
- `GET /api/jobs/<job_id>`

Legacy aliases are also kept for compatibility:

- `POST /video_info`
- `POST /available_resolutions`
- `POST /download/<resolution>`

## Request body

```json
{
  "url": "https://www.youtube.com/watch?v=VIDEO_ID",
  "resolution": "720p"
}
```

If `resolution` is omitted or set to `best`, the backend picks the highest available stream.

For adaptive streams, the backend prefers MP4 when both tracks are MP4-compatible. Otherwise it falls back to MKV so that higher-resolution WebM streams can still be merged and downloaded cleanly.

`POST /api/download` starts a background job and returns a `job_id`. The extension polls `GET /api/jobs/<job_id>` until the job becomes `complete`, then it downloads the final file from the returned `download_url`.

## Deploy to Render

This backend is ready for a Render free web service.

1. Push the `backend/` repo to GitHub.
2. In Render, create a new `Web Service`.
3. Choose the Docker-based service type.
4. Use the provided `render.yaml` or point Render at this folder.
5. Keep the instance on the `Free` plan.

Render will build the `Dockerfile`, install `ffmpeg`, and start the app with Gunicorn on the Render port.

After deploy, the extension will use the Render service URL by default.

## Notes

- The backend accepts common YouTube URL forms such as `youtube.com/watch`, `youtu.be`, `shorts`, and `embed`.
- Downloads are written to `backend/downloads/` and served back as attachments.
- CORS is enabled for local extension usage.
- If YouTube flags requests as bot traffic on your hosting IP, configure a proxy for the backend and redeploy.
