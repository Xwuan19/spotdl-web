# ── Python 3.12 — nằm trong range >=3.10,<3.15 mà spotdl 4.5.0 yêu cầu ──────
FROM python:3.12-slim

# ── Cài ffmpeg (bắt buộc cho spotdl chuyển đổi sang FLAC) ───────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        && \
    rm -rf /var/lib/apt/lists/*

# ── Working directory ─────────────────────────────────────────────────────────
WORKDIR /app

# ── Cài Python dependencies ───────────────────────────────────────────────────
# Copy requirements trước để Docker cache layer này
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ── Copy source ───────────────────────────────────────────────────────────────
COPY main.py .

# ── Thư mục tạm để lưu nhạc đang tải ─────────────────────────────────────────
RUN mkdir -p /tmp/spotdl_downloads && chmod 777 /tmp/spotdl_downloads

# ── Port ──────────────────────────────────────────────────────────────────────
EXPOSE 8000

# ── Start (Railway truyền $PORT tự động) ──────────────────────────────────────
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
