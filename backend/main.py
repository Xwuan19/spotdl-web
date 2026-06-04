"""
SpotDL Web App — Backend v5 (FastAPI)
Logic:
- Link Spotify  → spotdl (metadata Spotify + audio từ YTM)
- Link YTM/YT   → yt-dlp trực tiếp (metadata từ YTM, không cần Spotify)
"""

import asyncio
from urllib.parse import quote
import json
import logging
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="SpotDL Web App", version="5.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp/spotdl_downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

job_results:   dict[str, str] = {}
job_filenames: dict[str, str] = {}
job_mimetypes: dict[str, str] = {}

# ── URL helpers ───────────────────────────────────────────────────────────────
_VALID_HOSTS = ["spotify.com", "music.youtube.com", "youtube.com", "youtu.be"]

def is_valid_url(url: str) -> bool:
    return any(h in url for h in _VALID_HOSTS)

def _is_spotify(url: str) -> bool:
    return "spotify.com" in url

def _is_ytm_or_yt(url: str) -> bool:
    return "music.youtube.com" in url or "youtube.com" in url or "youtu.be" in url

def _is_playlist(url: str) -> bool:
    return "playlist" in url or "album" in url or "artist" in url


# ── Regex helpers ─────────────────────────────────────────────────────────────
_RE_SONG_STATUS = re.compile(
    r"^(.+?):\s+(Downloading|Converting|Embedding metadata|Done|Skipped|Error)\s*$",
    re.IGNORECASE,
)
_RE_OVERALL = re.compile(r"^(\d+)/(\d+)\s+complete\s*$", re.IGNORECASE)

STATUS_TO_PERCENT = {
    "downloading": 30, "converting": 65,
    "embedding metadata": 90, "done": 100,
    "skipped": 100, "error": 100,
}

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return name.strip(". ")[:80] or "music"

def _scan_flac(directory: Path) -> list[Path]:
    return list(directory.rglob("*.flac"))


# ════════════════════════════════════════════════════════════════════════════════
# SPOTDL — dùng cho link Spotify
# ════════════════════════════════════════════════════════════════════════════════
def _spotdl_cmd(url: str) -> list[str]:
    return [
        "spotdl", "download", url,
        "--output", "{title} - {artist}.{output-ext}",
        "--format", "flac",
        "--audio",  "youtube-music",
        "--simple-tui",
    ]

def parse_spotdl_line(raw: str) -> dict | None:
    line = re.sub(r"\[/?[a-zA-Z0-9 _#=]+\]", "", raw.strip()).strip()
    if not line:
        return None
    m = _RE_SONG_STATUS.match(line)
    if m:
        status = m.group(2).strip().lower()
        return {
            "type": "song_status", "title": m.group(1).strip(),
            "status": status, "percent": STATUS_TO_PERCENT.get(status, 30),
        }
    m = _RE_OVERALL.match(line)
    if m:
        return {"type": "overall", "done": int(m.group(1)), "total": int(m.group(2))}
    if re.search(r"(error|exception|traceback|failed)", line, re.IGNORECASE) and len(line) < 300:
        return {"type": "log_error", "message": line}
    if len(line) < 200:
        return {"type": "log", "message": line}
    return None


async def _run_spotdl(
    job_id: str, url: str, work_dir: Path
) -> AsyncGenerator[str, None]:
    cmd = _spotdl_cmd(url)
    logger.info("spotdl cmd: %s", " ".join(cmd))
    yield _sse({"type": "start", "message": "Đang khởi động spotdl..."})

    done_songs:   list[str] = []
    failed_songs: list[str] = []

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(work_dir),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )

    async for raw in process.stdout:
        line  = raw.decode("utf-8", errors="replace")
        event = parse_spotdl_line(line)
        if not event:
            await asyncio.sleep(0)
            continue
        if event["type"] == "song_status":
            if event["status"] == "done":
                done_songs.append(event["title"])
            elif event["status"] == "error":
                failed_songs.append(event["title"])
        yield _sse(event)
        await asyncio.sleep(0)

    stderr_out = await process.stderr.read()
    if stderr_out:
        logger.error("spotdl stderr: %s", stderr_out.decode("utf-8", errors="replace")[:2000])

    await process.wait()

    if process.returncode not in (0, 1):
        yield _sse({"type": "error", "message": f"spotdl lỗi (code {process.returncode})."})
        return

    async for ev in _finalize(job_id, work_dir, done_songs, failed_songs):
        yield ev


# ════════════════════════════════════════════════════════════════════════════════
# YT-DLP — dùng cho link YTM / YouTube
# ════════════════════════════════════════════════════════════════════════════════
def _ytdlp_cmd(url: str, work_dir: Path) -> list[str]:
    """
    yt-dlp tải FLAC trực tiếp, embed metadata từ YouTube (không cần Spotify).
    --embed-thumbnail  → ảnh album
    --embed-metadata   → title, artist, album từ YouTube
    --parse-metadata   → map uploader → artist
    """
    return [
        "yt-dlp", url,
        "--format", "bestaudio/best",
        "--extract-audio",
        "--audio-format", "flac",
        "--audio-quality", "0",
        "--embed-thumbnail",
        "--embed-metadata",
        "--add-metadata",
        "--parse-metadata", "uploader:%(artist)s",
        "--output", str(work_dir / "%(title)s - %(uploader)s.%(ext)s"),
        "--no-playlist",
        "--newline",
    ]

def parse_ytdlp_line(raw: str) -> dict | None:
    line = raw.strip()
    if not line:
        return None

    # [download] 100% of X.XXMiB
    dl = re.match(r"\[download\]\s+(\d+(?:\.\d+)?)%\s+of", line)
    if dl:
        pct = float(dl.group(1))
        return {"type": "song_status", "title": "Đang tải...", "status": "downloading", "percent": int(pct * 0.6)}

    # [ExtractAudio] ...
    if "[ExtractAudio]" in line or "Destination" in line:
        return {"type": "song_status", "title": "Đang chuyển đổi FLAC...", "status": "converting", "percent": 70}

    # [EmbedThumbnail] or [Metadata]
    if "EmbedThumbnail" in line or "Metadata" in line:
        return {"type": "song_status", "title": "Đang gắn metadata...", "status": "embedding metadata", "percent": 90}

    if re.search(r"(error|failed)", line, re.IGNORECASE) and len(line) < 300:
        return {"type": "log_error", "message": line}

    return None


async def _run_ytdlp(
    job_id: str, url: str, work_dir: Path
) -> AsyncGenerator[str, None]:
    cmd = _ytdlp_cmd(url, work_dir)
    logger.info("yt-dlp cmd: %s", " ".join(cmd))
    yield _sse({"type": "start", "message": "Đang khởi động yt-dlp..."})
    yield _sse({"type": "overall", "done": 0, "total": 1})

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(work_dir),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )

    song_title = "Bài hát"
    async for raw in process.stdout:
        line  = raw.decode("utf-8", errors="replace")
        logger.info("yt-dlp: %s", line.rstrip())

        # Lấy tên bài từ dòng [youtube] title
        m = re.search(r'\[youtube\]\s+\S+:\s+(.+)', line)
        if m and "Downloading" not in line:
            song_title = m.group(1).strip()

        event = parse_ytdlp_line(line)
        if event:
            if event.get("title") == "Đang tải...":
                event["title"] = song_title
            yield _sse(event)
        await asyncio.sleep(0)

    await process.wait()

    if process.returncode != 0:
        yield _sse({"type": "error", "message": "yt-dlp thất bại. Link có thể bị giới hạn vùng địa lý."})
        return

    yield _sse({"type": "song_status", "title": song_title, "status": "done", "percent": 100})
    yield _sse({"type": "overall", "done": 1, "total": 1})

    async for ev in _finalize(job_id, work_dir, [song_title], []):
        yield ev


# ════════════════════════════════════════════════════════════════════════════════
# FINALIZE — Kiểm tra file, đóng gói, gửi event xong
# ════════════════════════════════════════════════════════════════════════════════
async def _finalize(
    job_id: str, work_dir: Path,
    done_songs: list[str], failed_songs: list[str]
) -> AsyncGenerator[str, None]:
    yield _sse({"type": "validating", "message": "Đang kiểm tra file đã tải..."})

    flac_files    = await asyncio.to_thread(_scan_flac, work_dir)
    actual_done   = len(flac_files)
    actual_failed = len(failed_songs)

    if not flac_files:
        yield _sse({
            "type": "error_empty",
            "message": (
                "Không tải được bài nào. Có thể do:\n"
                "• Link bị giới hạn vùng địa lý\n"
                "• Bài hát không có trên YouTube Music\n"
                "• Thử đổi sang link YouTube Music trực tiếp"
            ),
        })
        return

    if failed_songs:
        yield _sse({
            "type": "partial_warning",
            "done_count": actual_done, "failed_count": actual_failed,
            "failed_songs": failed_songs,
            "message": f"Đã tải {actual_done} bài, {actual_failed} bài thất bại.",
        })

    # ── 1 file → trả thẳng FLAC ──────────────────────────────────────────────
    if actual_done == 1:
        flac_path = flac_files[0]
        dest      = DOWNLOAD_DIR / f"{job_id}.flac"
        await asyncio.to_thread(shutil.copy2, str(flac_path), str(dest))

        job_results[job_id]   = str(dest)
        job_filenames[job_id] = flac_path.name
        job_mimetypes[job_id] = "audio/flac"

        yield _sse({
            "type": "file_ready", "job_id": job_id,
            "filename": flac_path.name,
            "done_count": 1, "failed_count": actual_failed,
            "failed_songs": failed_songs,
            "message": "Hoàn tất! File FLAC sẵn sàng tải.",
        })
        return

    # ── Nhiều file → ZIP ──────────────────────────────────────────────────────
    yield _sse({"type": "zipping", "message": "Đang nén nhạc thành file .zip..."})

    zip_name = _safe_filename(done_songs[0]) if done_songs else "music"
    zip_base = str(DOWNLOAD_DIR / job_id)
    zip_path = Path(zip_base + ".zip")

    await asyncio.to_thread(shutil.make_archive, zip_base, "zip", str(work_dir))

    job_results[job_id]   = str(zip_path)
    job_filenames[job_id] = f"{zip_name}.zip"
    job_mimetypes[job_id] = "application/zip"

    yield _sse({
        "type": "zip_ready", "job_id": job_id,
        "filename": f"{zip_name}.zip",
        "done_count": actual_done, "failed_count": actual_failed,
        "failed_songs": failed_songs,
        "message": f"Hoàn tất! {actual_done} bài đã sẵn sàng.",
    })


# ════════════════════════════════════════════════════════════════════════════════
# MAIN STREAM DISPATCHER
# ════════════════════════════════════════════════════════════════════════════════
async def stream_download(
    job_id: str, url: str, work_dir: Path
) -> AsyncGenerator[str, None]:
    try:
        if _is_spotify(url):
            async for ev in _run_spotdl(job_id, url, work_dir):
                yield ev
        else:
            async for ev in _run_ytdlp(job_id, url, work_dir):
                yield ev
    except FileNotFoundError as e:
        yield _sse({"type": "error", "message": f"Không tìm thấy lệnh: {e}"})
    except Exception as exc:
        logger.exception("Lỗi stream_download")
        yield _sse({"type": "error", "message": f"Lỗi server: {exc}"})
    finally:
        await asyncio.to_thread(shutil.rmtree, str(work_dir), ignore_errors=True)


# ── API endpoints ─────────────────────────────────────────────────────────────
class DownloadRequest(BaseModel):
    url: str

@app.post("/api/download")
async def create_job(req: DownloadRequest):
    url = req.url.strip()
    if not is_valid_url(url):
        raise HTTPException(status_code=400, detail="URL không hợp lệ.")
    job_id   = str(uuid.uuid4())
    work_dir = DOWNLOAD_DIR / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    return {"job_id": job_id}

@app.get("/api/stream/{job_id}")
async def sse_stream(job_id: str, url: str):
    work_dir = DOWNLOAD_DIR / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    return StreamingResponse(
        stream_download(job_id, url, work_dir),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )

@app.get("/api/file/{job_id}")
async def download_file(job_id: str):
    file_path = job_results.get(job_id)
    if not file_path or not Path(file_path).exists():
        raise HTTPException(status_code=404, detail="File không tồn tại hoặc đã bị xóa.")

    nice_name    = job_filenames.get(job_id, Path(file_path).name)
    mime_type    = job_mimetypes.get(job_id, "application/octet-stream")
    encoded_name = quote(nice_name, safe="")

    async def stream_and_cleanup():
        try:
            with open(file_path, "rb") as f:
                while chunk := f.read(65536):
                    yield chunk
        finally:
            Path(file_path).unlink(missing_ok=True)
            job_results.pop(job_id, None)
            job_filenames.pop(job_id, None)
            job_mimetypes.pop(job_id, None)

    return StreamingResponse(
        stream_and_cleanup(),
        media_type=mime_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"},
    )

@app.get("/health")
def health():
    return {
        "status": "ok" if shutil.which("spotdl") and shutil.which("ffmpeg") else "degraded",
        "spotdl": shutil.which("spotdl") is not None,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "yt-dlp": shutil.which("yt-dlp") is not None,
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
