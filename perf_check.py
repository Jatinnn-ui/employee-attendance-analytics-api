"""Load ~100k attendance records into a throwaway DB and verify the explain plans the
app produces use IXSCAN and contain no COLLSCAN. Local verification only; not submitted.

Run: python perf_check.py
Uses MONGO_DB_PERF (default attendance_perf) so it never touches your real data.
"""
import os
import random
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING, DESCENDING

load_dotenv()
URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB = os.getenv("MONGO_DB_PERF", "attendance_perf")

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

client = MongoClient(URI, tz_aware=False)
db = client[DB]

print(f"seeding into {DB} ...")
db.employees.delete_many({})
db.attendance_logs.delete_many({})

DEPARTMENTS = ["Engineering", "Sales", "Support", "HR", "Finance"]
N_EMP = 400
emps = []
for i in range(1, N_EMP + 1):
    code = f"EMP{i:05d}"
    emps.append({
        "emp_code": code,
        "name": f"Emp {i}",
        "email": f"emp{i}@example.com",
        "department": random.choice(DEPARTMENTS),
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
        "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=UTC).replace(tzinfo=None),
    })
db.employees.insert_many(emps)

statuses = ["PRESENT", "PRESENT", "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
batch = []
start = datetime(2026, 1, 1)
total = 0
for emp in emps:
    for day in range(260):  # ~260 days each -> ~104k docs
        d = (start + timedelta(days=day)).date().isoformat()
        st = random.choice(statuses)
        if st in ("ABSENT", "LEAVE"):
            doc = {"emp_code": emp["emp_code"], "date": d, "status": st,
                   "punch_in": None, "punch_out": None, "work_hours": None,
                   "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
        else:
            pin = datetime(2026, 1, 1) + timedelta(days=day, hours=4, minutes=random.randint(0, 90))
            pout = pin + timedelta(hours=random.randint(3, 10))
            late = random.choice([0, 0, 0, 12, 35, 45])
            doc = {"emp_code": emp["emp_code"], "date": d, "status": st,
                   "punch_in": pin, "punch_out": pout, "work_hours": round((pout - pin).total_seconds() / 3600, 2),
                   "late_minutes": late, "overtime_minutes": random.choice([0, 0, 30, 40]),
                   "half_day": False, "history": []}
        batch.append(doc)
        total += 1
        if len(batch) >= 5000:
            db.attendance_logs.insert_many(batch)
            batch = []
if batch:
    db.attendance_logs.insert_many(batch)
print(f"inserted {total} attendance docs")

# Create the same indexes the app creates.
db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")
db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date")
db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)], name="status_date")

# Point the app's module at this DB and reuse its real pipeline/query builders.
os.environ["MONGO_DB"] = DB
import importlib
import app.main as m
importlib.reload(m)
m.db = db  # ensure helpers use the perf DB


def stages_of(explain):
    """Flatten an explain doc into a list of stage names (IXSCAN/COLLSCAN/etc.)."""
    found = []

    def walk(o):
        if isinstance(o, dict):
            if "stage" in o and isinstance(o["stage"], str):
                found.append(o["stage"])
            if "inputStage" in o:
                walk(o["inputStage"])
            for v in o.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(explain)
    return found


def check_plan(label, explain):
    s = stages_of(explain)
    has_ix = "IXSCAN" in s
    has_coll = "COLLSCAN" in s
    status = "OK " if (has_ix and not has_coll) else "BAD"
    print(f"  [{status}] {label:22s} IXSCAN={has_ix} COLLSCAN={has_coll}  stages={sorted(set(s))}")
    return has_ix and not has_coll


print("\nexplain plans on ~100k docs:")
ok = True
ok &= check_plan("attendance_list", m.explain_find(
    "attendance_logs", m.build_attendance_query("EMP00010", "2026-03-01", "2026-03-31", None),
    sort=[("date", DESCENDING), ("emp_code", ASCENDING)], skip=0, limit=20))
ok &= check_plan("attendance_list(date)", m.explain_find(
    "attendance_logs", m.build_attendance_query(None, "2026-03-01", "2026-03-31", None),
    sort=[("date", DESCENDING), ("emp_code", ASCENDING)], skip=0, limit=20))
ok &= check_plan("employee_monthly", m.explain_aggregate(
    "attendance_logs", m.employee_monthly_pipeline("EMP00010", "2026-03")))
ok &= check_plan("late_leaderboard", m.explain_aggregate(
    "attendance_logs", m.late_leaderboard_pipeline("2026-03", None)))

# department_summary runs on employees; department_trend on attendance_logs.
emps_e = list(db.employees.find({"department": "Engineering"}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
codes = [e["emp_code"] for e in emps_e]
from datetime import date as date_cls
jd = {e["emp_code"]: date_cls.fromisoformat(e["joined_on"]) for e in emps_e}
ok &= check_plan("department_trend", m.explain_aggregate(
    "attendance_logs", m.department_trend_pipeline("Engineering", "2026-03-01", "2026-03-31", codes, jd)))
# department_summary's first stage is on employees (small); its $lookup into
# attendance_logs is what must be indexed. We report its plan for completeness.
ds = m.explain_aggregate("employees", m.department_summary_pipeline("2026-03", "Engineering"))
check_plan("department_summary", ds)

print("\nRESULT:", "all attendance_logs plans use IXSCAN, no COLLSCAN" if ok else "SOME PLANS NEED ATTENTION")
