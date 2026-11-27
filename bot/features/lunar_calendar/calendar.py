"""Pure-Python Vietnamese astronomical lunar calendar engine (UTC+7).

Implements Hồ Ngọc Đức's standard Vietnamese lunar calendar algorithm based on
Jean Meeus' Astronomical Algorithms, plus Can Chi, 24 Solar Terms (Tiết khí),
Hoàng Đạo / Hắc Đạo days, 12 Trực, and Giờ Hoàng Đạo.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

VIETNAM_TZ_OFFSET = 7.0

CAN: tuple[str, ...] = (
    "Giáp",
    "Ất",
    "Bính",
    "Đinh",
    "Mậu",
    "Kỷ",
    "Canh",
    "Tân",
    "Nhâm",
    "Quý",
)

CHI: tuple[str, ...] = (
    "Tý",
    "Sửu",
    "Dần",
    "Mão",
    "Thìn",
    "Tỵ",
    "Ngọ",
    "Mùi",
    "Thân",
    "Dậu",
    "Tuất",
    "Hợi",
)

CHI_HOUR_RANGES: tuple[str, ...] = (
    "23-1h",
    "1-3h",
    "3-5h",
    "5-7h",
    "7-9h",
    "9-11h",
    "11-13h",
    "13-15h",
    "15-17h",
    "17-19h",
    "19-21h",
    "21-23h",
)

# 12 Thần (Hoàng Đạo / Hắc Đạo) in cycle order from each lunar month's Thanh Long Chi
HOANG_DAO_STARS: tuple[tuple[str, bool], ...] = (
    ("Thanh Long", True),
    ("Minh Đường", True),
    ("Thiên Hình", False),
    ("Chu Tước", False),
    ("Kim Quỹ", True),
    ("Kim Đường", True),
    ("Bạch Hổ", False),
    ("Ngọc Đường", True),
    ("Thiên Lao", False),
    ("Nguyên Vũ", False),
    ("Tư Mệnh", True),
    ("Câu Trận", False),
)

_HOANG_DAO_OFFSETS: tuple[int, ...] = (0, 1, 4, 5, 7, 10)

# 12 Trực (Kiến Trừ) with concise Vietnamese meaning
TRUC_INFO: tuple[tuple[str, str], ...] = (
    ("Kiến", "Tốt cho khởi đầu, xuất hành"),
    ("Trừ", "Tốt cho dọn dẹp, giải trừ, khám chữa"),
    ("Mãn", "Tốt cho cầu tài, khai trương, cưới hỏi"),
    ("Bình", "Bình hoà, phù hợp việc thường nhật"),
    ("Định", "Tốt cho ký kết, giao dịch, cưới hỏi"),
    ("Chấp", "Tốt cho khởi công, xây dựng, nhận việc"),
    ("Phá", "Nên thận trọng việc lớn, hợp phá dỡ"),
    ("Nguy", "Không thuận việc lớn, nên cẩn trọng"),
    ("Thành", "Rất tốt cho khai trương, ký kết, cưới hỏi"),
    ("Thu", "Tốt cho thu hoạch, thu tiền, nhận bàn giao"),
    ("Khai", "Rất tốt cho khởi sự, xuất hành, khai trương"),
    ("Bế", "Nên nghỉ ngơi, tránh khởi công hay ký kết lớn"),
)

# 24 Tiết khí indexed by floor(sun_longitude_deg / 15), starting at 0° = Xuân Phân
SOLAR_TERMS_VI: tuple[str, ...] = (
    "Xuân Phân",
    "Thanh Minh",
    "Cốc Vũ",
    "Lập Hạ",
    "Tiểu Mãn",
    "Mang Chủng",
    "Hạ Chí",
    "Tiểu Thử",
    "Đại Thử",
    "Lập Thu",
    "Xử Thử",
    "Bạch Lộ",
    "Thu Phân",
    "Hàn Lộ",
    "Sương Giáng",
    "Lập Đông",
    "Tiểu Tuyết",
    "Đại Tuyết",
    "Đông Chí",
    "Tiểu Hàn",
    "Đại Hàn",
    "Lập Xuân",
    "Vũ Thủy",
    "Kinh Trập",
)

WEEKDAYS_VI: tuple[str, ...] = (
    "Thứ 2",
    "Thứ 3",
    "Thứ 4",
    "Thứ 5",
    "Thứ 6",
    "Thứ 7",
    "Chủ nhật",
)


@dataclass(frozen=True)
class LunarDate:
    """Vietnamese lunar calendar date."""

    day: int
    month: int
    year: int
    is_leap: bool = False
    month_length: int = 30  # 29 (tháng thiếu) or 30 (tháng đủ)

    def format_short(self, *, include_year: bool = False) -> str:
        leap_suffix = " nhuận" if self.is_leap else ""
        base = f"{self.day:02d}/{self.month:02d}{leap_suffix}"
        if include_year:
            return f"{base}/{self.year}"
        return base

    def format_vi(self, *, include_year: bool = True) -> str:
        day_label = f"Mùng {self.day}" if self.day <= 10 else str(self.day)
        month_names = {1: "Giêng", 12: "Chạp"}
        month_label = (
            f"tháng {month_names[self.month]}"
            if self.month in month_names and not self.is_leap
            else f"tháng {self.month}"
        )
        leap_suffix = " nhuận" if self.is_leap else ""
        if include_year:
            return f"{day_label} {month_label}{leap_suffix} năm {get_year_can_chi(self.year)}"
        return f"{day_label} {month_label}{leap_suffix}"


@dataclass(frozen=True)
class DayAuspiciousInfo:
    """Complete Vietnamese almanac details for a given solar date."""

    solar: date
    lunar: LunarDate
    year_can_chi: str
    month_can_chi: str
    day_can_chi: str
    is_hoang_dao: bool
    star_name: str
    truc_name: str
    truc_note: str
    solar_term: str
    auspicious_hours: tuple[str, ...]


def jd_from_date(dd: int, mm: int, yy: int) -> int:
    """Compute Julian Day Number for a Gregorian/Julian calendar date."""
    a = (14 - mm) // 12
    y = yy + 4800 - a
    m = mm + 12 * a - 3
    jd = dd + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045
    if jd < 2299161:
        jd = dd + (153 * m + 2) // 5 + 365 * y + y // 4 - 32083
    return jd


def jd_to_date(jd: int) -> tuple[int, int, int]:
    """Convert Julian Day Number back to (day, month, year)."""
    if jd > 2299160:
        a = jd + 32044
        b = (4 * a + 3) // 146097
        c = a - (b * 146097) // 4
    else:
        b = 0
        c = jd + 32082
    d = (4 * c + 3) // 1461
    e = c - (1461 * d) // 4
    m = (5 * e + 2) // 153
    day = e - (153 * m + 2) // 5 + 1
    month = m + 3 - 12 * (m // 10)
    year = b * 100 + d - 4800 + m // 10
    return day, month, year


def new_moon(k: int) -> float:
    """Compute the Julian Day of the k-th New Moon since Jan 1, 1900 (UTC)."""
    t = k / 1236.85
    t2 = t * t
    t3 = t2 * t
    dr = math.pi / 180.0
    jd1 = 2415020.75933 + 29.53058868 * k + 0.0001178 * t2 - 0.000000155 * t3
    jd1 += 0.00033 * math.sin((166.56 + 132.87 * t - 0.009173 * t2) * dr)
    m = 359.2242 + 29.10535608 * k - 0.0000333 * t2 - 0.00000347 * t3
    mpr = 306.0253 + 385.81691806 * k + 0.0107306 * t2 + 0.00001236 * t3
    f = 21.2964 + 390.67050646 * k - 0.0016528 * t2 - 0.00000239 * t3

    c1 = (0.1734 - 0.000393 * t) * math.sin(m * dr) + 0.0021 * math.sin(2 * dr * m)
    c1 -= 0.4068 * math.sin(mpr * dr) + 0.0161 * math.sin(dr * 2 * mpr)
    c1 -= 0.0004 * math.sin(dr * 3 * mpr)
    c1 += 0.0104 * math.sin(dr * 2 * f) - 0.0051 * math.sin(dr * (m + mpr))
    c1 -= 0.0074 * math.sin(dr * (m - mpr)) + 0.0004 * math.sin(dr * (2 * f + m))
    c1 -= 0.0004 * math.sin(dr * (2 * f - m)) - 0.0006 * math.sin(dr * (2 * f + mpr))
    c1 += 0.0010 * math.sin(dr * (2 * f - mpr)) + 0.0005 * math.sin(dr * (2 * mpr + m))

    if t < -11:
        delta_t = 0.001 + 0.000839 * t + 0.0002261 * t2 - 0.00000845 * t3 - 0.000000081 * t * t3
    else:
        delta_t = -0.000278 + 0.000265 * t + 0.000262 * t2
    return jd1 + c1 - delta_t


def sun_longitude(jdn: float) -> float:
    """Compute the Sun's ecliptic longitude in radians at Julian Day jdn (UTC)."""
    t = (jdn - 2451545.0) / 36525.0
    t2 = t * t
    dr = math.pi / 180.0
    m = 357.52910 + 35999.05030 * t - 0.0001559 * t2 - 0.00000048 * t * t2
    l0 = 280.46645 + 36000.76983 * t + 0.0003032 * t2
    dl = (1.914600 - 0.004817 * t - 0.000014 * t2) * math.sin(dr * m)
    dl += (0.019993 - 0.000101 * t) * math.sin(dr * 2 * m) + 0.000290 * math.sin(dr * 3 * m)
    l_rad = (l0 + dl) * dr
    l_rad = l_rad - math.pi * 2 * math.floor(l_rad / (math.pi * 2))
    return l_rad


def get_new_moon_day(k: int, time_zone: float = VIETNAM_TZ_OFFSET) -> int:
    """Return local Julian Day Number of the k-th New Moon."""
    return int(math.floor(new_moon(k) + 0.5 + time_zone / 24.0))


def get_sun_longitude_sector(day_number: int, time_zone: float = VIETNAM_TZ_OFFSET) -> int:
    """Return major solar term sector (0..11) at midnight local time for day_number."""
    return int(math.floor(sun_longitude(day_number - 0.5 - time_zone / 24.0) / math.pi * 6.0))


def get_solar_term_index(solar_date: date, time_zone: float = VIETNAM_TZ_OFFSET) -> int:
    """Return the 24 Solar Term index (0..23) at noon local time for solar_date."""
    jd = jd_from_date(solar_date.day, solar_date.month, solar_date.year)
    lon_rad = sun_longitude(jd - time_zone / 24.0)
    return int(math.floor(lon_rad / math.pi * 12.0)) % 24


def get_lunar_month11(yy: int, time_zone: float = VIETNAM_TZ_OFFSET) -> int:
    """Find the Julian Day Number of the start of lunar month 11 (contains Winter Solstice) in year yy."""
    off = jd_from_date(31, 12, yy) - 2415021
    k = int(math.floor(off / 29.530588853))
    nm = get_new_moon_day(k, time_zone)
    sun_long = get_sun_longitude_sector(nm, time_zone)
    if sun_long >= 9:
        nm = get_new_moon_day(k - 1, time_zone)
    return nm


def get_leap_month_offset(a11: int, time_zone: float = VIETNAM_TZ_OFFSET) -> int:
    """Find the leap month offset after lunar month 11 starting at Julian Day a11."""
    k = int(math.floor((a11 - 2415021.076998695) / 29.530588853 + 0.5))
    last = 0
    i = 1
    arc = get_sun_longitude_sector(get_new_moon_day(k + i, time_zone), time_zone)
    while True:
        last = arc
        i += 1
        arc = get_sun_longitude_sector(get_new_moon_day(k + i, time_zone), time_zone)
        if arc == last or i >= 14:
            break
    return i - 1


def solar_to_lunar(solar_date: date, time_zone: float = VIETNAM_TZ_OFFSET) -> LunarDate:
    """Convert a Gregorian solar date to a Vietnamese LunarDate."""
    dd, mm, yy = solar_date.day, solar_date.month, solar_date.year
    day_number = jd_from_date(dd, mm, yy)
    k = int(math.floor((day_number - 2415021.076998695) / 29.530588853))
    month_start = get_new_moon_day(k + 1, time_zone)
    if month_start > day_number:
        month_start = get_new_moon_day(k, time_zone)
        next_month_start = get_new_moon_day(k + 1, time_zone)
    else:
        next_month_start = get_new_moon_day(k + 2, time_zone)

    month_length = next_month_start - month_start

    a11 = get_lunar_month11(yy, time_zone)
    b11 = a11
    if a11 >= month_start:
        lunar_year = yy
        a11 = get_lunar_month11(yy - 1, time_zone)
    else:
        lunar_year = yy + 1
        b11 = get_lunar_month11(yy + 1, time_zone)

    lunar_day = day_number - month_start + 1
    diff = int(math.floor((month_start - a11) / 29.0))
    lunar_leap = False
    lunar_month = diff + 11

    if b11 - a11 > 365:
        leap_month_diff = get_leap_month_offset(a11, time_zone)
        if diff >= leap_month_diff:
            lunar_month = diff + 10
            if diff == leap_month_diff:
                lunar_leap = True

    if lunar_month > 12:
        lunar_month -= 12
    if lunar_month >= 11 and diff < 4:
        lunar_year -= 1

    return LunarDate(
        day=lunar_day,
        month=lunar_month,
        year=lunar_year,
        is_leap=lunar_leap,
        month_length=month_length,
    )


def lunar_to_solar(
    lunar_day: int,
    lunar_month: int,
    lunar_year: int,
    *,
    is_leap: bool = False,
    time_zone: float = VIETNAM_TZ_OFFSET,
) -> date | None:
    """Convert a Vietnamese lunar date to a Gregorian solar date, or None if invalid."""
    if not (1 <= lunar_day <= 30 and 1 <= lunar_month <= 12):
        return None

    if lunar_month < 11:
        a11 = get_lunar_month11(lunar_year - 1, time_zone)
        b11 = get_lunar_month11(lunar_year, time_zone)
    else:
        a11 = get_lunar_month11(lunar_year, time_zone)
        b11 = get_lunar_month11(lunar_year + 1, time_zone)

    k = int(math.floor(0.5 + (a11 - 2415021.076998695) / 29.530588853))
    off = lunar_month - 11
    if off < 0:
        off += 12

    if b11 - a11 > 365:
        leap_off = get_leap_month_offset(a11, time_zone)
        leap_month = leap_off - 2
        if leap_month <= 0:
            leap_month += 12
        if is_leap and lunar_month != leap_month:
            return None
        if is_leap or off >= leap_off:
            off += 1
    elif is_leap:
        return None

    month_start = get_new_moon_day(k + off, time_zone)
    next_month_start = get_new_moon_day(k + off + 1, time_zone)
    month_length = next_month_start - month_start
    if lunar_day > month_length:
        return None

    dd, mm, yy = jd_to_date(month_start + lunar_day - 1)
    try:
        return date(yy, mm, dd)
    except ValueError:
        return None


def get_year_can_chi(lunar_year: int) -> str:
    """Return Can Chi name for a lunar year (e.g. 2026 -> 'Bính Ngọ')."""
    return f"{CAN[(lunar_year + 6) % 10]} {CHI[(lunar_year + 8) % 12]}"


def get_month_can_chi(lunar_month: int, lunar_year: int) -> str:
    """Return Can Chi name for a lunar month in lunar_year."""
    can_idx = (lunar_year * 12 + lunar_month + 3) % 10
    chi_idx = (lunar_month + 1) % 12  # Tháng 1 = Dần (2)
    return f"{CAN[can_idx]} {CHI[chi_idx]}"


def get_day_can_chi_indices(solar_date: date) -> tuple[int, int]:
    """Return (can_idx, chi_idx) for a solar date."""
    jd = jd_from_date(solar_date.day, solar_date.month, solar_date.year)
    return (jd + 9) % 10, (jd + 1) % 12


def get_day_can_chi(solar_date: date) -> str:
    """Return Can Chi name for a solar date."""
    can_idx, chi_idx = get_day_can_chi_indices(solar_date)
    return f"{CAN[can_idx]} {CHI[chi_idx]}"


def get_auspicious_hours(solar_date: date) -> tuple[str, ...]:
    """Return the 6 Giờ Hoàng Đạo strings (e.g. 'Tý (23-1h)') sorted chronologically."""
    _, day_chi_idx = get_day_can_chi_indices(solar_date)
    start_chi = ((day_chi_idx + 4) % 6) * 2
    hour_indices = sorted((start_chi + offset) % 12 for offset in _HOANG_DAO_OFFSETS)
    return tuple(f"{CHI[idx]} ({CHI_HOUR_RANGES[idx]})" for idx in hour_indices)


def get_day_auspicious_info(
    solar_date: date,
    time_zone: float = VIETNAM_TZ_OFFSET,
) -> DayAuspiciousInfo:
    """Compute complete Vietnamese lunar & auspicious almanac info for solar_date."""
    lunar = solar_to_lunar(solar_date, time_zone=time_zone)
    _, day_chi_idx = get_day_can_chi_indices(solar_date)

    # Hoàng Đạo / Hắc Đạo star for this lunar month & day Chi
    month_start_chi = ((lunar.month - 1) % 6) * 2
    star_offset = (day_chi_idx - month_start_chi) % 12
    star_name, is_hoang_dao = HOANG_DAO_STARS[star_offset]

    # 12 Trực (Kiến Trừ): month 1 = Dần (2)
    month_chi_idx = (lunar.month + 1) % 12
    truc_idx = (day_chi_idx - month_chi_idx) % 12
    truc_name, truc_note = TRUC_INFO[truc_idx]

    solar_term = SOLAR_TERMS_VI[get_solar_term_index(solar_date, time_zone=time_zone)]

    return DayAuspiciousInfo(
        solar=solar_date,
        lunar=lunar,
        year_can_chi=get_year_can_chi(lunar.year),
        month_can_chi=get_month_can_chi(lunar.month, lunar.year),
        day_can_chi=get_day_can_chi(solar_date),
        is_hoang_dao=is_hoang_dao,
        star_name=star_name,
        truc_name=truc_name,
        truc_note=truc_note,
        solar_term=solar_term,
        auspicious_hours=get_auspicious_hours(solar_date),
    )


def format_solar_date_vi(target: date, *, include_weekday: bool = False) -> str:
    """Format a solar date as 'DD/MM/YYYY' or 'Thứ X, DD/MM/YYYY'."""
    base = target.strftime("%d/%m/%Y")
    if include_weekday:
        return f"{WEEKDAYS_VI[target.weekday()]}, {base}"
    return base


def iter_dates(start: date, days: int) -> list[date]:
    """Return [start, start + 1d, ..., start + days]."""
    return [start + timedelta(days=offset) for offset in range(max(0, days) + 1)]
