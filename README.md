# 🎵 SpotDL Web App v2 — Hướng dẫn cài đặt toàn bộ

**Ứng dụng tải nhạc Spotify chất lượng FLAC lossless, giao diện Web đẹp, tự host miễn phí.**

> Phiên bản này tương thích với **spotdl 4.5.0** — đã kiểm tra source code thực tế.

---

## 📂 Cấu trúc thư mục

```
spotdl-web/
├── backend/
│   ├── main.py           ← Server Python/FastAPI (logic tải nhạc)
│   ├── requirements.txt  ← Thư viện cần cài
│   ├── Dockerfile        ← Railway dùng cái này để build
│   └── railway.toml      ← Cấu hình Railway
├── frontend/
│   ├── index.html        ← Toàn bộ giao diện web (1 file duy nhất)
│   └── vercel.json       ← Cấu hình Vercel
├── .gitignore
└── README.md             ← File này
```

---

## 🚀 HƯỚNG DẪN DEPLOY — TỪNG BƯỚC

### ═══ BƯỚC 1: Tạo tài khoản GitHub ═══

1. Mở trình duyệt, vào **https://github.com**
2. Nhấn **Sign up** → điền email, mật khẩu, tên người dùng
3. Xác nhận email
4. Đăng nhập vào GitHub

---

### ═══ BƯỚC 2: Upload code lên GitHub ═══

1. Sau khi đăng nhập, nhấn dấu **+** (góc trên bên phải) → **New repository**
2. Đặt tên: `spotdl-web`
3. Chọn **Public**
4. Nhấn **Create repository**
5. Trên trang repo vừa tạo, nhấn **uploading an existing file**
6. Giải nén file `spotdl-web-project.zip` trên máy tính
7. Kéo thả **toàn bộ nội dung bên trong thư mục** `spotdl-web/` vào trang upload
   *(phải thấy các thư mục `backend/`, `frontend/`, file `README.md`, `.gitignore`)*
8. Cuộn xuống → nhấn **Commit changes**
9. Chờ upload xong

---

### ═══ BƯỚC 3: Deploy BACKEND lên Railway ═══

> Railway cung cấp $5 credit/tháng miễn phí — đủ cho dùng cá nhân.

1. Vào **https://railway.app**
2. Nhấn **Login** → chọn **Login with GitHub**
3. Nhấn **New Project** → **Deploy from GitHub repo**
4. Chọn repo `spotdl-web`
5. Railway hỏi chọn folder — chọn **backend**
   *(Railway tự nhận Dockerfile trong thư mục này)*
6. Nhấn **Deploy Now**
7. Chờ Railway build xong — thường mất **3–8 phút** lần đầu
   *(Thanh tiến trình màu xanh ở góc trên)*
8. Sau khi deploy xong:
   - Vào tab **Settings** (trong project)
   - Cuộn xuống phần **Networking**
   - Nhấn **Generate Domain**
   - Copy URL dạng: `https://spotdl-web-production-xxxx.up.railway.app`

   ⚠️ **LƯU URL NÀY LẠI — dùng ở Bước 5**

---

### ═══ BƯỚC 4: Deploy FRONTEND lên Vercel ═══

> Vercel hoàn toàn miễn phí cho static site.

1. Vào **https://vercel.com**
2. Nhấn **Sign Up** → **Continue with GitHub**
3. Nhấn **Add New...** → **Project**
4. Tìm repo `spotdl-web` → nhấn **Import**
5. Trong mục **Root Directory** → nhấn **Edit** → chọn `frontend`
6. Giữ nguyên mọi thứ khác
7. Nhấn **Deploy**
8. Chờ ~1 phút → Vercel tạo URL dạng: `https://spotdl-web-xxx.vercel.app`

---

### ═══ BƯỚC 5: Kết nối Frontend ↔ Backend ═══ ⚠️ QUAN TRỌNG NHẤT

Đây là bước **bắt buộc** — nếu bỏ qua, ứng dụng sẽ không hoạt động.

**Cách thực hiện:**

1. Giải nén `spotdl-web-project.zip` trên máy (nếu chưa)
2. Mở file `frontend/index.html` bằng **Notepad++**
3. Dùng **Ctrl+F** để tìm dòng này:
   ```
   const API = window.BACKEND_URL || "http://localhost:8000";
   ```
4. Thay toàn bộ dòng đó bằng URL Railway của bạn (từ Bước 3), ví dụ:
   ```
   const API = "https://spotdl-web-production-xxxx.up.railway.app";
   ```
   *(Thay `spotdl-web-production-xxxx` bằng tên thật của bạn)*
5. **Lưu file** (Ctrl+S)
6. Vào GitHub → vào repo `spotdl-web` → vào thư mục `frontend`
7. Nhấn vào file `index.html`
8. Nhấn biểu tượng **bút chì** (Edit this file) ở góc trên bên phải
9. Tìm lại dòng cũ, thay bằng dòng mới giống bước 4
10. Cuộn xuống → nhấn **Commit changes**
11. Vercel tự động cập nhật trong vòng ~30 giây

---

## ✅ Kiểm tra hoạt động

1. Mở URL Vercel trong trình duyệt (máy tính hoặc điện thoại)
2. Dán link Spotify (track, playlist, hoặc album)
3. Nhấn **Bắt đầu tải**
4. Quan sát các bài nhạc xuất hiện trong danh sách với trạng thái:
   - `Đang tải` → `Chuyển đổi` → `Metadata` → `✓ Xong`
5. Khi tất cả xong → nhấn **Tải file .zip về máy**

---

## 🔧 Chạy thử trên máy tính (Không cần Railway/Vercel)

Yêu cầu: Python 3.10+, pip, ffmpeg đã cài

```
# Bước 1: Cài ffmpeg
# Windows: tải tại https://ffmpeg.org/download.html → giải nén → thêm vào PATH
# Mac:     brew install ffmpeg
# Ubuntu:  sudo apt install ffmpeg

# Bước 2: Cài thư viện Python
cd backend
pip install -r requirements.txt

# Bước 3: Chạy server
python main.py
# → Server chạy tại http://localhost:8000

# Bước 4: Mở giao diện
# Click đúp vào frontend/index.html
# (API_BASE mặc định là http://localhost:8000 nên không cần sửa gì)
```

---

## ❓ XỬ LÝ LỖI THƯỜNG GẶP

### ❌ "Không thể kết nối backend" / Trang trắng khi bấm tải
→ **Nguyên nhân:** Chưa sửa URL trong `index.html` (Bước 5)  
→ **Sửa:** Kiểm tra lại dòng `const API = ...` trong `frontend/index.html`  
→ **Lưu ý:** URL phải bắt đầu bằng `https://` và **không** có dấu `/` ở cuối

---

### ❌ "spotdl thoát với mã lỗi" hoặc không tải được
→ **Nguyên nhân 1:** Railway chưa build xong lần đầu  
→ **Sửa:** Chờ thêm 5–10 phút, vào Railway kiểm tra tab **Deploy** xem có lỗi không

→ **Nguyên nhân 2:** Link Spotify bị hạn chế vùng (geo-restrict)  
→ **Sửa:** Thử link khác, hoặc link playlist thay vì track

---

### ❌ Railway báo lỗi khi build
→ Vào Railway → tab **Deploy** → xem log màu đỏ  
→ Hay gặp nhất: thiếu file. Kiểm tra GitHub có đủ 4 file trong `backend/` không:
  - `main.py`, `requirements.txt`, `Dockerfile`, `railway.toml`

---

### ❌ Railway tự tắt sau 30 phút không dùng (gói miễn phí)
→ Đây là hành vi bình thường của Railway gói free  
→ Khi bạn bấm tải lần đầu, server "ngủ" mất ~20-30 giây để khởi động lại  
→ Chờ rồi bấm lại lần 2 là được  
→ Nếu muốn server chạy liên tục: nâng cấp Railway lên $5/tháng

---

### ❌ File .zip tải về bị trống hoặc rất nhỏ
→ spotdl không tìm thấy bài hát trên YouTube Music  
→ Thử lại với playlist khác, hoặc link album thay vì track đơn lẻ

---

## 🌐 Bảng so sánh dịch vụ deploy

| Dịch vụ      | Dùng cho         | Giá              | Link              |
|--------------|------------------|------------------|-------------------|
| **Vercel**   | Frontend (HTML)  | Miễn phí mãi mãi | vercel.com        |
| **Railway**  | Backend (Python) | $5 credit/tháng  | railway.app       |
| **Render**   | Backend (thay thế)| Miễn phí (chậm) | render.com        |
| **Fly.io**   | Backend (thay thế)| $0 nếu ít dùng  | fly.io            |

> **Khuyến nghị:** Vercel (frontend) + Railway (backend) là combo ổn định nhất.

---

## 📋 Thông tin kỹ thuật (cho người muốn biết)

- **Backend:** Python 3.12 + FastAPI + spotdl 4.5.0
- **Lệnh tải:** `spotdl download [url] --output "{list-name}/{title} - {artist}.{output-ext}" --format flac --audio youtube-music --simple-tui`
- **Lý do dùng `--simple-tui`:** Tránh lỗi buffer/flush khi chạy trong container, đảm bảo metadata được gắn đúng
- **Lý do dùng `--audio youtube-music`:** Nguồn ổn định nhất, ít bị block nhất
- **Realtime:** Server-Sent Events (SSE) — nhẹ hơn WebSocket, không cần thư viện thêm
- **Cleanup:** File zip tự xóa sau khi người dùng tải về

---

*Dự án cá nhân. Chỉ dùng cho mục đích học tập và sử dụng cá nhân.*  
*Tôn trọng bản quyền âm nhạc.*
