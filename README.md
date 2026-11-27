# Multi-Feature Telegram Bot

Bot Telegram được tổ chức theo kiến trúc module hóa, mỗi tính năng nằm độc lập trong `bot/features/` và chạy qua bộ điều phối chung (`BotScheduler`).

## Cấu trúc thư mục

```text
.
├── main.py                         # Điểm khởi chạy bot (--once, --fail-on-error, --set-webhook, ...)
├── webhook_server.py               # HTTP Webhook server (dùng cho Render / chạy thử local)
├── api/
│   └── webhook.py                  # Vercel Serverless Function cho Telegram Webhook
├── .github/
│   └── workflows/
│       └── run-bot.yml             # GitHub Actions chạy tự động mỗi tiếng
├── .env                            # Cấu hình thật (không commit lên Git)
├── .env.example                    # Mẫu file cấu hình
├── .gitignore
├── requirements.txt
├── bot/
│   ├── config.py                   # Đọc cấu hình từ .env / biến môi trường
│   ├── core/                       # Hạ tầng dùng chung cho mọi tính năng
│   │   ├── feature.py              # Lớp cơ sở BaseFeature
│   │   ├── commands.py             # Bộ định tuyến lệnh Telegram (/start, /help, /gold_*)
│   │   ├── webhook.py              # Xử lý HTTP webhook dùng chung cho Vercel & Render
│   │   ├── github_vars.py          # Đồng bộ biến cấu hình qua GitHub Repository Variables API
│   │   ├── telegram_client.py      # Gửi tin nhắn, setWebhook, setMyCommands
│   │   ├── scheduler.py            # Điều phối nhiều tính năng theo chu kỳ riêng
│   │   ├── state.py                # Lưu trạng thái JSON giữa các lần chạy (dùng cho CI/cron)
│   │   └── logger.py               # Cấu hình logging thống nhất
│   ├── features/                   # Các tính năng của bot (mỗi tính năng 1 thư mục)
│   │   ├── __init__.py             # Đăng ký danh sách tính năng được kích hoạt
│   │   ├── gold_price/             # Tính năng theo dõi giá vàng SJC
│   │   │   ├── scraper.py          # Cào và phân tích dữ liệu HTML
│   │   │   └── feature.py          # Logic kiểm tra ngưỡng giá & gửi cảnh báo
│   │   ├── morning_news/           # Tính năng tóm tắt tin tức buổi sáng
│   │   │   ├── rss.py              # Lấy và gộp tin RSS (VnExpress, Tuổi Trẻ)
│   │   │   ├── summarizer.py       # Tóm tắt tin bằng Gemini AI (kèm dự phòng RSS)
│   │   │   └── feature.py          # Lập lịch gửi 07:00 sáng & lệnh /news_*
│   │   └── reminders/              # Tính năng nhắc việc & đếm ngược sự kiện
│   │       ├── models.py           # Mô hình sự kiện, tính lặp lại & JSON
│   │       ├── parser.py           # Parse ngày/giờ linh hoạt (DD/MM, +3d, ngaymai...)
│   │       └── feature.py          # Lập lịch nhắc tự động & lệnh /remind_*, /countdown
│   └── utils/                      # Hàm tiện ích dùng chung
│       └── parsers.py              # Parse & format giá tiền
└── tests/
    └── test_bot.py                 # Unit test cho parser, scraper, feature, config
```

## Cài đặt trên thiết bị mới (sau khi `git clone`)

Do `.venv/` và `.env` không được đẩy lên Git, trên máy mới bạn chỉ cần làm các bước sau **1 lần đầu tiên**:

1. **Tạo môi trường ảo `.venv` và cài thư viện từ `requirements.txt`:**
   - **macOS / Linux:**
     ```bash
     python3 -m venv .venv
     .venv/bin/pip install -r requirements.txt
     ```
   - **Windows (PowerShell / CMD):**
     ```powershell
     python -m venv .venv
     .venv\Scripts\pip install -r requirements.txt
     ```

2. **Tạo file cấu hình `.env` từ mẫu:**
   ```bash
   cp .env.example .env
   ```
   Sau đó mở `.env` và điền `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` cùng các thông số mong muốn.

> **Lưu ý khi phát triển thêm tính năng:** Mỗi khi cài thêm thư viện mới, hãy cập nhật vào `requirements.txt`. Các lần `git pull` sau trên máy khác không cần tạo lại `.venv`, chỉ cần chạy lại lệnh `pip install -r requirements.txt` nếu có thư viện mới.

## Cách chạy

1. **Chạy bot liên tục theo lịch:**
   - macOS / Linux: `.venv/bin/python main.py`
   - Windows: `.venv\Scripts\python main.py`

2. **Chạy thử 1 lần rồi thoát (phù hợp kiểm thử hoặc gắn cron):**
   ```bash
   .venv/bin/python main.py --once
   ```

3. **Chạy unit test:**
   ```bash
   .venv/bin/python -m unittest discover -s tests -v
   ```

## Chạy tự động mỗi tiếng với GitHub Actions

Repo đã có sẵn workflow [.github/workflows/run-bot.yml](.github/workflows/run-bot.yml) tự động chạy tất cả tính năng đang bật mỗi tiếng (`cron: "17 * * * *"`), lưu trạng thái chống spam qua GitHub Actions Cache và báo fail nếu có tính năng lỗi.

Để kích hoạt sau khi `git push`:
1. Vào **Settings → Secrets and variables → Actions** trên GitHub repo.
2. Trong tab **Secrets**, thêm các secret sau:
   - `TELEGRAM_TOKEN` *(bắt buộc)*
   - `TELEGRAM_CHAT_ID` *(bắt buộc)*
   - `GEMINI_API_KEY` *(khuyến nghị — lấy miễn phí tại [Google AI Studio](https://aistudio.google.com/apikey) để tóm tắt bằng AI; nếu bỏ trống bot sẽ tự chuyển sang tóm tắt nhanh từ RSS)*
3. (Tuỳ chọn) Trong tab **Variables**, thêm các biến cấu hình nếu muốn đổi so với mặc định:
   - `GOLD_TARGET_PRICE` (mặc định `13500000`)
   - `GOLD_ENABLED` (mặc định `true`)
   - `GOLD_URL` (mặc định `https://webgia.com/gia-vang/sjc/`)
   - `NEWS_ENABLED` (mặc định `true`)
   - `NEWS_RSS_URLS` (mặc định `https://vnexpress.net/rss/tin-noi-bat.rss,https://tuoitre.vn/rss/tin-moi-nhat.rss`)
   - `NEWS_SEND_HOUR` / `NEWS_SEND_MINUTE` (mặc định `7` / `0` theo `Asia/Ho_Chi_Minh`)
   - `GEMINI_MODEL` (mặc định `gemini-2.5-flash`)
   - `REMINDERS_ENABLED` (mặc định `true`)
   - `REMINDERS_SEND_HOUR` / `REMINDERS_SEND_MINUTE` (mặc định `7` / `0` theo `Asia/Ho_Chi_Minh`)
   - `REMINDERS_LEAD_DAYS` (mặc định `7,3,1,0` — nhắc trước 7, 3, 1 ngày và đúng ngày)
   - `REMINDERS_DATA` *(tự động tạo/cập nhật khi bạn thêm/xoá bằng lệnh `/remind_add`, `/remind_del`)*
   - `LOG_LEVEL` (mặc định `INFO`)
4. Vào tab **Actions → Run Telegram Bot Hourly → Run workflow** để chạy thử ngay.

## Các lệnh Telegram tương tác (Webhook Serverless trên Vercel / Render)

Bot hỗ trợ các lệnh phản hồi tức thì qua Webhook (Telegram chỉ cho phép dấu `_` trong tên lệnh, nhưng bot vẫn tự nhận nếu bạn gõ dấu `-`):
- `/start` hoặc `/help`: Xem hướng dẫn và danh sách lệnh.
- `/gold_check`: Kiểm tra giá vàng hiện tại ngay lập tức.
- `/gold_set <số tiền>`: Cập nhật `GOLD_TARGET_PRICE` lên **GitHub Repository Variable** (hỗ trợ cả `13450000` và `13.450.000`).
- `/gold_status`: Xem mức giá mục tiêu hiện tại đang đồng bộ.
- `/news_now`: Lấy và tóm tắt tin tức nổi bật ngay lập tức.
- `/news_status`: Xem giờ gửi bản tin sáng, danh sách RSS và trạng thái Gemini AI.
- `/remind_add <ngày> [giờ] <nội dung> [#yearly|#monthly|#weekly|#daily]`: Thêm nhắc việc hoặc sự kiện đếm ngược (hỗ trợ `25/12`, `25/12/2026`, `2026-12-25`, `homnay`, `ngaymai`, `+3d`, `+2w`, giờ `09:30` hoặc `9h30`).
- `/remind_list`: Xem toàn bộ danh sách nhắc việc & đếm ngược.
- `/countdown`: Xem nhanh đếm ngược các sự kiện sắp tới.
- `/remind_del <ID | expired>`: Xoá nhắc việc theo ID (hoặc `expired` để dọn toàn bộ mục đã qua hạn).
- `/remind_status`: Xem trạng thái tính năng nhắc việc & nguồn đồng bộ.

### Cách triển khai Webhook (khuyến nghị Vercel — miễn phí)

1. **Tạo GitHub Fine-grained Personal Access Token** (để `/gold_set` ghi được vào Repository Variable):
   - Vào GitHub → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
   - Chọn **Only select repositories** → chọn repo `telegram-bot`.
   - Mục **Repository permissions → Variables**: chọn **Read and write** → tạo token.

2. **Deploy lên Vercel** (hoặc **Render**):
   - **Vercel:** Import repo GitHub này lên [vercel.com](https://vercel.com) (Vercel tự nhận endpoint tại `https://<ten-app>.vercel.app/api/webhook`).
   - **Render:** Tạo Web Service chạy Python, Start Command: `python webhook_server.py` (endpoint tại `https://<ten-app>.onrender.com/api/webhook`).
   - Thêm các Environment Variables sau trên Vercel/Render:
     - `TELEGRAM_TOKEN`
     - `TELEGRAM_CHAT_ID`
     - `TELEGRAM_WEBHOOK_SECRET` *(chuỗi ngẫu nhiên tự đặt để bảo vệ endpoint)*
     - `GITHUB_TOKEN` *(Fine-grained PAT vừa tạo ở bước 1)*
     - `GITHUB_REPOSITORY` *(ví dụ: `ndtruongg/telegram-bot`)*

3. **Đăng ký Webhook & menu lệnh `/` lên Telegram** (chạy 1 lần duy nhất từ máy bạn sau khi điền `TELEGRAM_WEBHOOK_SECRET` vào `.env`):
   ```bash
   .venv/bin/python main.py --set-webhook https://<ten-app>.vercel.app/api/webhook
   ```
   *(Nếu chỉ muốn cập nhật lại menu lệnh `/` sau khi thêm tính năng mới: `.venv/bin/python main.py --sync-commands`.)*

## Cách thêm tính năng mới

Ví dụ muốn thêm tính năng theo dõi thời tiết (`weather`):

1. **Tạo thư mục mới** `bot/features/weather/`:
   - `client.py`: Gọi API hoặc cào dữ liệu thời tiết.
   - `feature.py`: Tạo class `WeatherFeature(BaseFeature)` cài đặt `name`, `interval_seconds` và `execute()`.
2. **Thêm cấu hình** (nếu cần) vào `bot/config.py`, `.env` và `.env.example`.
3. **Đăng ký tính năng** trong hàm `load_features()` ở `bot/features/__init__.py`.
4. **Viết test** trong thư mục `tests/`.
