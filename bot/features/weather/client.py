"""Open-Meteo API client, Vietnamese geocoding helpers, and weather/AQI analysis."""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

logger = logging.getLogger(__name__)

GEOCODING_API_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_API_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_API_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
MET_NO_FORECAST_API_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"

DEFAULT_TIMEOUT = 10
DEFAULT_USER_AGENT = "telegram-bot-weather/1.0 (+https://github.com/ndtruongg/telegram-bot)"
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

# WMO Weather interpretation codes (WW) mapped to (emoji, Vietnamese description)
WMO_WEATHER_CODES: dict[int, tuple[str, str]] = {
    0: ("☀️", "Trời quang đãng"),
    1: ("🌤️", "Ít mây"),
    2: ("⛅", "Có mây"),
    3: ("☁️", "Nhiều mây"),
    45: ("🌫️", "Sương mù"),
    48: ("🌫️", "Sương mù dày"),
    51: ("🌦️", "Mưa phùn nhẹ"),
    53: ("🌦️", "Mưa phùn vừa"),
    55: ("🌧️", "Mưa phùn dày"),
    56: ("🌧️", "Mưa phùn lạnh nhẹ"),
    57: ("🌧️", "Mưa phùn lạnh dày"),
    61: ("🌦️", "Mưa nhẹ"),
    63: ("🌧️", "Mưa vừa"),
    65: ("🌧️", "Mưa to"),
    66: ("🌧️", "Mưa lạnh nhẹ"),
    67: ("🌧️", "Mưa lạnh to"),
    71: ("🌨️", "Tuyết nhẹ"),
    73: ("🌨️", "Tuyết vừa"),
    75: ("❄️", "Tuyết dày"),
    77: ("🌨️", "Mưa hạt tuyết"),
    80: ("🌦️", "Mưa rào nhẹ"),
    81: ("🌧️", "Mưa rào vừa"),
    82: ("⛈️", "Mưa rào rất to"),
    85: ("🌨️", "Mưa tuyết nhẹ"),
    86: ("❄️", "Mưa tuyết dày"),
    95: ("⛈️", "Mưa giông"),
    96: ("⛈️", "Giông kèm mưa đá nhẹ"),
    99: ("⛈️", "Giông kèm mưa đá lớn"),
}

RAIN_WEATHER_CODES: frozenset[int] = frozenset(
    {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
)
SEVERE_RAIN_CODES: frozenset[int] = frozenset({65, 82, 95, 96, 99})


@dataclass(frozen=True)
class GeoLocation:
    """Resolved geographic location for weather queries."""

    name: str
    latitude: float
    longitude: float
    country: str = ""
    admin1: str = ""
    timezone: str = "Asia/Ho_Chi_Minh"

    @property
    def display_name(self) -> str:
        parts = [self.name]
        if self.admin1 and self.admin1.lower() != self.name.lower():
            parts.append(self.admin1)
        if self.country:
            parts.append(self.country)
        return ", ".join(parts)


@dataclass(frozen=True)
class HourlyForecast:
    """Single-hour weather forecast snapshot."""

    time_iso: str
    temperature: float | None = None
    precipitation_probability: int | None = None
    precipitation: float | None = None
    rain: float | None = None
    weather_code: int | None = None
    uv_index: float | None = None

    @property
    def hour_label(self) -> str:
        # Format YYYY-MM-DDTHH:MM -> HH:MM
        if "T" in self.time_iso:
            return self.time_iso.split("T", maxsplit=1)[1][:5]
        return self.time_iso[-5:]

    @property
    def date_iso(self) -> str:
        if "T" in self.time_iso:
            return self.time_iso.split("T", maxsplit=1)[0]
        return self.time_iso[:10]


@dataclass(frozen=True)
class DailyForecast:
    """Single-day weather summary."""

    date_iso: str
    weather_code: int | None = None
    temp_max: float | None = None
    temp_min: float | None = None
    apparent_max: float | None = None
    apparent_min: float | None = None
    precipitation_sum: float | None = None
    precipitation_prob_max: int | None = None
    uv_index_max: float | None = None
    wind_speed_max: float | None = None
    sunrise: str = ""
    sunset: str = ""


@dataclass(frozen=True)
class AirQualityData:
    """Air quality metrics from Open-Meteo Air Quality API."""

    us_aqi: int | None = None
    european_aqi: int | None = None
    pm2_5: float | None = None
    pm10: float | None = None
    peak_us_aqi_today: int | None = None


@dataclass(frozen=True)
class WeatherReport:
    """Complete weather and air-quality report for a location."""

    location: GeoLocation
    current_time_iso: str
    timezone: str
    current_temp: float | None
    current_apparent_temp: float | None
    current_humidity: int | None
    current_wind_speed: float | None
    current_precipitation: float | None
    current_weather_code: int | None
    current_uv: float | None
    today: DailyForecast | None
    next_days: tuple[DailyForecast, ...]
    hourly: tuple[HourlyForecast, ...]
    air_quality: AirQualityData | None
    source: str = "Open-Meteo"


@dataclass(frozen=True)
class HeavyRainAlertInfo:
    """Details about impending heavy rain / thunderstorm."""

    max_precipitation_mm: float
    max_probability: int
    peak_hour_label: str
    weather_code: int | None
    rain_hours: tuple[HourlyForecast, ...]


@dataclass(frozen=True)
class BadAqiAlertInfo:
    """Details about unhealthy air quality."""

    us_aqi: int
    pm2_5: float | None
    pm10: float | None
    level_icon: str
    level_label: str
    advice: str


@dataclass(frozen=True)
class WeatherAlertCheck:
    """Result of checking severe rain and bad AQI thresholds."""

    heavy_rain: HeavyRainAlertInfo | None = None
    bad_aqi: BadAqiAlertInfo | None = None

    @property
    def has_alert(self) -> bool:
        return self.heavy_rain is not None or self.bad_aqi is not None


def _normalize_city_key(raw: str) -> str:
    """Lowercase, strip Vietnamese accents, and collapse punctuation/spaces."""
    text = raw.strip().replace("đ", "d").replace("Đ", "D")
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).strip()
    return cleaned


_PRESET_LOCATIONS: list[tuple[tuple[str, ...], GeoLocation]] = [
    (
        (
            "hcm",
            "tphcm",
            "tp hcm",
            "tp ho chi minh",
            "ho chi minh",
            "ho chi minh city",
            "saigon",
            "sai gon",
            "sg",
        ),
        GeoLocation("TP. Hồ Chí Minh", 10.8231, 106.6297, "Việt Nam", "", "Asia/Ho_Chi_Minh"),
    ),
    (
        ("hn", "hanoi", "ha noi", "tp ha noi", "thu do ha noi"),
        GeoLocation("Hà Nội", 21.0285, 105.8542, "Việt Nam", "", "Asia/Ho_Chi_Minh"),
    ),
    (
        ("dn", "danang", "da nang", "tp da nang"),
        GeoLocation("Đà Nẵng", 16.0544, 108.2022, "Việt Nam", "", "Asia/Ho_Chi_Minh"),
    ),
    (
        ("hp", "haiphong", "hai phong", "tp hai phong"),
        GeoLocation("Hải Phòng", 20.8449, 106.6881, "Việt Nam", "", "Asia/Ho_Chi_Minh"),
    ),
    (
        ("ct", "cantho", "can tho", "tp can tho"),
        GeoLocation("Cần Thơ", 10.0452, 105.7469, "Việt Nam", "", "Asia/Ho_Chi_Minh"),
    ),
    (
        ("hue", "tp hue", "thua thien hue"),
        GeoLocation("Huế", 16.4637, 107.5909, "Việt Nam", "", "Asia/Ho_Chi_Minh"),
    ),
    (
        ("nhatrang", "nha trang", "khanh hoa"),
        GeoLocation("Nha Trang", 12.2388, 109.1967, "Việt Nam", "Khánh Hòa", "Asia/Ho_Chi_Minh"),
    ),
    (
        ("dalat", "da lat", "lam dong"),
        GeoLocation("Đà Lạt", 11.9404, 108.4583, "Việt Nam", "Lâm Đồng", "Asia/Ho_Chi_Minh"),
    ),
    (
        ("vungtau", "vung tau", "ba ria vung tau"),
        GeoLocation("Vũng Tàu", 10.3460, 107.0843, "Việt Nam", "Bà Rịa - Vũng Tàu", "Asia/Ho_Chi_Minh"),
    ),
    (
        ("quynhon", "quy nhon", "binh dinh"),
        GeoLocation("Quy Nhơn", 13.7830, 109.2197, "Việt Nam", "Bình Định", "Asia/Ho_Chi_Minh"),
    ),
    (
        ("phuquoc", "phu quoc"),
        GeoLocation("Phú Quốc", 10.2899, 103.9840, "Việt Nam", "Kiên Giang", "Asia/Ho_Chi_Minh"),
    ),
    (
        ("bienhoa", "bien hoa", "dong nai"),
        GeoLocation("Biên Hòa", 10.9465, 106.8340, "Việt Nam", "Đồng Nai", "Asia/Ho_Chi_Minh"),
    ),
    (
        ("binhduong", "binh duong", "thu dau mot"),
        GeoLocation("Thủ Dầu Một", 10.9804, 106.6519, "Việt Nam", "Bình Dương", "Asia/Ho_Chi_Minh"),
    ),
    (
        ("vinh", "nghe an"),
        GeoLocation("Vinh", 18.6796, 105.6813, "Việt Nam", "Nghệ An", "Asia/Ho_Chi_Minh"),
    ),
    (
        ("bacninh", "bac ninh"),
        GeoLocation("Bắc Ninh", 21.1861, 106.0763, "Việt Nam", "", "Asia/Ho_Chi_Minh"),
    ),
    (
        ("halong", "ha long", "quang ninh"),
        GeoLocation("Hạ Long", 20.9599, 107.0425, "Việt Nam", "Quảng Ninh", "Asia/Ho_Chi_Minh"),
    ),
    (
        ("buonmathuot", "buon ma thuot", "bmt", "dak lak"),
        GeoLocation("Buôn Ma Thuột", 12.6667, 108.0500, "Việt Nam", "Đắk Lắk", "Asia/Ho_Chi_Minh"),
    ),
    (
        ("thainguyen", "thai nguyen"),
        GeoLocation("Thái Nguyên", 21.5672, 105.8252, "Việt Nam", "", "Asia/Ho_Chi_Minh"),
    ),
]

VIETNAM_CITY_PRESETS: dict[str, GeoLocation] = {
    alias: loc for aliases, loc in _PRESET_LOCATIONS for alias in aliases
}


def describe_weather_code(code: int | None) -> tuple[str, str]:
    """Return (emoji, Vietnamese description) for a WMO weather code."""
    if code is None:
        return ("🌡️", "Không rõ")
    return WMO_WEATHER_CODES.get(int(code), ("🌡️", f"Thời tiết (mã {code})"))


def classify_uv(uv: float | None) -> tuple[str, str, str]:
    """Classify UV index into (emoji, Vietnamese label, short advice)."""
    if uv is None:
        return ("⚪", "Không rõ", "")
    if uv < 3.0:
        return ("🟢", "Thấp", "An toàn khi ra ngoài")
    if uv < 6.0:
        return ("🟡", "Trung bình", "Nên đeo kính râm nếu ở ngoài lâu")
    if uv < 8.0:
        return ("🟠", "Cao", "Nên đội mũ, đeo kính râm và dùng kem chống nắng")
    if uv < 11.0:
        return ("🔴", "Rất cao", "Che chắn kỹ, hạn chế ra nắng 10h–16h")
    return ("🟣", "Cực cao", "Tránh tiếp xúc trực tiếp với ánh nắng")


def classify_aqi(us_aqi: int | None) -> tuple[str, str, str]:
    """Classify US AQI into (emoji, Vietnamese label, health advice)."""
    if us_aqi is None:
        return ("⚪", "Không rõ", "")
    if us_aqi <= 50:
        return ("🟢", "Tốt", "Không khí trong lành, thoải mái hoạt động ngoài trời")
    if us_aqi <= 100:
        return ("🟡", "Trung bình", "Chấp nhận được, người nhạy cảm nên lưu ý")
    if us_aqi <= 150:
        return ("🟠", "Kém (nhạy cảm)", "Người nhạy cảm nên đeo khẩu trang, hạn chế ra ngoài lâu")
    if us_aqi <= 200:
        return ("🔴", "Xấu", "Nên đeo khẩu trang lọc bụi mịn PM2.5, hạn chế hoạt động ngoài trời")
    if us_aqi <= 300:
        return ("🟣", "Rất xấu", "Tránh ra ngoài nếu không cần thiết, đóng kín cửa sổ")
    return ("🟤", "Nguy hại", "Cảnh báo sức khỏe khẩn cấp — nên ở trong nhà")


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _at_index(values: Any, idx: int) -> Any:
    if isinstance(values, list) and 0 <= idx < len(values):
        return values[idx]
    return None


def _format_hour_ranges(hours: list[HourlyForecast]) -> str:
    """Summarize rainy hourly slots into compact readable windows (e.g. 14:00–17:00)."""
    if not hours:
        return ""
    parsed_hours: list[int] = []
    for item in hours:
        label = item.hour_label
        if len(label) >= 2 and label[:2].isdigit():
            parsed_hours.append(int(label[:2]))
    if not parsed_hours:
        return ", ".join(h.hour_label for h in hours[:4])

    unique_sorted = sorted(set(parsed_hours))
    ranges: list[tuple[int, int]] = []
    start = prev = unique_sorted[0]
    for h in unique_sorted[1:]:
        if h == prev + 1:
            prev = h
        else:
            ranges.append((start, prev))
            start = prev = h
    ranges.append((start, prev))

    formatted: list[str] = []
    for s, e in ranges[:3]:
        if s == e:
            formatted.append(f"{s:02d}:00")
        else:
            end_h = (e + 1) % 24
            formatted.append(f"{s:02d}:00–{end_h:02d}:00")
    return ", ".join(formatted)


def get_today_hourly(report: WeatherReport) -> list[HourlyForecast]:
    """Return hourly forecast entries belonging to the report's current date."""
    today_date = (
        report.today.date_iso
        if report.today is not None
        else report.current_time_iso[:10]
    )
    return [h for h in report.hourly if h.date_iso == today_date]


def get_upcoming_hourly(report: WeatherReport, hours_ahead: int = 3) -> list[HourlyForecast]:
    """Return hourly forecast entries starting from the current hour up to ``hours_ahead``."""
    if not report.hourly or hours_ahead <= 0:
        return []
    current_hour_prefix = report.current_time_iso[:13]  # YYYY-MM-DDTHH
    upcoming: list[HourlyForecast] = []
    for item in report.hourly:
        if item.time_iso[:13] >= current_hour_prefix:
            upcoming.append(item)
            if len(upcoming) >= hours_ahead:
                break
    return upcoming


def build_raincoat_advice(report: WeatherReport) -> tuple[str, str]:
    """Generate practical Vietnamese raincoat/umbrella advice for today."""
    today = report.today
    today_hours = get_today_hourly(report)

    max_prob = today.precipitation_prob_max if today and today.precipitation_prob_max is not None else 0
    precip_sum = today.precipitation_sum if today and today.precipitation_sum is not None else 0.0
    daily_code = today.weather_code if today is not None else report.current_weather_code

    hourly_max_prob = max(
        (h.precipitation_probability or 0 for h in today_hours),
        default=0,
    )
    max_prob = max(max_prob, hourly_max_prob)

    rainy_hours = [
        h
        for h in today_hours
        if (h.precipitation_probability or 0) >= 45
        or (h.precipitation or 0.0) >= 0.5
        or (h.weather_code in RAIN_WEATHER_CODES)
    ]
    severe_today = (daily_code in SEVERE_RAIN_CODES) or any(
        (h.weather_code in SEVERE_RAIN_CODES) or (h.precipitation or 0.0) >= 5.0
        for h in today_hours
    )

    window_text = _format_hour_ranges(rainy_hours)
    window_suffix = f" (dễ mưa lúc {window_text})" if window_text else ""

    if severe_today or max_prob >= 70 or precip_sum >= 10.0:
        return (
            "☔",
            f"<b>Nên mang áo mưa / ô!</b> Khả năng mưa cao ({max_prob}%, tổng ~{precip_sum:.1f} mm){window_suffix}.",
        )
    if max_prob >= 45 or precip_sum >= 2.0 or (daily_code in RAIN_WEATHER_CODES):
        return (
            "🌂",
            f"<b>Nên mang dự phòng áo mưa hoặc ô</b> — có khả năng mưa ({max_prob}%){window_suffix}.",
        )
    if max_prob >= 25 or precip_sum >= 0.5:
        return (
            "🌦️",
            f"Khả năng mưa thấp ({max_prob}%){window_suffix}, mang ô gọn nếu đi xa.",
        )
    return (
        "☀️",
        f"<b>Không cần mang áo mưa</b> — trời khá khô ráo (xác suất mưa tối đa {max_prob}%).",
    )


def detect_weather_alerts(
    report: WeatherReport,
    *,
    rain_lookahead_hours: int = 3,
    rain_alert_mm: float = 5.0,
    rain_alert_prob: int = 75,
    aqi_threshold: int = 151,
) -> WeatherAlertCheck:
    """Inspect a WeatherReport for upcoming heavy rain or unhealthy AQI."""
    upcoming = get_upcoming_hourly(report, hours_ahead=rain_lookahead_hours)

    heavy_hours: list[HourlyForecast] = []
    for item in upcoming:
        precip = item.precipitation if item.precipitation is not None else (item.rain or 0.0)
        prob = item.precipitation_probability or 0
        code = item.weather_code

        is_heavy_mm = precip >= rain_alert_mm
        is_likely_moderate = prob >= rain_alert_prob and precip >= max(2.0, rain_alert_mm * 0.4)
        is_severe_code = code in SEVERE_RAIN_CODES and (prob >= 50 or precip > 0.0)

        if is_heavy_mm or is_likely_moderate or is_severe_code:
            heavy_hours.append(item)

    heavy_rain_info: HeavyRainAlertInfo | None = None
    if heavy_hours:
        peak_item = max(
            heavy_hours,
            key=lambda h: (
                h.precipitation if h.precipitation is not None else (h.rain or 0.0),
                h.precipitation_probability or 0,
            ),
        )
        max_mm = max(
            (h.precipitation if h.precipitation is not None else (h.rain or 0.0))
            for h in heavy_hours
        )
        max_prob = max((h.precipitation_probability or 0) for h in heavy_hours)
        heavy_rain_info = HeavyRainAlertInfo(
            max_precipitation_mm=max_mm,
            max_probability=max_prob,
            peak_hour_label=peak_item.hour_label,
            weather_code=peak_item.weather_code,
            rain_hours=tuple(heavy_hours),
        )

    bad_aqi_info: BadAqiAlertInfo | None = None
    aq = report.air_quality
    if aq is not None and aq.us_aqi is not None and aq.us_aqi >= aqi_threshold:
        icon, label, advice = classify_aqi(aq.us_aqi)
        bad_aqi_info = BadAqiAlertInfo(
            us_aqi=aq.us_aqi,
            pm2_5=aq.pm2_5,
            pm10=aq.pm10,
            level_icon=icon,
            level_label=label,
            advice=advice,
        )

    return WeatherAlertCheck(heavy_rain=heavy_rain_info, bad_aqi=bad_aqi_info)


def geocode_city(
    query: str,
    *,
    session: requests.Session | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> GeoLocation | None:
    """Resolve a city name to geographic coordinates via local Vietnam presets or Open-Meteo Geocoding."""
    cleaned = query.strip()
    if not cleaned:
        return None

    normalized = _normalize_city_key(cleaned)
    if normalized in VIETNAM_CITY_PRESETS:
        return VIETNAM_CITY_PRESETS[normalized]

    http = session or requests.Session()
    candidates = [cleaned]
    if normalized and normalized != cleaned.lower():
        candidates.append(normalized)

    for term in candidates:
        try:
            resp = http.get(
                GEOCODING_API_URL,
                params={
                    "name": term,
                    "count": 5,
                    "language": "vi",
                    "format": "json",
                },
                headers={"User-Agent": DEFAULT_USER_AGENT},
                timeout=timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.error("Lỗi geocoding thành phố '%s': %s", term, exc)
            return None

        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or not results:
            continue

        # Prefer Vietnamese match if present, otherwise pick the top Open-Meteo match
        chosen = next(
            (r for r in results if str(r.get("country_code", "")).upper() == "VN"),
            results[0],
        )
        lat = _safe_float(chosen.get("latitude"))
        lon = _safe_float(chosen.get("longitude"))
        if lat is None or lon is None:
            continue

        return GeoLocation(
            name=str(chosen.get("name") or cleaned),
            latitude=lat,
            longitude=lon,
            country=str(chosen.get("country") or ""),
            admin1=str(chosen.get("admin1") or ""),
            timezone=str(chosen.get("timezone") or "Asia/Ho_Chi_Minh"),
        )

    return None


def _parse_forecast_payload(
    location: GeoLocation,
    forecast_data: dict[str, Any],
    aq_data: dict[str, Any] | None = None,
) -> WeatherReport:
    """Parse raw Open-Meteo forecast and air-quality JSON payloads into a WeatherReport."""
    timezone_str = str(forecast_data.get("timezone") or location.timezone or "Asia/Ho_Chi_Minh")
    current = forecast_data.get("current") or {}
    current_time_iso = str(current.get("time") or "")

    # Parse hourly forecast
    hourly_block = forecast_data.get("hourly") or {}
    hourly_times = hourly_block.get("time") or []
    hourly_list: list[HourlyForecast] = []
    for idx, t_iso in enumerate(hourly_times):
        hourly_list.append(
            HourlyForecast(
                time_iso=str(t_iso),
                temperature=_safe_float(_at_index(hourly_block.get("temperature_2m"), idx)),
                precipitation_probability=_safe_int(
                    _at_index(hourly_block.get("precipitation_probability"), idx)
                ),
                precipitation=_safe_float(_at_index(hourly_block.get("precipitation"), idx)),
                rain=_safe_float(_at_index(hourly_block.get("rain"), idx)),
                weather_code=_safe_int(_at_index(hourly_block.get("weather_code"), idx)),
                uv_index=_safe_float(_at_index(hourly_block.get("uv_index"), idx)),
            )
        )

    # Parse daily forecast
    daily_block = forecast_data.get("daily") or {}
    daily_times = daily_block.get("time") or []
    daily_list: list[DailyForecast] = []
    for idx, d_iso in enumerate(daily_times):
        sunrise_raw = str(_at_index(daily_block.get("sunrise"), idx) or "")
        sunset_raw = str(_at_index(daily_block.get("sunset"), idx) or "")
        daily_list.append(
            DailyForecast(
                date_iso=str(d_iso),
                weather_code=_safe_int(_at_index(daily_block.get("weather_code"), idx)),
                temp_max=_safe_float(_at_index(daily_block.get("temperature_2m_max"), idx)),
                temp_min=_safe_float(_at_index(daily_block.get("temperature_2m_min"), idx)),
                apparent_max=_safe_float(_at_index(daily_block.get("apparent_temperature_max"), idx)),
                apparent_min=_safe_float(_at_index(daily_block.get("apparent_temperature_min"), idx)),
                precipitation_sum=_safe_float(_at_index(daily_block.get("precipitation_sum"), idx)),
                precipitation_prob_max=_safe_int(
                    _at_index(daily_block.get("precipitation_probability_max"), idx)
                ),
                uv_index_max=_safe_float(_at_index(daily_block.get("uv_index_max"), idx)),
                wind_speed_max=_safe_float(_at_index(daily_block.get("wind_speed_10m_max"), idx)),
                sunrise=sunrise_raw.split("T", maxsplit=1)[1][:5] if "T" in sunrise_raw else sunrise_raw,
                sunset=sunset_raw.split("T", maxsplit=1)[1][:5] if "T" in sunset_raw else sunset_raw,
            )
        )

    today = daily_list[0] if daily_list else None
    next_days = tuple(daily_list[1:])

    # Determine current UV from AQ payload or matching hourly slot
    current_uv: float | None = None
    air_quality: AirQualityData | None = None
    if isinstance(aq_data, dict):
        aq_current = aq_data.get("current") or {}
        current_uv = _safe_float(aq_current.get("uv_index"))
        us_aqi = _safe_int(aq_current.get("us_aqi"))
        eu_aqi = _safe_int(aq_current.get("european_aqi"))
        pm2_5 = _safe_float(aq_current.get("pm2_5"))
        pm10 = _safe_float(aq_current.get("pm10"))

        peak_aqi_today: int | None = None
        aq_hourly = aq_data.get("hourly") or {}
        aq_times = aq_hourly.get("time") or []
        aq_us_list = aq_hourly.get("us_aqi") or []
        today_date = today.date_iso if today is not None else current_time_iso[:10]
        today_aqis = [
            val
            for t_str, raw_val in zip(aq_times, aq_us_list)
            if str(t_str).startswith(today_date)
            for val in [_safe_int(raw_val)]
            if val is not None
        ]
        if today_aqis:
            peak_aqi_today = max(today_aqis)
        if us_aqi is None and today_aqis:
            us_aqi = today_aqis[0]

        if any(v is not None for v in (us_aqi, eu_aqi, pm2_5, pm10, peak_aqi_today)):
            air_quality = AirQualityData(
                us_aqi=us_aqi,
                european_aqi=eu_aqi,
                pm2_5=pm2_5,
                pm10=pm10,
                peak_us_aqi_today=peak_aqi_today,
            )

    if current_uv is None and current_time_iso:
        current_hour_prefix = current_time_iso[:13]
        for h in hourly_list:
            if h.time_iso[:13] == current_hour_prefix and h.uv_index is not None:
                current_uv = h.uv_index
                break

    return WeatherReport(
        location=location,
        current_time_iso=current_time_iso,
        timezone=timezone_str,
        current_temp=_safe_float(current.get("temperature_2m")),
        current_apparent_temp=_safe_float(current.get("apparent_temperature")),
        current_humidity=_safe_int(current.get("relative_humidity_2m")),
        current_wind_speed=_safe_float(current.get("wind_speed_10m")),
        current_precipitation=_safe_float(current.get("precipitation")),
        current_weather_code=_safe_int(current.get("weather_code")),
        current_uv=current_uv,
        today=today,
        next_days=next_days,
        hourly=tuple(hourly_list),
        air_quality=air_quality,
    )


def _extract_error_reason(resp: requests.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("reason"):
            return str(body["reason"])
    except Exception:
        pass
    text = getattr(resp, "text", "")
    if isinstance(text, str) and text.strip():
        return text.strip()[:160]
    return f"HTTP {getattr(resp, 'status_code', 'error')}"


def _request_json(
    http: requests.Session,
    url: str,
    *,
    params: dict[str, Any],
    timeout: int,
    retries: int = 0,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Perform a GET request returning a JSON object, retrying transient errors."""
    req_headers = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        req_headers.update(headers)

    last_exc: Exception | None = None
    attempts = max(1, retries + 1)
    for attempt in range(attempts):
        try:
            resp = http.get(url, params=params, headers=req_headers, timeout=timeout)
            status = getattr(resp, "status_code", 200)
            if isinstance(status, int) and status in _RETRYABLE_STATUS_CODES and attempt + 1 < attempts:
                logger.warning(
                    "API %s trả về HTTP %s (lần %d/%d), thử lại...",
                    url,
                    status,
                    attempt + 1,
                    attempts,
                )
                time.sleep(0.5 * (attempt + 1))
                continue
            if isinstance(status, int) and status >= 400:
                reason = _extract_error_reason(resp)
                raise requests.HTTPError(f"HTTP {status}: {reason}", response=resp)
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, dict):
                raise ValueError("Phản hồi JSON không phải đối tượng hợp lệ.")
            if payload.get("error") is True:
                raise RuntimeError(str(payload.get("reason") or "API trả về lỗi."))
            return payload
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_exc = exc
            if attempt + 1 < attempts and isinstance(
                exc, (requests.ConnectionError, requests.Timeout)
            ):
                logger.warning(
                    "Lỗi kết nối %s (%s), thử lại lần %d/%d...",
                    url,
                    exc,
                    attempt + 1,
                    attempts,
                )
                time.sleep(0.5 * (attempt + 1))
                continue
            break

    assert last_exc is not None
    raise last_exc


def _met_symbol_to_wmo(symbol_code: str | None) -> int | None:
    """Convert a MET Norway symbol_code string into an approximate WMO weather code."""
    if not symbol_code:
        return None
    base = symbol_code.split("_", maxsplit=1)[0].lower()
    if "thunder" in base:
        return 95
    if "snow" in base or "sleet" in base:
        return 73 if "heavy" not in base else 75
    if "heavyrainshowers" in base:
        return 82
    if "lightrainshowers" in base:
        return 80
    if "rainshowers" in base:
        return 81
    if "heavyrain" in base:
        return 65
    if "lightrain" in base:
        return 61
    if "rain" in base:
        return 63
    if "fog" in base:
        return 45
    if base == "clearsky":
        return 0
    if base == "fair":
        return 1
    if base == "partlycloudy":
        return 2
    if base == "cloudy":
        return 3
    return None


def _estimate_rain_prob_from_met(precip_mm: float | None, wmo_code: int | None) -> int:
    mm = precip_mm or 0.0
    if mm >= 5.0 or wmo_code in SEVERE_RAIN_CODES:
        return 90
    if mm >= 2.0:
        return 80
    if mm >= 0.5:
        return 65
    if mm > 0.0 or wmo_code in RAIN_WEATHER_CODES:
        return 45
    return 10


def _parse_met_no_payload(
    location: GeoLocation,
    met_data: dict[str, Any],
    aq_data: dict[str, Any] | None = None,
) -> WeatherReport:
    """Parse MET Norway locationforecast/2.0/compact JSON into a WeatherReport."""
    tz_name = location.timezone or "Asia/Ho_Chi_Minh"
    try:
        local_tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz_name = "Asia/Ho_Chi_Minh"
        local_tz = ZoneInfo(tz_name)

    timeseries = ((met_data.get("properties") or {}).get("timeseries")) or []
    if not isinstance(timeseries, list) or not timeseries:
        raise RuntimeError("Dữ liệu MET Norway trống.")

    hourly_list: list[HourlyForecast] = []
    daily_buckets: dict[str, list[HourlyForecast]] = {}
    first_details: dict[str, Any] = {}
    first_time_iso = ""
    first_code: int | None = None
    first_precip: float | None = None

    for idx, entry in enumerate(timeseries[:72]):
        raw_time = str(entry.get("time") or "")
        if not raw_time:
            continue
        try:
            utc_dt = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            if utc_dt.tzinfo is None:
                utc_dt = utc_dt.replace(tzinfo=dt_timezone.utc)
            local_dt = utc_dt.astimezone(local_tz)
        except ValueError:
            continue

        time_iso = local_dt.strftime("%Y-%m-%dT%H:%M")
        data = entry.get("data") or {}
        instant = (data.get("instant") or {}).get("details") or {}
        next_1h = data.get("next_1_hours") or {}
        next_6h = data.get("next_6_hours") or {}

        symbol = (
            (next_1h.get("summary") or {}).get("symbol_code")
            or (next_6h.get("summary") or {}).get("symbol_code")
        )
        wmo = _met_symbol_to_wmo(str(symbol) if symbol else None)
        precip_1h = _safe_float((next_1h.get("details") or {}).get("precipitation_amount"))
        if precip_1h is None:
            precip_6h = _safe_float((next_6h.get("details") or {}).get("precipitation_amount"))
            precip_1h = round(precip_6h / 6.0, 2) if precip_6h is not None else 0.0

        temp = _safe_float(instant.get("air_temperature"))
        prob = _estimate_rain_prob_from_met(precip_1h, wmo)

        hf = HourlyForecast(
            time_iso=time_iso,
            temperature=temp,
            precipitation_probability=prob,
            precipitation=precip_1h,
            rain=precip_1h,
            weather_code=wmo,
            uv_index=None,
        )
        hourly_list.append(hf)
        daily_buckets.setdefault(hf.date_iso, []).append(hf)

        if idx == 0:
            first_details = instant
            first_time_iso = time_iso
            first_code = wmo
            first_precip = precip_1h

    daily_list: list[DailyForecast] = []
    for d_iso in sorted(daily_buckets.keys())[:3]:
        items = daily_buckets[d_iso]
        temps = [h.temperature for h in items if h.temperature is not None]
        probs = [h.precipitation_probability for h in items if h.precipitation_probability is not None]
        precip_sum = round(sum((h.precipitation or 0.0) for h in items), 1)
        codes = [h.weather_code for h in items if h.weather_code is not None]
        chosen_code = max(
            codes,
            key=lambda c: (2 if c in SEVERE_RAIN_CODES else (1 if c in RAIN_WEATHER_CODES else 0), c),
        ) if codes else None
        daily_list.append(
            DailyForecast(
                date_iso=d_iso,
                weather_code=chosen_code,
                temp_max=max(temps) if temps else None,
                temp_min=min(temps) if temps else None,
                precipitation_sum=precip_sum,
                precipitation_prob_max=max(probs) if probs else None,
                uv_index_max=None,
            )
        )

    wind_ms = _safe_float(first_details.get("wind_speed"))
    wind_kmh = round(wind_ms * 3.6, 1) if wind_ms is not None else None

    today = daily_list[0] if daily_list else None
    air_quality: AirQualityData | None = None
    current_uv: float | None = None
    if isinstance(aq_data, dict):
        parsed_aq = _parse_forecast_payload(location, {"timezone": tz_name}, aq_data)
        air_quality = parsed_aq.air_quality
        current_uv = parsed_aq.current_uv

    return WeatherReport(
        location=location,
        current_time_iso=first_time_iso,
        timezone=tz_name,
        current_temp=_safe_float(first_details.get("air_temperature")),
        current_apparent_temp=None,
        current_humidity=_safe_int(first_details.get("relative_humidity")),
        current_wind_speed=wind_kmh,
        current_precipitation=first_precip,
        current_weather_code=first_code,
        current_uv=current_uv,
        today=today,
        next_days=tuple(daily_list[1:]),
        hourly=tuple(hourly_list),
        air_quality=air_quality,
        source="MET Norway (dự phòng Open-Meteo)",
    )


def fetch_weather_report(
    city: str,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    timezone: str = "auto",
    session: requests.Session | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> WeatherReport:
    """Fetch weather forecast and air quality from Open-Meteo (with MET Norway fallback)."""
    http = session or requests.Session()

    if latitude is not None and longitude is not None:
        location = GeoLocation(
            name=city.strip() or f"{latitude:.2f}, {longitude:.2f}",
            latitude=latitude,
            longitude=longitude,
            timezone=timezone if timezone != "auto" else "Asia/Ho_Chi_Minh",
        )
    else:
        resolved = geocode_city(city, session=http, timeout=timeout)
        if resolved is None:
            raise LookupError(f"Không tìm thấy thành phố '{city}'.")
        location = resolved

    forecast_params = {
        "latitude": location.latitude,
        "longitude": location.longitude,
        "timezone": timezone or "auto",
        "forecast_days": 3,
        "current": (
            "temperature_2m,relative_humidity_2m,apparent_temperature,"
            "precipitation,rain,weather_code,wind_speed_10m"
        ),
        "hourly": (
            "temperature_2m,precipitation_probability,precipitation,"
            "rain,weather_code,uv_index"
        ),
        "daily": (
            "weather_code,temperature_2m_max,temperature_2m_min,"
            "apparent_temperature_max,apparent_temperature_min,"
            "precipitation_sum,precipitation_probability_max,"
            "uv_index_max,wind_speed_10m_max,sunrise,sunset"
        ),
    }

    forecast_data: dict[str, Any] | None = None
    open_meteo_err: Exception | None = None
    try:
        forecast_data = _request_json(
            http,
            FORECAST_API_URL,
            params=forecast_params,
            timeout=timeout,
            retries=1,
        )
    except Exception as exc:
        open_meteo_err = exc
        logger.warning(
            "Open-Meteo Forecast lỗi (%s), chuyển sang nguồn dự phòng MET Norway...",
            exc,
        )

    aq_data: dict[str, Any] | None = None
    aq_params = {
        "latitude": location.latitude,
        "longitude": location.longitude,
        "timezone": timezone or "auto",
        "forecast_days": 2,
        "current": "us_aqi,european_aqi,pm2_5,pm10,uv_index",
        "hourly": "us_aqi,pm2_5,pm10,uv_index",
    }
    try:
        aq_data = _request_json(
            http,
            AIR_QUALITY_API_URL,
            params=aq_params,
            timeout=timeout,
            retries=0,
        )
    except Exception as exc:
        logger.warning("Không thể tải dữ liệu AQI từ Open-Meteo (%s), tiếp tục không có AQI.", exc)

    if forecast_data is not None:
        return _parse_forecast_payload(location, forecast_data, aq_data)

    # Fallback to MET Norway (free, no API key, friendly to shared cloud IPs)
    try:
        met_data = _request_json(
            http,
            MET_NO_FORECAST_API_URL,
            params={
                "lat": round(location.latitude, 4),
                "lon": round(location.longitude, 4),
            },
            timeout=timeout,
            retries=0,
        )
        return _parse_met_no_payload(location, met_data, aq_data)
    except Exception as fallback_exc:
        raise RuntimeError(
            f"Không thể tải dữ liệu thời tiết Open-Meteo ({open_meteo_err}) "
            f"và nguồn dự phòng MET Norway ({fallback_exc})"
        ) from open_meteo_err
