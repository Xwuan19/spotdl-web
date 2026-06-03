"""
SpotDL Web App — Backend v3 (FastAPI)
- Hỗ trợ link Spotify, YouTube Music, YouTube
- Kiểm tra thư mục sau khi tải (Folder Validation)
- Xử lý lỗi một phần playlist (Partial Success)
- Gửi danh sách bài lỗi về Frontend
"""

import asyncio
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

app = FastAPI(title="SpotDL Web App", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp/spotdl_downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

job_results: dict[str, str] = {}
job_zip_names: dict[str, str] = {}   # tên đẹp cho Content-Disposition

# ── URL Validation ────────────────────────────────────────────────────────────
_VALID_HOSTS = [
    "spotify.com",
    "music.youtube.com",
    "youtube.com",
    "youtu.be",
]

def is_valid_url(url: str) -> bool:
    """Chấp nhận Spotify, YouTube Music, YouTube."""
    return any(host in url for host in _VALID_HOSTS)

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
    """Đệ quy tìm tất cả file .flac trong thư mục."""
    return list(directory.rglob("*.flac"))


def _is_ytm_url(url: str) -> bool:
    """Kiểm tra có phải link YouTube Music không."""
    return "music.youtube.com" in url


def _build_spotdl_cmd(url: str) -> list[str]:
    """
    Tạo lệnh spotdl phù hợp:
    - Link YTM → KHÔNG dùng {list-name} (tránh None)
    - Link Spotify / YouTube → dùng {list-name} cho playlist, fallback "music" nếu None
    """
    if _is_ytm_url(url):
        return [
            "spotdl", "download", url,
            "--output",   "{title} - {artist}.{output-ext}",
            "--format",   "flac",
            "--audio",    "youtube-music",
            "--simple-tui",
        ]
    else:
        return [
            "spotdl", "download", url,
            "--output", "{list-name|music}/{title} - {artist}.{output-ext}",
            "--format",  "flac",
            "--audio",   "youtube-music",
            "--simple-tui",
        ]


def _safe_filename(name: str) -> str:
    """Loại bỏ ký tự không hợp lệ trong tên file/folder."""
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.strip(". ")
    return name[:80] or "music"  # giới hạn độ dài, fallback nếu rỗng


def _get_zip_name(work_dir: Path, done_songs: list[str]) -> str:
    """
    Xác định tên ZIP:
    1. Nếu có subfolder (playlist) → dùng tên subfolder
    2. Nếu chỉ có 1 bài → dùng tên bài đó
    3. Fallback → "music"
    """
    subdirs = [d for d in work_dir.iterdir() if d.is_dir()]
    if subdirs:
        return _safe_filename(subdirs[0].name)
    if len(done_songs) == 1:
        return _safe_filename(done_songs[0])
    # nhiều bài không có subfolder (hiếm gặp)
    return _safe_filename(done_songs[0]) if done_songs else "music"


async def stream_download(
    job_id: str, url: str, work_dir: Path
) -> AsyncGenerator[str, None]:
    """
    Generator SSE với Folder Validation + Partial Success tracking.
    """
    cmd = _build_spotdl_cmd(url)

    yield _sse({"type": "start", "message": "Đang khởi động spotdl..."})

    # ── Theo dõi bài thành công / thất bại trong session ─────────────────────
    done_songs:   list[str] = []   # tên bài đã Done
    failed_songs: list[str] = []   # tên bài bị Error/Skipped-with-error

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

            # Theo dõi kết quả từng bài
            if event["type"] == "song_status":
                if event["status"] == "done":
                    done_songs.append(event["title"])
                elif event["status"] == "error":
                    failed_songs.append(event["title"])

            yield _sse(event)
            await asyncio.sleep(0)

        await process.wait()

        # returncode 2+ = lỗi nghiêm trọng (không phải lỗi 1 bài)
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
            # Không có file nào → thất bại hoàn toàn
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

        # Nếu log theo dõi không bắt được tên bài Done (ví dụ spotdl in khác),
        # dùng số file thực tế làm fallback
        actual_done  = len(flac_files)
        actual_failed = len(failed_songs)

        # ── PARTIAL SUCCESS: cảnh báo bài lỗi nhưng vẫn nén ─────────────────
        partial_info = None
        if failed_songs:
            partial_info = {
                "done_count":   actual_done,
                "failed_count": actual_failed,
                "failed_songs": failed_songs,
            }
            yield _sse({
                "type":         "partial_warning",
                "done_count":   actual_done,
                "failed_count": actual_failed,
                "failed_songs": failed_songs,
                "message": (
                    f"Đã tải được {actual_done} bài, "
                    f"{actual_failed} bài thất bại. Vẫn tiếp tục nén..."
                ),
            })

        # ── NÉN ZIP ───────────────────────────────────────────────────────────
        yield _sse({"type": "zipping", "message": "Đang nén nhạc thành file .zip..."})

        subdirs  = [d for d in work_dir.iterdir() if d.is_dir()]
        target   = subdirs[0] if subdirs else work_dir

        # Tên ZIP = tên playlist/bài, dùng job_id làm tên file thực tế (an toàn)
        # nhưng gửi tên đẹp về frontend qua Content-Disposition
        zip_name      = _get_zip_name(work_dir, done_songs)
        zip_base      = str(DOWNLOAD_DIR / job_id)          # path thực tế dùng UUID (an toàn)
        zip_path      = Path(zip_base + ".zip")
        zip_nice_name = f"{zip_name}.zip"                  # tên hiển thị cho user

        await asyncio.to_thread(
            shutil.make_archive, zip_base, "zip", str(target)
        )

        job_results[job_id] = str(zip_path)
        job_zip_names[job_id] = zip_nice_name               # lưu tên đẹp riêng

        yield _sse({
            "type":         "zip_ready",
            "job_id":       job_id,
            "filename":     zip_path.name,
            "done_count":   actual_done,
            "failed_count": actual_failed,
            "failed_songs": failed_songs,
            "message":      f"Hoàn tất! {actual_done} bài đã sẵn sàng.",
        })

    except FileNotFoundError:
        yield _sse({
            "type":    "error",
            "message": "Không tìm thấy lệnh 'spotdl'. Hãy kiểm tra Dockerfile.",
        })
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
            detail=(
                "URL không hợp lệ. Chấp nhận: "
                "Spotify (spotify.com), "
                "YouTube Music (music.youtube.com), "
                "YouTube (youtube.com / youtu.be)."
            ),
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


# ── API: Tải zip ──────────────────────────────────────────────────────────────
@app.get("/api/file/{job_id}")
async def download_zip(job_id: str):
    zip_path = job_results.get(job_id)
    if not zip_path or not Path(zip_path).exists():
        raise HTTPException(status_code=404, detail="File không tồn tại hoặc đã bị xóa.")

    async def stream_and_cleanup():
        try:
            with open(zip_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            Path(zip_path).unlink(missing_ok=True)
            job_results.pop(job_id, None)

    nice_name = job_zip_names.get(job_id, Path(zip_path).name)

    async def cleanup_names():
        job_zip_names.pop(job_id, None)

    return StreamingResponse(
        stream_and_cleanup(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{nice_name}"'},
    )


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health")
def health_check():
    return {
        "status":  "ok" if shutil.which("spotdl") and shutil.which("ffmpeg") else "degraded",
        "spotdl":  shutil.which("spotdl") is not None,
        "ffmpeg":  shutil.which("ffmpeg") is not None,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
