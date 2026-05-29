"""
SpotDL Web App — Backend (FastAPI)
Tương thích với spotdl 4.5.0

Format output thực tế của spotdl --simple-tui (từ source code 4.5.0):
  Per-song:   "{song_name}: Downloading"
              "{song_name}: Converting"       (nếu có delta tiến trình)
              "{song_name}: Embedding metadata"
              "{song_name}: Done"
              "{song_name}: Skipped"
              "{song_name}: Error"
  Overall:    "{n}/{total} complete"

Lưu ý: --simple-tui KHÔNG in ra % tải về. Chỉ in trạng thái (status word).
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

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="SpotDL Web App", version="1.0.0")

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Sau khi deploy: thay "*" bằng URL Vercel của bạn
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Thư mục lưu file tạm ──────────────────────────────────────────────────────
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp/spotdl_downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ── Job registry: job_id → path file zip ──────────────────────────────────────
job_results: dict[str, str] = {}

# ── Trạng thái (status) → phần trăm ảo để hiển thị progress bar trên UI ──────
# spotdl --simple-tui không in ra % thực — ta map status → % để UI đẹp hơn
STATUS_TO_PERCENT = {
    "downloading":       30,
    "converting":        65,
    "embedding metadata": 90,
    "done":             100,
    "skipped":          100,
    "error":            100,
}

# ── Patterns để nhận diện dòng output của spotdl --simple-tui ─────────────────
# Pattern 1: "{song_name}: {status}"
# Ví dụ: "Blinding Lights: Downloading"
#        "Blinding Lights: Embedding metadata"
#        "Blinding Lights: Done"
_RE_SONG_STATUS = re.compile(
    r"^(.+?):\s+(Downloading|Converting|Embedding metadata|Done|Skipped|Error)\s*$",
    re.IGNORECASE,
)

# Pattern 2: "{n}/{total} complete"
# Ví dụ: "1/5 complete"
_RE_OVERALL = re.compile(r"^(\d+)/(\d+)\s+complete\s*$", re.IGNORECASE)

# Pattern 3: Lỗi phổ biến từ spotdl hoặc yt-dlp
_RE_ERROR = re.compile(r"(error|exception|traceback|failed)", re.IGNORECASE)


class DownloadRequest(BaseModel):
    url: str


def _sse(data: dict) -> str:
    """Tạo chuỗi SSE đúng chuẩn."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def parse_spotdl_line(raw: str) -> dict | None:
    """
    Phân tích 1 dòng output của spotdl --simple-tui.

    Dựa trực tiếp trên source code spotdl 4.5.0:
      progress_handler.py → SongTracker.update() →
        logger.info("%s: %s", self.song_name, message)   ← dòng per-song
        logger.info("%s/%s complete", done, total)        ← dòng tổng

    Trả về dict event hoặc None nếu không nhận ra.
    """
    line = raw.strip()

    # Loại bỏ Rich markup nếu còn sót (ví dụ: "[bold]...[/bold]")
    line = re.sub(r"\[/?[a-zA-Z0-9 _#=]+\]", "", line).strip()

    if not line:
        return None

    # ── Pattern 1: Per-song status ────────────────────────────────────────────
    m = _RE_SONG_STATUS.match(line)
    if m:
        title   = m.group(1).strip()
        status  = m.group(2).strip().lower()
        percent = STATUS_TO_PERCENT.get(status, 30)
        return {
            "type":    "song_status",
            "title":   title,
            "status":  status,       # "downloading" / "converting" / "embedding metadata" / "done" / "skipped" / "error"
            "percent": percent,      # % ảo để vẽ progress bar
        }

    # ── Pattern 2: Overall progress ──────────────────────────────────────────
    m = _RE_OVERALL.match(line)
    if m:
        return {
            "type":  "overall",
            "done":  int(m.group(1)),
            "total": int(m.group(2)),
        }

    # ── Pattern 3: Dòng lỗi quan trọng ────────────────────────────────────────
    if _RE_ERROR.search(line) and len(line) < 300:
        return {"type": "log_error", "message": line}

    # ── Dòng log thông thường — ít quan trọng ─────────────────────────────────
    if len(line) < 200:
        return {"type": "log", "message": line}

    return None


async def stream_download(
    job_id: str, url: str, work_dir: Path
) -> AsyncGenerator[str, None]:
    """
    Generator SSE: chạy spotdl, stream tiến trình, nén zip, trả link tải.
    """

    # Lệnh spotdl — giữ nguyên 100% theo yêu cầu bất di bất dịch
    cmd = [
        "spotdl", "download", url,
        "--output", "{list-name}/{title} - {artist}.{output-ext}",
        "--format",  "flac",
        "--audio",   "youtube-music",
        "--simple-tui",
    ]

    yield _sse({"type": "start", "message": "Đang khởi động spotdl..."})

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,   # Gộp stderr vào stdout để bắt được log
            cwd=str(work_dir),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},  # Tắt buffer để nhận ngay
        )

        async for raw_bytes in process.stdout:
            line = raw_bytes.decode("utf-8", errors="replace")
            event = parse_spotdl_line(line)
            if event:
                yield _sse(event)
            await asyncio.sleep(0)  # Nhường CPU để không block event loop

        await process.wait()

        if process.returncode not in (0, 1):
            # spotdl trả về 1 nếu có 1 bài lỗi nhưng các bài khác thành công
            yield _sse({
                "type":    "error",
                "message": f"spotdl kết thúc với mã lỗi {process.returncode}. "
                           f"Kiểm tra log ở trên để biết chi tiết.",
            })
            return

        # ── Nén thư mục thành .zip ────────────────────────────────────────────
        yield _sse({"type": "zipping", "message": "Đang nén nhạc thành file .zip..."})

        # spotdl tạo thư mục con theo tên playlist/album
        # Ví dụ: work_dir/My Playlist/{title} - {artist}.flac
        subdirs = [d for d in work_dir.iterdir() if d.is_dir()]
        target = subdirs[0] if subdirs else work_dir

        zip_base = str(DOWNLOAD_DIR / job_id)
        zip_path = Path(zip_base + ".zip")

        await asyncio.to_thread(
            shutil.make_archive, zip_base, "zip", str(target)
        )

        job_results[job_id] = str(zip_path)

        yield _sse({
            "type":     "zip_ready",
            "job_id":   job_id,
            "filename": zip_path.name,
            "message":  "Hoàn tất! File .zip đã sẵn sàng.",
        })

    except FileNotFoundError:
        yield _sse({
            "type":    "error",
            "message": (
                "Không tìm thấy lệnh 'spotdl'. "
                "Server chưa cài spotdl — hãy kiểm tra Dockerfile."
            ),
        })
    except Exception as exc:
        logger.exception("Lỗi không mong đợi trong stream_download")
        yield _sse({"type": "error", "message": f"Lỗi server: {exc}"})
    finally:
        # Xóa thư mục làm việc tạm (giữ lại file zip)
        await asyncio.to_thread(shutil.rmtree, str(work_dir), ignore_errors=True)


# ── API: Tạo job ──────────────────────────────────────────────────────────────
@app.post("/api/download")
async def create_download_job(req: DownloadRequest):
    url = req.url.strip()
    if "spotify.com" not in url:
        raise HTTPException(
            status_code=400,
            detail="URL không hợp lệ. Chỉ chấp nhận link Spotify.",
        )

    job_id  = str(uuid.uuid4())
    work_dir = DOWNLOAD_DIR / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    return {"job_id": job_id}


# ── API: SSE stream tiến trình ────────────────────────────────────────────────
@app.get("/api/stream/{job_id}")
async def sse_stream(job_id: str, url: str):
    work_dir = DOWNLOAD_DIR / job_id
    if not work_dir.exists():
        work_dir.mkdir(parents=True, exist_ok=True)

    return StreamingResponse(
        stream_download(job_id, url, work_dir),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",   # Quan trọng cho Nginx/Railway
            "Connection":       "keep-alive",
        },
    )


# ── API: Tải file zip ─────────────────────────────────────────────────────────
@app.get("/api/file/{job_id}")
async def download_zip(job_id: str):
    zip_path = job_results.get(job_id)
    if not zip_path or not Path(zip_path).exists():
        raise HTTPException(status_code=404, detail="File không tồn tại hoặc đã bị xóa.")

    async def file_stream_and_cleanup():
        try:
            with open(zip_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            # Tự xóa sau khi gửi xong
            Path(zip_path).unlink(missing_ok=True)
            job_results.pop(job_id, None)
            logger.info("Đã xóa file tạm: %s", zip_path)

    filename = Path(zip_path).name
    return StreamingResponse(
        file_stream_and_cleanup(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health")
def health_check():
    spotdl_found = shutil.which("spotdl") is not None
    ffmpeg_found = shutil.which("ffmpeg") is not None
    return {
        "status":  "ok" if (spotdl_found and ffmpeg_found) else "degraded",
        "spotdl":  spotdl_found,
        "ffmpeg":  ffmpeg_found,
    }


# ── Chạy local ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
