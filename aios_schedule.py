"""Schedule syntax for aios-core jobs. Local time, standard library only.

    every 30m | every 2h | every 1d     repeat (at least every 1 minute)
    daily 09:00                         every day at 09:00
    weekly mon 09:00                    every Monday at 09:00 (mon..sun or full names)
    once 2026-12-24 18:30               one time, then the job disables itself

Times are local wall-clock time. Around daylight-saving changes a daily/weekly job may run an hour
early or late once; `every` jobs are unaffected.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

MIN_INTERVAL_S = 60
MAX_INTERVAL_S = 366 * 86400
GRAMMAR = "every 30m | every 2h | every 1d | daily 09:00 | weekly mon 09:00 | once 2026-12-24 18:30"
DAYS = {"mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "wednesday": 2,
        "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4,
        "sat": 5, "saturday": 5, "sun": 6, "sunday": 6}
_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class Schedule:
    kind: str  # "every" | "daily" | "weekly" | "once"
    seconds: int = 0
    hour: int = 0
    minute: int = 0
    weekday: int = 0
    at: datetime | None = None

    def next_after(self, after: datetime) -> datetime | None:
        """The first run strictly after `after`, or None if there will be no more runs."""
        if self.kind == "every":
            return after + timedelta(seconds=self.seconds)
        if self.kind == "once":
            return self.at if self.at and self.at > after else None
        cand = after.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        if self.kind == "weekly":
            cand += timedelta(days=(self.weekday - after.weekday()) % 7)
        if cand <= after:
            cand += timedelta(days=1 if self.kind == "daily" else 7)
        return cand

    def describe(self) -> str:
        if self.kind == "every":
            s = self.seconds
            return f"every {s // 86400}d" if s % 86400 == 0 else f"every {s // 3600}h" if s % 3600 == 0 else f"every {s // 60}m"
        if self.kind == "daily":
            return f"daily {self.hour:02d}:{self.minute:02d}"
        if self.kind == "weekly":
            return f"weekly {_DAY_NAMES[self.weekday].lower()} {self.hour:02d}:{self.minute:02d}"
        return f"once {self.at:%Y-%m-%d %H:%M}"


def _hm(h: str, m: str) -> tuple[int, int]:
    hour, minute = int(h), int(m)
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ScheduleError(f"not a valid time: {h}:{m}")
    return hour, minute


def parse(spec: str) -> Schedule:
    s = " ".join(str(spec).lower().split())
    if m := re.fullmatch(r"every (\d+)\s*(m|min|mins|minutes?|h|hrs?|hours?|d|days?)", s):
        n, unit = int(m[1]), m[2][0]
        seconds = n * {"m": 60, "h": 3600, "d": 86400}[unit]
        if seconds < MIN_INTERVAL_S:
            raise ScheduleError("jobs cannot run more often than every 1 minute")
        if seconds > MAX_INTERVAL_S:
            raise ScheduleError("interval is longer than a year")
        return Schedule("every", seconds=seconds)
    if m := re.fullmatch(r"daily (\d{1,2}):(\d{2})", s):
        return Schedule("daily", hour=(hm := _hm(m[1], m[2]))[0], minute=hm[1])
    if m := re.fullmatch(r"weekly ([a-z]+) (\d{1,2}):(\d{2})", s):
        if m[1] not in DAYS:
            raise ScheduleError(f"unknown weekday '{m[1]}' (use mon..sun)")
        hm = _hm(m[2], m[3])
        return Schedule("weekly", hour=hm[0], minute=hm[1], weekday=DAYS[m[1]])
    if m := re.fullmatch(r"once (\d{4})-(\d{1,2})-(\d{1,2})[ t](\d{1,2}):(\d{2})", s):
        try:
            at = datetime(int(m[1]), int(m[2]), int(m[3]), *_hm(m[4], m[5]))
        except ValueError as e:
            raise ScheduleError(f"not a valid date: {e}") from None
        return Schedule("once", at=at)
    raise ScheduleError(f"cannot read schedule '{spec}'. Use one of: {GRAMMAR}")


def first_run(sched: Schedule, now: datetime) -> datetime:
    nxt = sched.next_after(now)
    if nxt is None:
        raise ScheduleError("that time is in the past")
    return nxt
