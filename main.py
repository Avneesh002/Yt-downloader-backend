from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlparse

from flask import Flask, jsonify, request, send_from_directory, url_for
from pytubefix import YouTube

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_ROOT = BASE_DIR / "downloads"
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

JOB_STORE: dict[str, dict] = {}
JOB_LOCK = threading.Lock()

JOB_PHASE_BOUNDS = {
    "queued": (0, 0),
    "resolving": (0, 5),
    "downloading_progressive": (5, 95),
    "downloading_video": (5, 65),
    "downloading_audio": (65, 85),
    "merging": (85, 98),
    "complete": (100, 100),
    "error": (0, 0),
}


def _add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.after_request
def after_request(response):
    return _add_cors_headers(response)


def _json_error(message, status_code=400):
    return jsonify({"error": message}), status_code


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _download_url(video_id: str, filename: str) -> str:
    return f"/api/downloads/{quote(video_id)}/{quote(filename)}"


def _create_job(url: str, requested_resolution: str | None) -> str:
    job_id = uuid.uuid4().hex
    payload = {
        "job_id": job_id,
        "url": url,
        "requested_resolution": _normalize_resolution(requested_resolution),
        "status": "queued",
        "phase": "queued",
        "message": "Queued",
        "progress": 0,
        "result": None,
        "error": None,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "last_progress_update": 0.0,
    }
    with JOB_LOCK:
        JOB_STORE[job_id] = payload
    return job_id


def _get_job(job_id: str) -> dict | None:
    with JOB_LOCK:
        job = JOB_STORE.get(job_id)
        return dict(job) if job else None


def _update_job(job_id: str, **updates) -> None:
    with JOB_LOCK:
        job = JOB_STORE.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = _utc_now()


def _set_job_phase(job_id: str, phase: str, message: str, progress: int | None = None) -> None:
    if progress is None:
        progress = JOB_PHASE_BOUNDS.get(phase, (0, 100))[0]
    _update_job(job_id, phase=phase, status="running", message=message, progress=progress)


def _start_job_progress(job_id: str, phase: str, downloaded: int, total: int, message: str) -> None:
    base, span = JOB_PHASE_BOUNDS.get(phase, (0, 100))
    percent = 0
    if total > 0:
        percent = int((downloaded / total) * 100)
    progress = min(base + int(span * percent / 100), 99)

    now = time.monotonic()
    job = _get_job(job_id) or {}
    last_progress = job.get("progress", 0)
    last_update = job.get("last_progress_update", 0.0)
    if progress == last_progress and now - last_update < 0.5:
        return

    _update_job(
        job_id,
        progress=progress,
        message=message,
        last_progress_update=now,
        downloaded_bytes=downloaded,
        total_bytes=total,
    )


def _progress_callback_factory(job_id: str):
    def callback(stream, chunk, bytes_remaining):
        total = getattr(stream, "filesize", None) or getattr(stream, "filesize_approx", None) or 0
        downloaded = max(int(total) - int(bytes_remaining), 0) if total else 0
        job = _get_job(job_id) or {}
        phase = job.get("phase", "downloading_progressive")
        message = job.get("message", "Downloading...")
        _start_job_progress(job_id, phase, downloaded, int(total), message)

    return callback


def _start_download_job(job_id: str, url: str, requested_resolution: str | None) -> None:
    try:
        _set_job_phase(job_id, "resolving", "Resolving available streams...", 1)
        yt = YouTube(url, on_progress_callback=_progress_callback_factory(job_id))
        payload = _prepare_download(job_id, yt, url, requested_resolution)
        _update_job(job_id, status="complete", phase="complete", message="Download complete", progress=100, result=payload)
    except Exception as exc:
        _update_job(job_id, status="error", phase="error", message=str(exc), error=str(exc))


def _sanitize_filename(value: str, fallback: str = "download") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "").strip("._-")
    return cleaned or fallback


def _extract_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    if host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]
        if parsed.path.startswith("/shorts/") or parsed.path.startswith("/embed/") or parsed.path.startswith("/v/"):
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2:
                return parts[1] if len(parts) == 2 else parts[2]

    if host == "youtu.be":
        parts = [part for part in parsed.path.split("/") if part]
        if parts:
            return parts[0]

    return None


def is_valid_youtube_url(url: str) -> bool:
    return bool(_extract_video_id(url))


def _normalize_resolution(resolution: str | None) -> str | None:
    if not resolution:
        return None

    value = resolution.strip().lower()
    if value == "best":
        return "best"

    match = re.fullmatch(r"(\d{3,4})p?", value)
    if match:
        return f"{match.group(1)}p"
    return value


def _resolution_height(resolution: str | None) -> int:
    if not resolution:
        return -1
    match = re.fullmatch(r"(\d{3,4})p", resolution.strip().lower())
    if not match:
        return -1
    return int(match.group(1))


def _parse_bitrate(value) -> int:
    if value is None:
        return 0
    match = re.search(r"(\d+)", str(value))
    return int(match.group(1)) if match else 0


def _stream_resolution_label(stream) -> str | None:
    return getattr(stream, "resolution", None) or getattr(stream, "res", None)


def _stream_subtype(stream) -> str | None:
    return getattr(stream, "subtype", None)


def _stream_mime_type(stream) -> str | None:
    return getattr(stream, "mime_type", None)


def _is_progressive(stream) -> bool:
    return bool(getattr(stream, "is_progressive", False))


def _has_audio(stream) -> bool:
    return bool(getattr(stream, "includes_audio_track", False) or _is_progressive(stream))


def _has_video(stream) -> bool:
    return bool(getattr(stream, "includes_video_track", False) or _is_progressive(stream))


def _stream_score(stream) -> tuple[int, int, int, int]:
    resolution = _stream_resolution_label(stream)
    return (
        _resolution_height(resolution),
        int(getattr(stream, "fps", 0) or 0),
        1 if _stream_subtype(stream) == "mp4" else 0,
        _parse_bitrate(getattr(stream, "abr", None)),
    )


def _best_stream_by_resolution(streams, requested_resolution: str | None):
    normalized = _normalize_resolution(requested_resolution)
    usable_streams = [stream for stream in streams if _stream_resolution_label(stream)]
    if not usable_streams:
        return None

    if normalized == "best" or normalized is None:
        return max(usable_streams, key=_stream_score)

    requested_height = _resolution_height(normalized)
    if requested_height < 0:
        return max(usable_streams, key=_stream_score)

    grouped = {}
    for stream in usable_streams:
        resolution = _stream_resolution_label(stream)
        height = _resolution_height(resolution)
        if height < 0:
            continue
        current = grouped.get(height)
        if current is None or _stream_score(stream) > _stream_score(current):
            grouped[height] = stream

    if not grouped:
        return max(usable_streams, key=_stream_score)

    if requested_height in grouped:
        return grouped[requested_height]

    lower_or_equal = [height for height in grouped if height <= requested_height]
    if lower_or_equal:
        return grouped[max(lower_or_equal)]

    return grouped[max(grouped)]


def _exact_stream_by_resolution(streams, requested_resolution: str | None):
    normalized = _normalize_resolution(requested_resolution)
    if normalized in {None, "best"}:
        return None

    exact = [stream for stream in streams if _stream_resolution_label(stream) == normalized]
    if not exact:
        return None
    return max(exact, key=_stream_score)


def _best_audio_stream(yt, preferred_subtype: str | None = None):
    audio_streams = list(yt.streams.filter(only_audio=True))
    if not audio_streams:
        audio_streams = [stream for stream in yt.streams if _has_audio(stream) and not _has_video(stream)]
    if not audio_streams:
        return None

    def score(stream):
        return (_parse_bitrate(getattr(stream, "abr", None)), _stream_score(stream))

    if preferred_subtype:
        preferred = [stream for stream in audio_streams if _stream_subtype(stream) == preferred_subtype]
        if preferred:
            audio_streams = preferred
    return max(audio_streams, key=score)


def _list_streams(yt):
    streams = list(yt.streams.filter(type="video"))
    unique_by_resolution = {}

    for stream in streams:
        resolution = _stream_resolution_label(stream)
        if not resolution:
            continue

        current = unique_by_resolution.get(resolution)
        if current is None or _stream_score(stream) > _stream_score(current):
            unique_by_resolution[resolution] = stream

    formatted = []
    for resolution in sorted(unique_by_resolution, key=_resolution_height):
        stream = unique_by_resolution[resolution]
        formatted.append(
            {
                "resolution": resolution,
                "itag": stream.itag,
                "fps": getattr(stream, "fps", None),
                "mime_type": getattr(stream, "mime_type", None),
                "subtype": _stream_subtype(stream),
                "progressive": _is_progressive(stream),
                "has_audio": _has_audio(stream),
                "has_video": _has_video(stream),
                "abr": getattr(stream, "abr", None),
                "filesize": getattr(stream, "filesize", None),
            }
        )

    return formatted


def _get_video_metadata(yt, video_id: str) -> dict:
    return {
        "video_id": video_id,
        "title": yt.title,
        "author": yt.author,
        "length": yt.length,
        "views": yt.views,
        "description": yt.description,
        "publish_date": yt.publish_date.isoformat() if yt.publish_date else None,
        "thumbnail": yt.thumbnail_url,
    }


def _ffmpeg_path() -> str:
    return shutil.which("ffmpeg") or ""


def _progressive_output_extension(stream) -> str:
    subtype = _stream_subtype(stream)
    return subtype if subtype else "mp4"


def _adaptive_output_extension(video_stream, audio_stream) -> str:
    video_subtype = _stream_subtype(video_stream)
    audio_subtype = _stream_subtype(audio_stream)
    if video_subtype == "mp4" and audio_subtype == "mp4":
        return "mp4"
    return "mkv"


def _run_ffmpeg_merge(video_path: Path, audio_path: Path, output_path: Path):
    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to merge video and audio streams.")

    command = [
        ffmpeg,
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(audio_path),
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        error_output = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"ffmpeg merge failed: {error_output or 'unknown error'}")


def _merge_streams(video_path: Path, audio_path: Path, output_dir: Path, filename_root: str, video_stream, audio_stream) -> Path:
    preferred_extension = _adaptive_output_extension(video_stream, audio_stream)
    final_path = output_dir / f"{filename_root}.{preferred_extension}"

    try:
        _run_ffmpeg_merge(video_path, audio_path, final_path)
        return final_path
    except RuntimeError:
        if preferred_extension != "mp4":
            raise

    fallback_path = output_dir / f"{filename_root}.mkv"
    _run_ffmpeg_merge(video_path, audio_path, fallback_path)
    return fallback_path


def _download_stream(stream, output_dir: Path, filename: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = stream.download(output_path=str(output_dir), filename=filename)
    return Path(downloaded)


def _prepare_download(job_id: str, yt, url: str, requested_resolution: str | None = None) -> dict:
    video_id = _extract_video_id(url)
    if not video_id:
        raise ValueError("Unable to extract a valid YouTube video ID.")

    title_slug = _sanitize_filename(yt.title, fallback=video_id)
    requested_resolution = _normalize_resolution(requested_resolution)

    target_dir = DOWNLOAD_ROOT / video_id
    target_dir.mkdir(parents=True, exist_ok=True)

    all_video_streams = list(yt.streams.filter(type="video"))
    progressive_streams = [stream for stream in all_video_streams if _is_progressive(stream)]
    video_only_streams = [stream for stream in all_video_streams if _has_video(stream) and not _has_audio(stream)]

    if requested_resolution in {None, "best"}:
        target_stream = _best_stream_by_resolution(all_video_streams, "best")
        if not target_stream:
            raise ValueError("No downloadable streams were found for this video.")

        resolution = _stream_resolution_label(target_stream) or "best"

        if _is_progressive(target_stream):
            filename = f"{title_slug}_{resolution}.{_progressive_output_extension(target_stream)}"
            _set_job_phase(job_id, "downloading_progressive", f"Downloading {resolution} stream...")
            final_path = _download_stream(target_stream, target_dir, filename)
            filename = final_path.name
            return {
                "video_id": video_id,
                "title": yt.title,
                "resolution": resolution,
                "filename": filename,
                "file_path": str(final_path),
                "download_url": _download_url(video_id, filename),
                "merged": False,
                "progressive": True,
            }

        audio_choice = _best_audio_stream(yt, _stream_subtype(target_stream))
        if not audio_choice:
            raise ValueError("No audio stream was found for this video.")

        with tempfile.TemporaryDirectory(prefix=f"{video_id}_", dir=str(target_dir)) as temp_dir:
            temp_path = Path(temp_dir)
            _set_job_phase(job_id, "downloading_video", f"Downloading {resolution} video track...")
            video_temp = _download_stream(
                target_stream,
                temp_path,
                f"{title_slug}_{resolution}_video.{_stream_subtype(target_stream) or 'mp4'}",
            )
            _set_job_phase(job_id, "downloading_audio", "Downloading audio track...")
            audio_temp = _download_stream(
                audio_choice,
                temp_path,
                f"{title_slug}_audio.{_stream_subtype(audio_choice) or 'm4a'}",
            )
            _set_job_phase(job_id, "merging", "Merging audio and video...")
            final_path = _merge_streams(video_temp, audio_temp, target_dir, f"{title_slug}_{resolution}", target_stream, audio_choice)
            filename = final_path.name

        return {
                "video_id": video_id,
                "title": yt.title,
                "resolution": resolution,
                "filename": filename,
                "file_path": str(final_path),
                "download_url": _download_url(video_id, filename),
                "merged": True,
                "progressive": False,
            }

    progressive_choice = _exact_stream_by_resolution(progressive_streams, requested_resolution)
    if progressive_choice:
        resolution = _stream_resolution_label(progressive_choice) or requested_resolution
        filename = f"{title_slug}_{resolution}.{_progressive_output_extension(progressive_choice)}"
        _set_job_phase(job_id, "downloading_progressive", f"Downloading {resolution} stream...")
        final_path = _download_stream(progressive_choice, target_dir, filename)
        filename = final_path.name
        return {
            "video_id": video_id,
            "title": yt.title,
            "resolution": resolution,
            "filename": filename,
            "file_path": str(final_path),
            "download_url": _download_url(video_id, filename),
            "merged": False,
            "progressive": True,
        }

    video_choice = _exact_stream_by_resolution(video_only_streams, requested_resolution)
    if not video_choice:
        video_choice = _best_stream_by_resolution(video_only_streams, requested_resolution)
    if not video_choice:
        video_choice = _best_stream_by_resolution(all_video_streams, requested_resolution)
    if not video_choice:
        raise ValueError("No downloadable video stream was found for the requested resolution.")

    audio_choice = _best_audio_stream(yt, _stream_subtype(video_choice))
    if not audio_choice:
        raise ValueError("No audio stream was found for this video.")

    resolution = _stream_resolution_label(video_choice) or requested_resolution

    with tempfile.TemporaryDirectory(prefix=f"{video_id}_", dir=str(target_dir)) as temp_dir:
        temp_path = Path(temp_dir)
        _set_job_phase(job_id, "downloading_video", f"Downloading {resolution} video track...")
        video_temp = _download_stream(
            video_choice,
            temp_path,
            f"{title_slug}_{resolution}_video.{_stream_subtype(video_choice) or 'mp4'}",
        )
        _set_job_phase(job_id, "downloading_audio", "Downloading audio track...")
        audio_temp = _download_stream(
            audio_choice,
            temp_path,
            f"{title_slug}_audio.{_stream_subtype(audio_choice) or 'm4a'}",
        )
        _set_job_phase(job_id, "merging", "Merging audio and video...")
        final_path = _merge_streams(video_temp, audio_temp, target_dir, f"{title_slug}_{resolution}", video_choice, audio_choice)
        filename = final_path.name

    return {
        "video_id": video_id,
        "title": yt.title,
        "resolution": resolution,
        "filename": filename,
        "file_path": str(final_path),
        "download_url": _download_url(video_id, filename),
        "merged": True,
        "progressive": False,
    }


def _handle_video_info(url: str):
    if not url:
        return _json_error("Missing 'url' parameter in the request body.", 400)

    if not is_valid_youtube_url(url):
        return _json_error("Invalid YouTube URL.", 400)

    try:
        yt = YouTube(url)
        video_id = _extract_video_id(url)
        return jsonify(_get_video_metadata(yt, video_id)), 200
    except Exception as exc:
        return _json_error(str(exc), 500)


def _handle_available_resolutions(url: str):
    if not url:
        return _json_error("Missing 'url' parameter in the request body.", 400)

    if not is_valid_youtube_url(url):
        return _json_error("Invalid YouTube URL.", 400)

    try:
        yt = YouTube(url)
        video_id = _extract_video_id(url)
        streams = _list_streams(yt)
        available_resolutions = [stream["resolution"] for stream in streams]
        best_stream = _best_stream_by_resolution(
            list(yt.streams.filter(type="video")),
            "best",
        )
        best_resolution = _stream_resolution_label(best_stream) if best_stream else None

        return (
            jsonify(
                {
                    "video_id": video_id,
                    "title": yt.title,
                    "author": yt.author,
                    "best_resolution": best_resolution,
                    "available_resolutions": available_resolutions,
                    "streams": streams,
                }
            ),
            200,
        )
    except Exception as exc:
        return _json_error(str(exc), 500)


def _handle_download(url: str, resolution: str | None):
    if not url:
        return _json_error("Missing 'url' parameter in the request body.", 400)

    if not is_valid_youtube_url(url):
        return _json_error("Invalid YouTube URL.", 400)

    try:
        job_id = _create_job(url, resolution)
        worker = threading.Thread(target=_start_download_job, args=(job_id, url, resolution), daemon=True)
        worker.start()
        return (
            jsonify(
                {
                    "message": "Download job started.",
                    "job_id": job_id,
                    "status": "queued",
                }
            ),
            202,
        )
    except Exception as exc:
        return _json_error(str(exc), 500)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/api/video-info", methods=["POST", "OPTIONS"])
@app.route("/video_info", methods=["POST", "OPTIONS"])
def video_info():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json(silent=True) or {}
    return _handle_video_info(data.get("url", ""))


@app.route("/api/available-resolutions", methods=["POST", "OPTIONS"])
@app.route("/available_resolutions", methods=["POST", "OPTIONS"])
def available_resolutions():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json(silent=True) or {}
    return _handle_available_resolutions(data.get("url", ""))


@app.route("/api/download", methods=["POST", "OPTIONS"])
def download():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json(silent=True) or {}
    return _handle_download(data.get("url", ""), data.get("resolution"))


@app.route("/api/jobs/<job_id>", methods=["GET", "OPTIONS"])
def job_status(job_id):
    if request.method == "OPTIONS":
        return jsonify({}), 200

    job = _get_job(job_id)
    if not job:
        return _json_error("Job not found.", 404)

    return jsonify(job), 200


@app.route("/download/<resolution>", methods=["POST", "OPTIONS"])
def download_by_resolution(resolution):
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json(silent=True) or {}
    return _handle_download(data.get("url", ""), resolution)


@app.route("/api/downloads/<video_id>/<filename>", methods=["GET"])
@app.route("/downloads/<video_id>/<filename>", methods=["GET"])
def serve_download(video_id, filename):
    directory = DOWNLOAD_ROOT / video_id
    return send_from_directory(directory, filename, as_attachment=True)


if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)
