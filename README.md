# Multi-Feature Telegram Bot

Bot Telegram được tổ chức theo kiến trúc module hóa, mỗi tính năng nằm độc lập trong `bot/features/` và chạy qua bộ điều phối chung (`BotScheduler`).

## Cấu trúc thư mục

```text
.
├── main.py                         # Điểm khởi chạy bot (hỗ trợ --once, --fail-on-error, --feature)
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
│   │   ├── telegram_client.py      # Gửi tin nhắn & nhận cập nhật Telegram
│   │   ├── scheduler.py            # Điều phối nhiều tính năng theo chu kỳ riêng
│   │   ├── state.py                # Lưu trạng thái JSON giữa các lần chạy (dùng cho CI/cron)
│   │   └── logger.py               # Cấu hình logging thống nhất
│   ├── features/                   # Các tính năng của bot (mỗi tính năng 1 thư mục)
│   │   ├── __init__.py             # Đăng ký danh sách tính năng được kích hoạt
│   │   └── gold_price/             # Tính năng theo dõi giá vàng SJC
│   │       ├── scraper.py          # Cào và phân tích dữ liệu HTML
│   │       └── feature.py          # Logic kiểm tra ngưỡng giá & gửi cảnh báo
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
2. Trong tab **Secrets**, thêm 2 secret bắt buộc:
   - `TELEGRAM_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. (Tuỳ chọn) Trong tab **Variables**, thêm các biến cấu hình nếu muốn đổi so với mặc định:
   - `GOLD_TARGET_PRICE` (mặc định `13500000`)
   - `GOLD_ENABLED` (mặc định `true`)
   - `GOLD_URL` (mặc định `https://webgia.com/gia-vang/sjc/`)
   - `LOG_LEVEL` (mặc định `INFO`)
4. Vào tab **Actions → Run Telegram Bot Hourly → Run workflow** để chạy thử ngay.

## Cách thêm tính năng mới

Ví dụ muốn thêm tính năng theo dõi thời tiết (`weather`):

1. **Tạo thư mục mới** `bot/features/weather/`:
   - `client.py`: Gọi API hoặc cào dữ liệu thời tiết.
   - `feature.py`: Tạo class `WeatherFeature(BaseFeature)` cài đặt `name`, `interval_seconds` và `execute()`.
2. **Thêm cấu hình** (nếu cần) vào `bot/config.py`, `.env` và `.env.example`.
3. **Đăng ký tính năng** trong hàm `load_features()` ở `bot/features/__init__.py`.
4. **Viết test** trong thư mục `tests/`.
