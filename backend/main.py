"""
SpotDL Web App — Backend v4 (FastAPI)
- Hỗ trợ link Spotify, YouTube Music, YouTube
- Link YTM bài lẻ: trả thẳng file FLAC, không ZIP
- Playlist/album: nén ZIP với tên playlist
- Metadata đúng cho link YTM nhờ --dont-filter-results
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

app = FastAPI(title="SpotDL Web App", version="4.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp/spotdl_downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# job_id → path file (FLAC hoặc ZIP)
job_results:   dict[str, str] = {}
# job_id → tên đẹp cho Content-Disposition
job_filenames: dict[str, str] = {}
# job_id → mime type
job_mimetypes: dict[str, str] = {}

# ── URL Validation ────────────────────────────────────────────────────────────
_VALID_HOSTS = ["spotify.com", "music.youtube.com", "youtube.com", "youtu.be"]

def is_valid_url(url: str) -> bool:
    return any(host in url for host in _VALID_HOSTS)

def _is_ytm_single(url: str) -> bool:
    """Link YTM bài lẻ (watch?v=...), không phải playlist."""
    return "music.youtube.com/watch" in url

def _is_playlist_url(url: str) -> bool:
    """Có phải playlist/album không (có 'playlist' hoặc 'album' trong URL)."""
    return "playlist" in url or "album" in url

# ── Status → % ảo ────────────────────────────────────────────────────────────
STATUS_TO_PERCENT = {
    "downloading":        30,
    "converting":         65,
    "embedding metadata": 90,
    "done":              100,
    "skipped":           100,
    "error":             100,
}

# ── Regex patterns ────────────────────────────────────────────────────────────
_RE_SONG_STATUS = re.compile(
    r"^(.+?):\s+(Downloading|Converting|Embedding metadata|Done|Skipped|Error)\s*$",
    re.IGNORECASE,
)
_RE_OVERALL = re.compile(r"^(\d+)/(\d+)\s+complete\s*$", re.IGNORECASE)
_RE_ERROR   = re.compile(r"(error|exception|traceback|failed)", re.IGNORECASE)


class DownloadRequest(BaseModel):
    url: str


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def parse_spotdl_line(raw: str) -> dict | None:
    line = raw.strip()
    line = re.sub(r"\[/?[a-zA-Z0-9 _#=]+\]", "", line).strip()
    if not line:
        return None

    m = _RE_SONG_STATUS.match(line)
    if m:
        title  = m.group(1).strip()
        status = m.group(2).strip().lower()
        return {
            "type":    "song_status",
            "title":   title,
            "status":  status,
            "percent": STATUS_TO_PERCENT.get(status, 30),
        }

    m = _RE_OVERALL.match(line)
    if m:
        return {"type": "overall", "done": int(m.group(1)), "total": int(m.group(2))}

    if _RE_ERROR.search(line) and len(line) < 300:
        return {"type": "log_error", "message": line}

    if len(line) < 200:
        return {"type": "log", "message": line}

    return None


def _scan_flac_files(directory: Path) -> list[Path]:
    return list(directory.rglob("*.flac"))


def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.strip(". ")
    return name[:80] or "music"


def _build_spotdl_cmd(url: str) -> list[str]:
    """
    Lệnh spotdl:
    - Link YTM bài lẻ: thêm --dont-filter-results để dùng đúng link đó,
      tránh spotdl đi match Spotify và lấy metadata sai
    - Các link khác: dùng lệnh chuẩn
    - KHÔNG dùng {list-name} trong template vì sẽ crash khi None
    """
    base = [
        "spotdl", "download", url,
        "--output", "{title} - {artist}.{output-ext}",
        "--format", "flac",
        "--audio",  "youtube-music",
        "--simple-tui",
    ]
    if _is_ytm_single(url):
        base.append("--dont-filter-results")
        base.append("--ytm-data")   # lấy metadata (tên, ảnh) trực tiếp từ YTM
    return base


async def stream_download(
    job_id: str, url: str, work_dir: Path
) -> AsyncGenerator[str, None]:
    cmd = _build_spotdl_cmd(url)
    is_single = _is_ytm_single(url) or not _is_playlist_url(url)

    yield _sse({"type": "start", "message": "Đang khởi động spotdl..."})

    done_songs:   list[str] = []
    failed_songs: list[str] = []

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(work_dir),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

        async for raw_bytes in process.stdout:
            line  = raw_bytes.decode("utf-8", errors="replace")
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

        await process.wait()

        if process.returncode not in (0, 1):
            yield _sse({
                "type":    "error",
                "message": (
                    f"spotdl kết thúc với mã lỗi {process.returncode}. "
                    "Có thể link không hỗ trợ hoặc nguồn bị chặn."
                ),
            })
            return

        # ── FOLDER VALIDATION ─────────────────────────────────────────────────
        yield _sse({"type": "validating", "message": "Đang kiểm tra file đã tải..."})
        flac_files = await asyncio.to_thread(_scan_flac_files, work_dir)

        if not flac_files:
            yield _sse({
                "type":    "error_empty",
                "message": (
                    "Không tải được bài nào. Có thể do:\n"
                    "• Link bị giới hạn vùng địa lý\n"
                    "• Bài hát không có trên YouTube Music\n"
                    "• Thử đổi sang link YouTube Music trực tiếp"
                ),
            })
            return

        actual_done   = len(flac_files)
        actual_failed = len(failed_songs)

        if failed_songs:
            yield _sse({
                "type":         "partial_warning",
                "done_count":   actual_done,
                "failed_count": actual_failed,
                "failed_songs": failed_songs,
                "message": f"Đã tải được {actual_done} bài, {actual_failed} bài thất bại.",
            })

        # ── BÀI LẺ: trả thẳng FLAC, không ZIP ────────────────────────────────
        if actual_done == 1:
            flac_path     = flac_files[0]
            nice_name     = flac_path.name
            dest_path     = DOWNLOAD_DIR / f"{job_id}.flac"
            await asyncio.to_thread(shutil.copy2, str(flac_path), str(dest_path))

            job_results[job_id]   = str(dest_path)
            job_filenames[job_id] = nice_name
            job_mimetypes[job_id] = "audio/flac"

            yield _sse({
                "type":         "file_ready",
                "job_id":       job_id,
                "filename":     nice_name,
                "done_count":   1,
                "failed_count": actual_failed,
                "failed_songs": failed_songs,
                "message":      "Hoàn tất! File FLAC sẵn sàng tải.",
            })
            return

        # ── PLAYLIST: nén ZIP ─────────────────────────────────────────────────
        yield _sse({"type": "zipping", "message": "Đang nén nhạc thành file .zip..."})

        zip_name      = _safe_filename(done_songs[0]) if done_songs else "music"
        zip_base      = str(DOWNLOAD_DIR / job_id)
        zip_path      = Path(zip_base + ".zip")

        await asyncio.to_thread(shutil.make_archive, zip_base, "zip", str(work_dir))

        job_results[job_id]   = str(zip_path)
        job_filenames[job_id] = f"{zip_name}.zip"
        job_mimetypes[job_id] = "application/zip"

        yield _sse({
            "type":         "zip_ready",
            "job_id":       job_id,
            "filename":     f"{zip_name}.zip",
            "done_count":   actual_done,
            "failed_count": actual_failed,
            "failed_songs": failed_songs,
            "message":      f"Hoàn tất! {actual_done} bài đã sẵn sàng.",
        })

    except FileNotFoundError:
        yield _sse({"type": "error", "message": "Không tìm thấy lệnh 'spotdl'. Hãy kiểm tra Dockerfile."})
    except Exception as exc:
        logger.exception("Lỗi không mong đợi trong stream_download")
        yield _sse({"type": "error", "message": f"Lỗi server: {exc}"})
    finally:
        await asyncio.to_thread(shutil.rmtree, str(work_dir), ignore_errors=True)


# ── API: Tạo job ──────────────────────────────────────────────────────────────
@app.post("/api/download")
async def create_download_job(req: DownloadRequest):
    url = req.url.strip()
    if not is_valid_url(url):
        raise HTTPException(
            status_code=400,
            detail="URL không hợp lệ. Chấp nhận: Spotify, YouTube Music, YouTube.",
        )
    job_id   = str(uuid.uuid4())
    work_dir = DOWNLOAD_DIR / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    return {"job_id": job_id}


# ── API: SSE stream ───────────────────────────────────────────────────────────
@app.get("/api/stream/{job_id}")
async def sse_stream(job_id: str, url: str):
    work_dir = DOWNLOAD_DIR / job_id
    if not work_dir.exists():
        work_dir.mkdir(parents=True, exist_ok=True)

    return StreamingResponse(
        stream_download(job_id, url, work_dir),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )


# ── API: Tải file (FLAC hoặc ZIP) ─────────────────────────────────────────────
@app.get("/api/file/{job_id}")
async def download_file(job_id: str):
    file_path = job_results.get(job_id)
    if not file_path or not Path(file_path).exists():
        raise HTTPException(status_code=404, detail="File không tồn tại hoặc đã bị xóa.")

    nice_name    = job_filenames.get(job_id, Path(file_path).name)
    mime_type    = job_mimetypes.get(job_id, "application/octet-stream")
    encoded_name = quote(nice_name, safe="")
    content_disp = f"attachment; filename*=UTF-8''{encoded_name}"

    async def stream_and_cleanup():
        try:
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            Path(file_path).unlink(missing_ok=True)
            job_results.pop(job_id, None)
            job_filenames.pop(job_id, None)
            job_mimetypes.pop(job_id, None)

    return StreamingResponse(
        stream_and_cleanup(),
        media_type=mime_type,
        headers={"Content-Disposition": content_disp},
    )


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health")
def health_check():
    return {
        "status": "ok" if shutil.which("spotdl") and shutil.which("ffmpeg") else "degraded",
        "spotdl": shutil.which("spotdl") is not None,
        "ffmpeg": shutil.which("ffmpeg") is not None,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
