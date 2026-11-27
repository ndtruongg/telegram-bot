"""Vietnamese lunar/solar holidays, Rằm/Mùng 1 events, and auspicious day queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from bot.features.lunar_calendar.calendar import (
    DayAuspiciousInfo,
    LunarDate,
    VIETNAM_TZ_OFFSET,
    get_day_auspicious_info,
    solar_to_lunar,
)

EventCategory = Literal["moon", "lunar_holiday", "solar_holiday"]


@dataclass(frozen=True)
class CalendarEvent:
    """A Vietnamese lunar/solar holiday or monthly Rằm / Mùng 1 occurrence."""

    event_id: str
    name: str
    solar_date: date
    lunar_date: LunarDate
    category: EventCategory
    icon: str
    is_public_holiday: bool = False
    note: str = ""


# Traditional Vietnamese Lunar Holidays: (month, day) -> (event_id, name, icon, is_public_holiday, note)
# Note: Tết Nguyên Đán (1/1), Rằm tháng Giêng (15/1), Lễ Phật Đản (15/4), Lễ Vu Lan (15/7),
# Tết Trung Thu (15/8) intentionally supersede the generic Mùng 1 / Rằm entry on those dates.
LUNAR_HOLIDAYS: dict[tuple[int, int], tuple[str, str, str, bool, str]] = {
    (1, 1): (
        "lunar_tet_1",
        "Tết Nguyên Đán (Mùng 1 Tết)",
        "🧧",
        True,
        "Chúc mừng năm mới! Ngày đầu tiên của năm âm lịch.",
    ),
    (1, 2): ("lunar_tet_2", "Mùng 2 Tết Nguyên Đán", "🧧", True, "Tết cha, Tết mẹ — chúc Tết gia đình."),
    (1, 3): ("lunar_tet_3", "Mùng 3 Tết Nguyên Đán", "🧧", True, "Tết thầy — du xuân, chúc Tết bạn bè."),
    (1, 15): (
        "lunar_ram_thang_gieng",
        "Rằm tháng Giêng (Tết Nguyên Tiêu)",
        "🌕",
        False,
        "Cúng Rằm lớn đầu năm, cầu bình an may mắn.",
    ),
    (3, 3): ("lunar_han_thuc", "Tết Hàn Thực", "🍡", False, "Ngày làm bánh trôi, bánh chay tưởng nhớ tổ tiên."),
    (3, 10): (
        "lunar_gio_to",
        "Giỗ Tổ Hùng Vương",
        "🇻🇳",
        True,
        "Quốc lễ tưởng nhớ các Vua Hùng (nghỉ lễ).",
    ),
    (4, 15): ("lunar_phat_dan", "Lễ Phật Đản (Rằm tháng 4)", "🪷", False, "Đại lễ kỷ niệm ngày Đức Phật đản sinh."),
    (5, 5): ("lunar_doan_ngo", "Tết Đoan Ngọ", "🍑", False, "Tết diệt sâu bọ, ăn cơm rượu nếp và hoa quả."),
    (7, 15): (
        "lunar_vu_lan",
        "Lễ Vu Lan (Rằm tháng 7)",
        "🏮",
        False,
        "Mùa Vu Lan báo hiếu cha mẹ và lễ xá tội vong nhân.",
    ),
    (8, 15): (
        "lunar_trung_thu",
        "Tết Trung Thu (Rằm tháng 8)",
        "🥮",
        False,
        "Tết đoàn viên, phá cỗ trông trăng.",
    ),
    (12, 23): (
        "lunar_ong_tao",
        "Ông Công Ông Táo",
        "🐟",
        False,
        "Lễ tiễn Táo Quân chầu trời.",
    ),
}

# Fixed Vietnamese Solar Holidays: (month, day) -> (event_id, name, icon, is_public_holiday, note)
SOLAR_HOLIDAYS: dict[tuple[int, int], tuple[str, str, str, bool, str]] = {
    (1, 1): ("solar_new_year", "Tết Dương lịch", "🎆", True, "Ngày đầu năm mới dương lịch (nghỉ lễ)."),
    (2, 3): ("solar_party_day", "Ngày Thành lập Đảng CSVN", "🇻🇳", False, "Kỷ niệm ngày thành lập Đảng 03/02."),
    (2, 27): ("solar_doctors_day", "Ngày Thầy thuốc Việt Nam", "🩺", False, "Tri ân đội ngũ y bác sĩ Việt Nam."),
    (3, 8): ("solar_womens_day", "Ngày Quốc tế Phụ nữ 8/3", "💐", False, "Chúc mừng và tri ân phái đẹp."),
    (4, 30): (
        "solar_reunification",
        "Ngày Giải phóng miền Nam 30/4",
        "🇻🇳",
        True,
        "Kỷ niệm thống nhất đất nước (nghỉ lễ).",
    ),
    (5, 1): (
        "solar_labor_day",
        "Ngày Quốc tế Lao động 1/5",
        "🎉",
        True,
        "Ngày Quốc tế Lao động (nghỉ lễ).",
    ),
    (6, 1): ("solar_childrens_day", "Ngày Quốc tế Thiếu nhi 1/6", "🎈", False, "Ngày hội dành cho các em nhỏ."),
    (7, 27): (
        "solar_war_invalids",
        "Ngày Thương binh - Liệt sĩ 27/7",
        "🕯️",
        False,
        "Uống nước nhớ nguồn, tri ân các anh hùng liệt sĩ.",
    ),
    (9, 2): (
        "solar_national_day",
        "Ngày Quốc khánh Việt Nam 2/9",
        "🇻🇳",
        True,
        "Quốc khánh nước CHXHCN Việt Nam (nghỉ lễ).",
    ),
    (10, 20): ("solar_vn_womens_day", "Ngày Phụ nữ Việt Nam 20/10", "🌹", False, "Chúc mừng ngày Phụ nữ Việt Nam."),
    (11, 20): ("solar_teachers_day", "Ngày Nhà giáo Việt Nam 20/11", "📚", False, "Tri ân thầy cô giáo."),
    (11, 24): (
        "solar_vn_culture_day",
        "Ngày Văn hóa Việt Nam 24/11",
        "🎭",
        True,
        "Ngày Văn hóa Việt Nam — tôn vinh bản sắc văn hóa dân tộc (nghỉ lễ).",
    ),
    (12, 25): ("solar_christmas", "Lễ Giáng sinh", "🎄", False, "Giáng sinh an lành."),
}


def get_events_for_date(
    solar_date: date,
    *,
    lunar_date: LunarDate | None = None,
    time_zone: float = VIETNAM_TZ_OFFSET,
) -> list[CalendarEvent]:
    """Return all Vietnamese holidays and Rằm/Mùng 1 events occurring on solar_date."""
    lunar = lunar_date or solar_to_lunar(solar_date, time_zone=time_zone)
    events: list[CalendarEvent] = []

    # 1. Fixed solar holidays
    solar_hit = SOLAR_HOLIDAYS.get((solar_date.month, solar_date.day))
    if solar_hit is not None:
        event_id, name, icon, is_public, note = solar_hit
        events.append(
            CalendarEvent(
                event_id=event_id,
                name=name,
                solar_date=solar_date,
                lunar_date=lunar,
                category="solar_holiday",
                icon=icon,
                is_public_holiday=is_public,
                note=note,
            )
        )

    # 2. Lunar holidays (non-leap lunar months only)
    lunar_holiday_hit = None
    if not lunar.is_leap:
        lunar_holiday_hit = LUNAR_HOLIDAYS.get((lunar.month, lunar.day))
        if lunar_holiday_hit is not None:
            event_id, name, icon, is_public, note = lunar_holiday_hit
            events.append(
                CalendarEvent(
                    event_id=event_id,
                    name=name,
                    solar_date=solar_date,
                    lunar_date=lunar,
                    category="lunar_holiday",
                    icon=icon,
                    is_public_holiday=is_public,
                    note=note,
                )
            )
        elif lunar.month == 12 and lunar.day == lunar.month_length:
            # Giao thừa: 30 Tết (tháng đủ) hoặc 29 Tết (tháng thiếu)
            events.append(
                CalendarEvent(
                    event_id="lunar_giao_thua",
                    name=f"Giao thừa (Tất niên {lunar.day} Tết)",
                    solar_date=solar_date,
                    lunar_date=lunar,
                    category="lunar_holiday",
                    icon="🎇",
                    is_public_holiday=True,
                    note="Đêm Giao thừa đón năm mới âm lịch.",
                )
            )

    # 3. Monthly Mùng 1 & Rằm (if not already represented by a named lunar holiday on this day)
    if lunar_holiday_hit is None:
        leap_label = " nhuận" if lunar.is_leap else ""
        month_suffix = (
            "Giêng"
            if lunar.month == 1 and not lunar.is_leap
            else ("Chạp" if lunar.month == 12 and not lunar.is_leap else str(lunar.month))
        )
        if lunar.day == 1:
            events.append(
                CalendarEvent(
                    event_id=f"moon_m1_m{lunar.month}{'_leap' if lunar.is_leap else ''}",
                    name=f"Mùng 1 tháng {month_suffix}{leap_label} Âm lịch",
                    solar_date=solar_date,
                    lunar_date=lunar,
                    category="moon",
                    icon="🌑",
                    is_public_holiday=False,
                    note="Ngày đầu tháng âm lịch — thắp hương, cầu bình an.",
                )
            )
        elif lunar.day == 15:
            events.append(
                CalendarEvent(
                    event_id=f"moon_r15_m{lunar.month}{'_leap' if lunar.is_leap else ''}",
                    name=f"Rằm tháng {month_suffix}{leap_label} Âm lịch",
                    solar_date=solar_date,
                    lunar_date=lunar,
                    category="moon",
                    icon="🌕",
                    is_public_holiday=False,
                    note="Ngày Rằm âm lịch — chuẩn bị hoa quả, lễ cúng Rằm.",
                )
            )

    return events


def get_upcoming_events(
    start_date: date,
    days_ahead: int = 45,
    *,
    time_zone: float = VIETNAM_TZ_OFFSET,
) -> list[tuple[int, CalendarEvent]]:
    """Return ``(days_left, CalendarEvent)`` for events in ``[start_date, start_date + days_ahead]``."""
    results: list[tuple[int, CalendarEvent]] = []
    for offset in range(max(0, days_ahead) + 1):
        target = start_date + timedelta(days=offset)
        for event in get_events_for_date(target, time_zone=time_zone):
            results.append((offset, event))
    return results


def get_upcoming_good_days(
    start_date: date,
    days_ahead: int = 14,
    *,
    time_zone: float = VIETNAM_TZ_OFFSET,
) -> list[tuple[int, DayAuspiciousInfo]]:
    """Return ``(days_left, DayAuspiciousInfo)`` for Hoàng Đạo days in ``[start_date, start_date + days_ahead]``."""
    results: list[tuple[int, DayAuspiciousInfo]] = []
    for offset in range(max(0, days_ahead) + 1):
        target = start_date + timedelta(days=offset)
        info = get_day_auspicious_info(target, time_zone=time_zone)
        if info.is_hoang_dao:
            results.append((offset, info))
    return results


def format_countdown_vi(days_left: int) -> str:
    """Format a compact Vietnamese countdown label."""
    if days_left == 0:
        return "Hôm nay"
    if days_left == 1:
        return "Ngày mai"
    if days_left > 1:
        return f"Còn {days_left} ngày"
    return f"Đã qua {abs(days_left)} ngày"
