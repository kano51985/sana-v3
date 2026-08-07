from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_FIXED_OFFSETS = {
    "UTC": timezone.utc,
    "Asia/Shanghai": timezone(timedelta(hours=8), name="Asia/Shanghai"),
}


class CurrentTimeProvider:
    """Provide a Chinese-readable current time string for prompt injection."""

    def __init__(self, timezone_override: str | None = None):
        self.timezone_override = timezone_override

    def format_now(self) -> str:
        tz = self._resolved_tz()
        if tz is not None:
            now = datetime.now(tz)
            label = self.timezone_override
        else:
            now = datetime.now().astimezone()
            label = now.tzname() or "本地时间"
        return self._format(now, label)

    def _resolved_tz(self):
        if not self.timezone_override:
            return None
        if self.timezone_override in _FIXED_OFFSETS:
            return _FIXED_OFFSETS[self.timezone_override]
        try:
            return ZoneInfo(self.timezone_override)
        except (ZoneInfoNotFoundError, ValueError):
            return None

    def _format(self, now: datetime, timezone_label: str) -> str:
        weekday = _WEEKDAYS[now.weekday()]
        return (
            f"{now.year}年{now.month}月{now.day}日 {weekday} "
            f"{now:%H:%M:%S}（{timezone_label}）"
        )
