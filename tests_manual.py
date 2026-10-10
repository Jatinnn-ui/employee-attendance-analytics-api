"""Manual integration + edge-case tests against a running server on :8000 with the
sample data seeded. Not part of the submission - a local verification harness.

Run: python tests_manual.py
Requires: the server running, MongoDB reachable, sample_seed.py already run.
"""
import os
import sys
import time
import urllib.request
import urllib.error
import json
from datetime import datetime, timezone, timedelta

BASE = "http://127.0.0.1:8000"
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


def req(method, path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def ist_ms(y, mo, d, h, mi, s=0):
    """Epoch ms for an IST wall-clock time."""
    dt = datetime(y, mo, d, h, mi, s, tzinfo=IST)
    return int(dt.timestamp() * 1000)


# wait for health
for _ in range(20):
    try:
        s, b = req("GET", "/health")
        if s == 200:
            break
    except Exception:
        pass
    time.sleep(0.5)

print("== health ==")
s, b = req("GET", "/health")
check("health 200 ok", s == 200 and b.get("status") == "ok", f"{s} {b}")

print("== employees validation ==")
s, b = req("POST", "/employees", {"emp_code": "BAD", "name": "x", "email": "a@b.co", "department": "D", "joined_on": "2026-01-01"})
check("bad emp_code -> 422", s == 422, s)
s, b = req("POST", "/employees", {"emp_code": "EMP9001", "name": "T", "email": "bademail", "department": "D", "joined_on": "2026-01-01"})
check("bad email -> 422", s == 422, s)
s, b = req("POST", "/employees", {"emp_code": "EMP9001", "name": "T", "email": "t@x.co", "department": "D", "shift_start": "09:30", "shift_end": "09:30", "joined_on": "2026-01-01"})
check("equal shift times -> 422", s == 422, s)
s, b = req("POST", "/employees", {"emp_code": "EMP9001", "name": "Tester", "email": "t@x.co", "department": "QA", "joined_on": "2026-07-01"})
check("create 201", s == 201, f"{s} {b}")
check("defaults applied", b.get("shift_start") == "09:30" and b.get("shift_end") == "18:30", b)
check("created_at epoch ms", isinstance(b.get("created_at"), int) and b["created_at"] > 100000000000, b)
check("no _id/id leaked", "_id" not in b and "id" not in b, b)
s, b = req("POST", "/employees", {"emp_code": "EMP9001", "name": "Dup", "email": "d@x.co", "department": "QA", "joined_on": "2026-07-01"})
check("duplicate emp_code -> 409", s == 409, f"{s} {b}")

print("== employees list ==")
s, b = req("GET", "/employees?page=1&page_size=3")
check("list 200", s == 200, s)
check("page_size respected", len(b["items"]) == 3, len(b["items"]))
check("sorted by emp_code asc", [i["emp_code"] for i in b["items"]] == sorted(i["emp_code"] for i in b["items"]), b["items"])
s, b = req("GET", "/employees?department=Engineering")
check("dept filter total=2", b["total"] == 2, b["total"])
s, b = req("GET", "/employees?department=engineering")
check("dept filter case-sensitive", b["total"] == 0, b["total"])
s, b = req("GET", "/employees?page_size=0")
check("page_size=0 -> 422", s == 422, s)
s, b = req("GET", "/employees?page_size=101")
check("page_size=101 -> 422", s == 422, s)
# pagination page 2
s, p1 = req("GET", "/employees?page=1&page_size=2")
s, p2 = req("GET", "/employees?page=2&page_size=2")
check("page1/page2 disjoint", set(i["emp_code"] for i in p1["items"]).isdisjoint(i["emp_code"] for i in p2["items"]), (p1, p2))

print("== punch-in ==")
s, b = req("POST", "/attendance/punch-in", {"emp_code": "NOPE"})
check("unknown employee -> 404", s == 404, s)
s, b = req("POST", "/attendance/punch-in", {"emp_code": "EMP9001", "punched_at": 123})
check("seconds-looking ts -> 422", s == 422, s)
# 09:40:01 IST on a 09:30 shift -> late 10 (R2)
pin = ist_ms(2026, 7, 15, 9, 40, 1)
s, b = req("POST", "/attendance/punch-in", {"emp_code": "EMP9001", "punched_at": pin})
check("punch-in 201", s == 201, f"{s} {b}")
check("R2 late 09:40:01 -> 10", b.get("late_minutes") == 10, b.get("late_minutes"))
check("date is 2026-07-15", b.get("date") == "2026-07-15", b.get("date"))
check("open record defaults", b.get("punch_out") is None and b.get("work_hours") is None and b.get("overtime_minutes") == 0 and b.get("half_day") is False and b.get("history") == [], b)
# duplicate same date
s, b = req("POST", "/attendance/punch-in", {"emp_code": "EMP9001", "punched_at": ist_ms(2026, 7, 15, 10, 0)})
check("duplicate date -> 409", s == 409, s)

# grace boundary: 09:40:00 -> 0
req("POST", "/employees", {"emp_code": "EMP9002", "name": "Grace", "email": "g@x.co", "department": "QA", "joined_on": "2026-07-01"})
s, b = req("POST", "/attendance/punch-in", {"emp_code": "EMP9002", "punched_at": ist_ms(2026, 7, 15, 9, 40, 0)})
check("R2 late 09:40:00 -> 0", b.get("late_minutes") == 0, b.get("late_minutes"))

print("== punch-out ==")
# EMP9001 punched in 09:40:01. Punch out at 18:59:59 same day.
# work = 18:59:59 - 09:40:01 = 9h 19m 58s = 33598s/3600 = 9.3327... -> 9.33
pout = ist_ms(2026, 7, 15, 18, 59, 59)
s, b = req("POST", "/attendance/punch-out", {"emp_code": "EMP9001", "punched_at": pout})
check("punch-out 200", s == 200, f"{s} {b}")
expected_wh = round(((18*3600+59*60+59) - (9*3600+40*60+1)) / 3600 + 1e-9, 2)
check("R4 work_hours 9.33", b.get("work_hours") == 9.33, f"{b.get('work_hours')} exp 9.33")
# overtime: shift_end 18:30, punch 18:59:59 -> 29m59s -> 29 floored -> < 30 -> 0
check("R3 overtime 29min -> 0", b.get("overtime_minutes") == 0, b.get("overtime_minutes"))
check("half_day false (9.33>=4.5)", b.get("half_day") is False, b)
# already punched out -> 409
s, b = req("POST", "/attendance/punch-out", {"emp_code": "EMP9001", "punched_at": pout})
check("double punch-out -> 409", s == 409, s)
# unknown
s, b = req("POST", "/attendance/punch-out", {"emp_code": "NOPE"})
check("punch-out unknown emp -> 404", s == 404, s)

# overtime exactly 30: shift 18:30, punch 19:00:00 -> 30 -> credited
req("POST", "/employees", {"emp_code": "EMP9003", "name": "OT", "email": "ot@x.co", "department": "QA", "joined_on": "2026-07-01"})
req("POST", "/attendance/punch-in", {"emp_code": "EMP9003", "punched_at": ist_ms(2026, 7, 16, 9, 0, 0)})
s, b = req("POST", "/attendance/punch-out", {"emp_code": "EMP9003", "punched_at": ist_ms(2026, 7, 16, 19, 0, 0)})
check("R3 overtime 30min -> 30", b.get("overtime_minutes") == 30, b.get("overtime_minutes"))

# half-day boundary: 4h29m59s -> <4.5 true ; punch 09:00 -> 13:29:59
req("POST", "/employees", {"emp_code": "EMP9004", "name": "Half", "email": "h@x.co", "department": "QA", "joined_on": "2026-07-01"})
req("POST", "/attendance/punch-in", {"emp_code": "EMP9004", "punched_at": ist_ms(2026, 7, 16, 9, 0, 0)})
s, b = req("POST", "/attendance/punch-out", {"emp_code": "EMP9004", "punched_at": ist_ms(2026, 7, 16, 13, 29, 59)})
check("half_day true at 4.50 boundary (4.4997->4.50? check)", b.get("work_hours") == 4.5 and b.get("half_day") is False, f"wh={b.get('work_hours')} half={b.get('half_day')}")

# punch-out <= punch-in -> 422
req("POST", "/employees", {"emp_code": "EMP9005", "name": "Ord", "email": "o@x.co", "department": "QA", "joined_on": "2026-07-01"})
req("POST", "/attendance/punch-in", {"emp_code": "EMP9005", "punched_at": ist_ms(2026, 7, 17, 9, 0, 0)})
s, b = req("POST", "/attendance/punch-out", {"emp_code": "EMP9005", "punched_at": ist_ms(2026, 7, 17, 8, 0, 0)})
check("punch-out before any punch-in -> 404 (no eligible)", s == 404, s)
s, b = req("POST", "/attendance/punch-out", {"emp_code": "EMP9005", "punched_at": ist_ms(2026, 7, 18, 10, 0, 0)})
check("punch-out >24h after -> 422", s == 422, s)

print("== attendance list ==")
s, b = req("GET", "/attendance?date_from=2026-07-10&date_to=2026-07-01")
check("date_from>date_to -> 422", s == 422, s)
s, b = req("GET", "/attendance?emp_code=EMP0001")
check("filter emp total=2", b["total"] == 2, b["total"])
s, b = req("GET", "/attendance?status=LEAVE")
check("status=LEAVE total=1", b["total"] == 1, b["total"])
s, b = req("GET", "/attendance?page=1&page_size=100")
check("list sorted date desc", [i["date"] for i in b["items"]] == sorted((i["date"] for i in b["items"]), reverse=True), [i["date"] for i in b["items"]])
check("no _id in list items", all("_id" not in i and "id" not in i for i in b["items"]), b["items"][0])

print("== regularize ==")
# EMP0004 2026-07-08 is ABSENT -> change to PRESENT with punch_in requires punch time
pin9 = ist_ms(2026, 7, 8, 9, 0, 0)
pout9 = ist_ms(2026, 7, 8, 18, 0, 0)
s, b = req("PATCH", "/attendance/EMP0004/2026-07-08", {"reason": "correcting", "regularized_by": "hr", "status": "PRESENT", "punch_in": pin9, "punch_out": pout9})
check("regularize ABSENT->PRESENT 200", s == 200, f"{s} {b}")
check("history appended one entry", len(b.get("history", [])) == 1, b.get("history"))
check("history has status change", "status" in b["history"][0]["changes"], b["history"][0]["changes"])
check("history punch_in epoch ms", b["history"][0]["changes"].get("punch_in", {}).get("to") == pin9, b["history"][0]["changes"])
check("recomputed work_hours=9.0", b.get("work_hours") == 9.0, b.get("work_hours"))
# no-op correction -> 422
s, b = req("PATCH", "/attendance/EMP0004/2026-07-08", {"reason": "noop again", "regularized_by": "hr"})
check("no-op correction -> 422", s == 422, s)
# ABSENT with punch time -> 422
s, b = req("PATCH", "/attendance/EMP0003/2026-07-08", {"reason": "wrong", "regularized_by": "hr", "status": "ABSENT", "punch_in": pin9})
check("ABSENT + punch_in -> 422", s == 422, s)
# unknown record
s, b = req("PATCH", "/attendance/EMP0001/2020-01-01", {"reason": "nope!", "regularized_by": "hr", "status": "LEAVE"})
check("regularize unknown record -> 404", s == 404, s)
# unknown employee
s, b = req("PATCH", "/attendance/NOPE/2026-07-08", {"reason": "nope!", "regularized_by": "hr", "status": "LEAVE"})
check("regularize unknown emp -> 404", s == 404, s)

print("== analytics: employee monthly ==")
s, b = req("GET", "/analytics/employees/NOPE/monthly?month=2026-07")
check("monthly unknown emp -> 404", s == 404, s)
s, b = req("GET", "/analytics/employees/EMP0001/monthly?month=2026-07")
check("monthly 200", s == 200, f"{s} {b}")
# EMP0001 joined 2026-01-05; July full month. Working days July 2026 (Mon-Fri):
import calendar
wd = sum(1 for d in range(1, 32) if datetime(2026, 7, d).weekday() < 5)
check("working_days July 2026", b.get("working_days") == wd, f"{b.get('working_days')} exp {wd}")
# EMP0001 has 2 PRESENT weekday records (07-06 Mon, 07-07 Tue) -> present 2
check("present_days=2", b.get("present_days") == 2, b.get("present_days"))

print("== analytics: department summary ==")
s, b = req("GET", "/analytics/departments/summary?month=2026-07")
check("dept summary 200", s == 200, f"{s} {b}")
depts = [i["department"] for i in b["items"]]
check("depts sorted asc", depts == sorted(depts), depts)
# QA employees created with joined_on 2026-07-01 should appear with headcount
qa = next((i for i in b["items"] if i["department"] == "QA"), None)
check("QA present in summary (zero/some logs)", qa is not None, depts)
eng = next((i for i in b["items"] if i["department"] == "Engineering"), None)
check("Engineering headcount=2", eng and eng["headcount"] == 2, eng)

print("== analytics: late leaderboard ==")
s, b = req("GET", "/analytics/leaderboard/late?month=2026-07")
check("leaderboard 200", s == 200, f"{s} {b}")
# EMP0003 has late 35 on 07-06. EMP9001 late 10 on 07-15.
codes = [i["emp_code"] for i in b["items"]]
check("leaderboard includes EMP0003", "EMP0003" in codes, codes)
check("ranks start at 1", b["items"][0]["rank"] == 1 if b["items"] else False, b["items"])

print("== analytics: department trend ==")
s, b = req("GET", "/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-15")
check("trend 200", s == 200, f"{s} {b}")
check("trend one row per day (15)", len(b["items"]) == 15, len(b["items"]))
check("trend weekends included", any(not i["is_working_day"] for i in b["items"]), b["items"])
# 2026-07-06 Mon: only EMP0001 (joined 01-05) has joined; EMP0002 joins 07-13.
# So headcount 1, present 1, rate 1.0.
day6 = next(i for i in b["items"] if i["date"] == "2026-07-06")
check("07-06 present_count 1", day6["present_count"] == 1, day6)
check("07-06 headcount 1 (EMP0002 not joined yet)", day6["headcount"] == 1, day6)
check("07-06 rate 1.0", day6["attendance_rate"] == 1.0, day6)
# 2026-07-14 Tue: both joined (EMP0002 joined 07-13). EMP0002 present -> present 1 of 2.
day14 = next(i for i in b["items"] if i["date"] == "2026-07-14")
check("07-14 headcount 2 (both joined)", day14["headcount"] == 2, day14)
# weekend rate null
sat = next(i for i in b["items"] if i["date"] == "2026-07-04")  # Saturday
check("07-04 Saturday rate null", sat["attendance_rate"] is None, sat)
s, b = req("GET", "/analytics/departments/Nope/trend?from=2026-07-01&to=2026-07-10")
check("trend unknown dept -> 404", s == 404, s)
s, b = req("GET", "/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-01")
check("trend to<from -> 422", s == 422, s)
s, b = req("GET", "/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-12-31")
check("trend >92 days -> 422", s == 422, s)

print("== admin explain ==")
for ep, qs in [
    ("attendance_list", "?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-31"),
    ("employee_monthly", "?emp_code=EMP0001&month=2026-07"),
    ("department_summary", "?month=2026-07"),
    ("late_leaderboard", "?month=2026-07"),
    ("department_trend", "?department=Engineering&from=2026-07-01&to=2026-07-10"),
]:
    s, b = req("GET", f"/admin/explain/{ep}{qs}")
    check(f"explain {ep} 200", s == 200, f"{s} {str(b)[:120]}")
    check(f"explain {ep} has explain doc", isinstance(b.get("explain"), dict), b)
s, b = req("GET", "/admin/explain/employee_monthly")
check("explain missing params -> 422", s == 422, s)
s, b = req("GET", "/admin/explain/bogus")
check("explain bad endpoint -> 422", s == 422, s)

print(f"\n==== {passed} passed, {failed} failed ====")
sys.exit(1 if failed else 0)
