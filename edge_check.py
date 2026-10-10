"""Targeted edge cases: overnight shift date (R1), midnight-crossing punch-out,
microsecond truncation, and late-boundary at the exact grace edge.
Run after sample_seed.py with server running. Local verification only."""
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

BASE = "http://127.0.0.1:8000"
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
passed = failed = 0


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  PASS  {name}")
    else:
        failed += 1; print(f"  FAIL  {name}  {extra}")


def ist_ms(y, mo, d, h, mi, s=0, ms=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp() * 1000) + ms


# Overnight employee EMP0005 (22:00-06:00). Punch-in at 01:30 IST on 2026-08-05 ->
# before shift_end 06:00 -> belongs to previous day 2026-08-04 (R1).
s, b = req("POST", "/attendance/punch-in", {"emp_code": "EMP0005", "punched_at": ist_ms(2026, 8, 5, 1, 30, 0)})
check("overnight early punch belongs to previous day", s == 201 and b.get("date") == "2026-08-04", f"{s} {b.get('date')}")
# Punch-out at 06:40 IST on 2026-08-05 -> work hours across midnight; overtime 40 (end 06:00 next day)
s, b = req("POST", "/attendance/punch-out", {"emp_code": "EMP0005", "punched_at": ist_ms(2026, 8, 5, 6, 40, 0)})
check("overnight punch-out 200", s == 200, f"{s} {b}")
# work: 01:30 -> 06:40 = 5h10m = 5.17
check("overnight work_hours 5.17", b.get("work_hours") == 5.17, b.get("work_hours"))
check("overnight overtime 40 (end 06:00 next day)", b.get("overtime_minutes") == 40, b.get("overtime_minutes"))

# Overnight late: shift start 22:00. Punch at 22:11:00 IST on 2026-08-06 -> 11 min late (>10).
s, b = req("POST", "/attendance/punch-in", {"emp_code": "EMP0005", "punched_at": ist_ms(2026, 8, 6, 22, 11, 0)})
check("overnight date is same day for evening punch", b.get("date") == "2026-08-06", b.get("date"))
check("overnight late 11", b.get("late_minutes") == 11, b.get("late_minutes"))

# Microsecond truncation: punched_at with +900ms must truncate to the whole second.
req("POST", "/employees", {"emp_code": "EMP8001", "name": "Trunc", "email": "tr@x.co", "department": "QA", "joined_on": "2026-07-01"})
raw = ist_ms(2026, 8, 7, 9, 40, 1) + 900  # 09:40:01.900
s, b = req("POST", "/attendance/punch-in", {"emp_code": "EMP8001", "punched_at": raw})
expected = ist_ms(2026, 8, 7, 9, 40, 1)  # truncated to whole second
check("microseconds truncated on store/return", b.get("punch_in") == expected, f"{b.get('punch_in')} exp {expected}")
check("late uses truncated value (09:40:01 -> 10)", b.get("late_minutes") == 10, b.get("late_minutes"))

# Late R2 example: 10:15:59 on 09:30 shift -> 45
req("POST", "/employees", {"emp_code": "EMP8002", "name": "Late45", "email": "l45@x.co", "department": "QA", "joined_on": "2026-07-01"})
s, b = req("POST", "/attendance/punch-in", {"emp_code": "EMP8002", "punched_at": ist_ms(2026, 8, 7, 10, 15, 59)})
check("R2 10:15:59 -> 45", b.get("late_minutes") == 45, b.get("late_minutes"))

print(f"\n==== edge: {passed} passed, {failed} failed ====")
import sys
sys.exit(1 if failed else 0)
