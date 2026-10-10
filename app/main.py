"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars win)

All application code lives in this single file, as required by the candidate kit.
The HTTP contract is openapi.yaml; the stored data model is DATA_MODEL.md; the
business rules R1-R10 are in openapi.yaml and summarised inline where they are used.

Key conventions implemented here
--------------------------------
* Instants cross the API boundary as integer epoch *milliseconds* and are stored in
  MongoDB as BSON datetimes. We treat stored datetimes as naive-UTC and convert
  explicitly (see `to_epoch_ms` / `from_epoch_ms`). Before any calculation an instant
  is truncated to whole seconds (R1).
* Calendar dates ("YYYY-MM-DD") and shift times ("HH:MM") are plain IST strings.
* Derived fields (work_hours, late_minutes, overtime_minutes, half_day) are computed by
  this API and read as stored by analytics.
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime, date as date_cls, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

# Real environment variables must take precedence over .env, which is python-dotenv's
# default behaviour (load_dotenv does not override already-set env vars).
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# IST is UTC+05:30 (R1). Used only for converting between stored UTC instants and the
# IST calendar reasoning the rules are written in.
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

# Epoch-millisecond acceptance window from the contract (EpochMillis schema).
EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=False)
db = client[MONGO_DB]


# --------------------------------------------------------------------------- #
# Time / instant helpers
# --------------------------------------------------------------------------- #
def from_epoch_ms(ms: int) -> datetime:
    """Epoch milliseconds -> naive-UTC datetime truncated to whole seconds (R1).

    We store naive datetimes (PyMongo's default read shape) that *represent* UTC, so
    the whole codebase stays on one consistent clock.
    """
    whole_seconds = ms // 1000  # truncate microseconds (R1), floor toward the epoch
    return datetime.fromtimestamp(whole_seconds, tz=UTC).replace(tzinfo=None)


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Stored datetime -> epoch milliseconds (or None).

    Stored datetimes are treated as UTC. If a tz-aware value ever shows up we convert
    it to UTC first so we never mix naive and aware clocks.
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    # Truncate to whole seconds defensively; stored values are already truncated.
    return int(dt.replace(microsecond=0).replace(tzinfo=UTC).timestamp() * 1000)


def utc_to_ist(dt: datetime) -> datetime:
    """Interpret a naive-UTC datetime as an IST wall-clock datetime (naive)."""
    return (dt.replace(tzinfo=UTC).astimezone(IST)).replace(tzinfo=None)


def now_utc_truncated() -> datetime:
    """Server clock as naive-UTC truncated to whole seconds."""
    return datetime.now(tz=UTC).replace(microsecond=0, tzinfo=None)


def parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def half_up(value: float, places: int) -> float:
    """Decimal half-up rounding (R4/R8) - not banker's rounding, not float rounding."""
    quant = Decimal(1).scaleb(-places)  # e.g. places=2 -> Decimal('0.01')
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Attendance-date and shift helpers (R1)
# --------------------------------------------------------------------------- #
def is_overnight(shift_start: str, shift_end: str) -> bool:
    """R1: shift_end <= shift_start means the shift crosses midnight."""
    return shift_end <= shift_start


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date a punch-in belongs to.

    Normal shift: the IST calendar date of punch_in.
    Overnight shift: a punch-in whose IST time-of-day is strictly earlier than
    shift_end belongs to the shift that *started the previous day*.
    """
    ist = utc_to_ist(punch_in)
    if is_overnight(shift_start, shift_end):
        eh, em = parse_hhmm(shift_end)
        end_seconds = (eh * 60 + em) * 60
        pi_seconds = ist.hour * 3600 + ist.minute * 60 + ist.second
        # "earlier than shift_end" (R1): a punch strictly before the shift_end
        # time-of-day belongs to the shift that started the previous calendar day.
        if pi_seconds < end_seconds:
            return (ist.date() - timedelta(days=1)).isoformat()
    return ist.date().isoformat()


def shift_start_instant_utc(date_str: str, shift_start: str) -> datetime:
    """UTC instant of shift_start on the record's attendance date (IST)."""
    y, mo, d = map(int, date_str.split("-"))
    h, mi = parse_hhmm(shift_start)
    ist_dt = datetime(y, mo, d, h, mi, 0, tzinfo=IST)
    return ist_dt.astimezone(UTC).replace(tzinfo=None)


def shift_end_instant_utc(date_str: str, shift_start: str, shift_end: str) -> datetime:
    """UTC instant of shift_end for a record whose attendance date is `date_str`.

    For overnight shifts the end falls on the *next* calendar day (R1/R3).
    """
    y, mo, d = map(int, date_str.split("-"))
    h, mi = parse_hhmm(shift_end)
    end_date = date_cls(y, mo, d)
    if is_overnight(shift_start, shift_end):
        end_date = end_date + timedelta(days=1)
    ist_dt = datetime(end_date.year, end_date.month, end_date.day, h, mi, 0, tzinfo=IST)
    return ist_dt.astimezone(UTC).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Derived-field computations (R2-R5)
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R2: grace is 10 minutes; late only when strictly more than 10:00 after
    shift_start; then floor(minutes since shift_start) - not since grace ended."""
    start = shift_start_instant_utc(date_str, shift_start)
    delta_seconds = (punch_in - start).total_seconds()
    if delta_seconds <= 600:  # within/at the 10:00 grace boundary -> not late
        return 0
    return int(delta_seconds // 60)  # whole minutes since shift_start, floored


def compute_overtime_minutes(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: floor(minutes from shift_end to punch_out), credited only when >= 30."""
    end = shift_end_instant_utc(date_str, shift_start, shift_end)
    minutes = int((punch_out - end).total_seconds() // 60)
    if minutes >= 30:
        return minutes
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) seconds / 3600, rounded to 2 decimals half-up."""
    seconds = (punch_out - punch_in).total_seconds()
    return half_up(seconds / 3600, 2)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: half_day true when rounded work_hours strictly < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


# --------------------------------------------------------------------------- #
# Pydantic models (request validation) - Pydantic v2
# --------------------------------------------------------------------------- #
EMP_CODE_PATTERN = r"^EMP\d{4,6}$"
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
HHMM_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


def validate_epoch_ms(v: Any) -> int:
    """Reject seconds-looking values, floats, strings and out-of-range numbers (422)."""
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("must be an integer epoch-millisecond value")
    if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
        raise ValueError("epoch milliseconds out of accepted range")
    return v


class EmployeeIn(BaseModel):
    model_config = {"extra": "ignore"}

    emp_code: str = Field(pattern=EMP_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=EMAIL_PATTERN)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: str = Field(pattern=DATE_PATTERN)

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        try:
            date_cls.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = {"extra": "ignore"}

    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def _epoch(cls, v):
        if v is None:
            return v
        return validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def _presence(cls, v):
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {PRESENCE_STATUSES}")
        return v


class PunchOutIn(BaseModel):
    model_config = {"extra": "ignore"}

    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def _epoch(cls, v):
        if v is None:
            return v
        return validate_epoch_ms(v)


class RegularizeIn(BaseModel):
    # Only these fields can be changed; derived fields are never accepted (extra=ignore
    # means a client sending work_hours/etc. is silently ignored per the contract).
    model_config = {"extra": "ignore"}

    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    # Sentinels so we can distinguish "field omitted" from "field explicitly null". The
    # contract only lets a client *set* punch times to real instants, so None == omitted.
    @field_validator("status")
    @classmethod
    def _status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {ALL_STATUSES}")
        return v

    @field_validator("punch_in", "punch_out")
    @classmethod
    def _epoch(cls, v):
        if v is None:
            return v
        return validate_epoch_ms(v)


# --------------------------------------------------------------------------- #
# Serialization helpers (never expose _id; instants -> epoch ms)
# --------------------------------------------------------------------------- #
def serialize_history_entry(entry: dict) -> dict:
    """History `changes` values for punch_in/punch_out are stored as BSON dates and must
    be returned as epoch milliseconds; other fields keep their native types."""
    changes = {}
    for field, pair in (entry.get("changes") or {}).items():
        frm, to = pair.get("from"), pair.get("to")
        if field in ("punch_in", "punch_out"):
            frm = to_epoch_ms(frm) if isinstance(frm, datetime) else frm
            to = to_epoch_ms(to) if isinstance(to, datetime) else to
        changes[field] = {"from": frm, "to": to}
    return {
        "at": to_epoch_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_attendance(doc: dict) -> dict:
    """Project a stored attendance_logs document into the API's AttendanceRecord shape.
    Handles legacy records that omit half_day / history (treat as false / [])."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day") or False),
        "history": [serialize_history_entry(e) for e in (doc.get("history") or [])],
    }


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Indexes (created idempotently at startup)
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    # Unique business key for employees; enforces 409 on duplicate emp_code at the DB.
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    # Supports department summary / trend joins and R9 headcount (department + joined_on).
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined"
    )

    # Unique compound key: at most one record per (emp_code, date). Makes concurrent
    # punch-ins race-safe (DuplicateKeyError -> 409) and backs employee+month queries.
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date"
    )
    # Backs GET /attendance sort (date desc, emp_code asc) and date-range filters, and
    # the trend/department pipelines that filter by date then group.
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp"
    )
    # Backs status-filtered listing and analytics grouping by status/date.
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", ASCENDING)], name="status_date"
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
def root():
    """Convenience redirect: opening the bare root sends you to the Swagger UI.
    Not part of the API contract (hidden from the schema)."""
    return RedirectResponse(url="/docs")


@app.get("/health")
def health():
    """200 only when MongoDB answers a ping, else 503."""
    try:
        db.command("ping")
    except PyMongoError:
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc_truncated()  # server clock (R-contract)
    try:
        # Rely on the unique index, not a check-then-insert, so concurrent duplicate
        # creates yield exactly one 201 and the rest 409.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict = {}
    if department is not None:
        q["department"] = department  # exact, case-sensitive
    total = db.employees.count_documents(q)  # counts the filtered set (R10)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)  # correct offset (R10)
        .limit(page_size)
    )
    items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance: punch-in / punch-out
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")

    punched = from_epoch_ms(body.punched_at) if body.punched_at is not None else now_utc_truncated()
    d = attendance_date(punched, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(punched, d, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": punched,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # The unique (emp_code, date) index is the single source of truth for the race:
        # two simultaneous inserts -> exactly one succeeds, the other raises here.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")

    punched = from_epoch_ms(body.punched_at) if body.punched_at is not None else now_utc_truncated()

    # Most recent open-or-any record whose punch_in <= punched_at. This naturally works
    # across midnight for overnight shifts because we match on the stored instant, not
    # on the date string.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no eligible punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")

    pin = record["punch_in"]
    if punched <= pin:
        raise HTTPException(422, "punch-out must be after punch-in")
    if (punched - pin) > timedelta(hours=24):
        raise HTTPException(422, "punch-out must be within 24 hours of punch-in")

    work_hours = compute_work_hours(pin, punched)
    overtime = compute_overtime_minutes(punched, record["date"], emp["shift_start"], emp["shift_end"])
    half = compute_half_day(work_hours)

    # Atomic compare-and-set: only update if still open. Two simultaneous punch-outs ->
    # exactly one matches punch_out: None and wins; the other gets 0 matches -> 409.
    res = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": punched,
            "work_hours": work_hours,
            "overtime_minutes": overtime,
            "half_day": half,
        }},
        return_document=True,
    )
    if res is None:
        raise HTTPException(409, "record already punched out")
    return serialize_attendance(res)


# --------------------------------------------------------------------------- #
# Attendance: list
# --------------------------------------------------------------------------- #
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(422, "invalid status")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

    q = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [serialize_attendance(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def build_attendance_query(emp_code, date_from, date_to, status) -> dict:
    """Shared filter builder so /attendance and /admin/explain stay identical."""
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        rng = {}
        if date_from:
            rng["$gte"] = date_from  # inclusive
        if date_to:
            rng["$lte"] = date_to  # inclusive
        q["date"] = rng
    if status:
        q["status"] = status
    return q


# --------------------------------------------------------------------------- #
# Attendance: regularize (manual correction)
# --------------------------------------------------------------------------- #
@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "no record for that date")

    fields = body.model_fields_set
    new_status = body.status if "status" in fields else record["status"]
    shift_start, shift_end = emp["shift_start"], emp["shift_end"]

    # Resolve the requested punch times (converting epoch ms -> stored datetime).
    pin_supplied = "punch_in" in fields
    pout_supplied = "punch_out" in fields
    new_pin = from_epoch_ms(body.punch_in) if pin_supplied else record.get("punch_in")
    new_pout = from_epoch_ms(body.punch_out) if pout_supplied else record.get("punch_out")

    if new_status in ("ABSENT", "LEAVE"):
        # These statuses clear both punch times; supplying any punch time is 422.
        if pin_supplied or pout_supplied:
            raise HTTPException(422, "punch times cannot be supplied with ABSENT/LEAVE")
        new_pin = None
        new_pout = None
    else:  # presence status requires a punch_in (supplied or already stored)
        if new_pin is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        # punch_in must stay on the record's attendance date (R1).
        if attendance_date(new_pin, shift_start, shift_end) != date:
            raise HTTPException(422, "punch_in must fall on the record's attendance date")
        if new_pout is not None:
            if new_pout <= new_pin:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_pout - new_pin) > timedelta(hours=24):
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # Recompute derived fields from the FINAL values (R2-R5).
    if new_status in ("ABSENT", "LEAVE") or new_pin is None:
        new_late, new_ot, new_wh, new_half = 0, 0, None, False
    else:
        new_late = compute_late_minutes(new_pin, date, shift_start, shift_end)
        if new_pout is not None:
            new_wh = compute_work_hours(new_pin, new_pout)
            new_ot = compute_overtime_minutes(new_pout, date, shift_start, shift_end)
            new_half = compute_half_day(new_wh)
        else:
            new_wh, new_ot, new_half = None, 0, False

    # Build the set of changes (only fields that actually changed), using the stored
    # (old) values as `from`. Derived fields are included when they change.
    old = {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": int(record.get("late_minutes") or 0),
        "overtime_minutes": int(record.get("overtime_minutes") or 0),
        "half_day": bool(record.get("half_day") or False),
    }
    new = {
        "status": new_status,
        "punch_in": new_pin,
        "punch_out": new_pout,
        "work_hours": new_wh,
        "late_minutes": new_late,
        "overtime_minutes": new_ot,
        "half_day": new_half,
    }
    changes = {}
    for key in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        if old[key] != new[key]:
            changes[key] = {"from": old[key], "to": new[key]}

    if not changes:
        raise HTTPException(422, "the correction changes nothing")

    history_entry = {
        "at": now_utc_truncated(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,  # punch_in/out stored as BSON dates here
    }

    # Optimistic concurrency: guard on the field values we read, and append (never
    # overwrite) history. A concurrent edit that already moved the record away from the
    # values we read fails the filter -> 0 matches -> 409 for the loser.
    res = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "status": old["status"],
            "punch_in": old["punch_in"],
            "punch_out": old["punch_out"],
            "history": record.get("history", []),
        },
        {
            "$set": {k: new[k] for k in new},
            "$push": {"history": history_entry},
        },
        return_document=True,
    )
    if res is None:
        raise HTTPException(409, "record was modified concurrently")
    return serialize_attendance(res)


# --------------------------------------------------------------------------- #
# Analytics month helpers
# --------------------------------------------------------------------------- #
def month_bounds(month: str) -> tuple[str, str, date_cls, date_cls]:
    """Return (first_str, last_str, first_date, last_date) for a YYYY-MM month."""
    y, m = map(int, month.split("-"))
    first = date_cls(y, m, 1)
    if m == 12:
        last = date_cls(y, 12, 31)
    else:
        last = date_cls(y, m + 1, 1) - timedelta(days=1)
    return first.isoformat(), last.isoformat(), first, last


def count_working_days(start: date_cls, end: date_cls) -> int:
    """Mon-Fri count in [start, end] inclusive (R7)."""
    if end < start:
        return 0
    days = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:  # Mon=0 .. Fri=4
            days += 1
        cur += timedelta(days=1)
    return days


# --------------------------------------------------------------------------- #
# Analytics: employee monthly
# --------------------------------------------------------------------------- #
def employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    first_str, last_str, _, _ = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_str, "$lte": last_str}}},
        {"$addFields": {
            # Day-of-week from the "YYYY-MM-DD" string. $dayOfWeek on a parsed date:
            # 1=Sun..7=Sat; weekday = 2..6 (Mon..Fri).
            "_dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "timezone": "UTC"}}},
            "_present": {"$in": ["$status", list(PRESENCE_STATUSES)]},
            "_half": {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
            "_late_min": {"$ifNull": ["$late_minutes", 0]},
            "_ot_min": {"$ifNull": ["$overtime_minutes", 0]},
        }},
        {"$addFields": {"_is_weekday": {"$and": [{"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]}}},
        {"$group": {
            "_id": None,
            # present_days: weekday presence records, half day counts 0.5 (R5-R7).
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": ["$_present", "$_is_weekday"]},
                    {"$cond": ["$_half", 0.5, 1]},
                    0,
                ]
            }},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            # Late and overtime totals include weekend records too (R7).
            "late_count": {"$sum": {"$cond": [{"$gt": ["$_late_min", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$_late_min"},
            "total_overtime_minutes": {"$sum": "$_ot_min"},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")

    _, _, first_date, last_date = month_bounds(month)
    # R7: working days from joined_on when the employee joins mid-month.
    joined = date_cls.fromisoformat(emp["joined_on"])
    wd_start = max(first_date, joined)
    working_days = count_working_days(wd_start, last_date)

    agg = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    row = agg[0] if agg else {}
    present_days = float(row.get("present_days", 0) or 0)
    leave_days = int(row.get("leave_days", 0) or 0)
    late_count = int(row.get("late_count", 0) or 0)
    total_late = int(row.get("total_late_minutes", 0) or 0)
    total_ot = int(row.get("total_overtime_minutes", 0) or 0)

    if working_days == 0:
        attendance_pct = None  # R: null when working_days is 0
    else:
        attendance_pct = half_up(present_days / working_days * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


# --------------------------------------------------------------------------- #
# Analytics: department summary
# --------------------------------------------------------------------------- #
def department_summary_pipeline(month: str, department: Optional[str]) -> list[dict]:
    first_str, last_str, _, _ = month_bounds(month)
    emp_match: dict = {"joined_on": {"$lte": last_str}}  # R9: joined on/before month end
    if department is not None:
        emp_match["department"] = department
    return [
        {"$match": emp_match},
        # Pull this employee's logs for the month (left join; zero-log employees stay).
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"ec": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$ec"]},
                    {"$gte": ["$date", first_str]},
                    {"$lte": ["$date", last_str]},
                ]}}},
                {"$addFields": {
                    "_dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "timezone": "UTC"}}},
                    "_present": {"$in": ["$status", list(PRESENCE_STATUSES)]},
                    "_half": {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                    "_wh": "$work_hours",
                    "_late_min": {"$ifNull": ["$late_minutes", 0]},
                }},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            # headcount counts distinct employees (zero-log employees included).
            "emp_codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$eq": [{"$ifNull": ["$logs._present", False]}, True]},
                        {"$gte": ["$logs._dow", 2]}, {"$lte": ["$logs._dow", 6]},
                    ]},
                    {"$cond": [{"$eq": ["$logs._half", True]}, 0.5, 1]},
                    0,
                ]
            }},
            # avg_work_hours: over presence records with a non-null work_hours (R-contract).
            "_wh_sum": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$eq": [{"$ifNull": ["$logs._present", False]}, True]},
                        {"$ne": [{"$ifNull": ["$logs._wh", None]}, None]},
                    ]},
                    "$logs._wh", 0,
                ]
            }},
            "_wh_cnt": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$eq": [{"$ifNull": ["$logs._present", False]}, True]},
                        {"$ne": [{"$ifNull": ["$logs._wh", None]}, None]},
                    ]},
                    1, 0,
                ]
            }},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs._late_min", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs._late_min", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$sort": {"_id": 1}},  # department ascending
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    rows = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    items = []
    for r in rows:
        headcount = len(r.get("emp_codes", []))
        if headcount == 0:  # omit zero-headcount departments
            continue
        wh_cnt = r.get("_wh_cnt", 0) or 0
        avg_wh = half_up(r["_wh_sum"] / wh_cnt, 2) if wh_cnt > 0 else None
        items.append({
            "department": r["_id"],
            "headcount": headcount,
            "present_days": float(r.get("present_days", 0) or 0),
            "avg_work_hours": avg_wh,
            "late_count": int(r.get("late_count", 0) or 0),
            "total_late_minutes": int(r.get("total_late_minutes", 0) or 0),
            "leave_count": int(r.get("leave_count", 0) or 0),
            "on_duty_count": int(r.get("on_duty_count", 0) or 0),
        })
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics: late leaderboard
# --------------------------------------------------------------------------- #
def late_leaderboard_pipeline(month: str, department: Optional[str]) -> list[dict]:
    first_str, last_str, _, _ = month_bounds(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": first_str, "$lte": last_str}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": 1},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        # Join employee name/department; logs for non-existent employees are dropped.
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    rows = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, department)))
    # Standard competition ranking (1,2,2,4): computed in Python over the already-sorted,
    # already-grouped result (small - one row per late employee), not from array index.
    items = []
    prev_total = None
    rank = 0
    for i, r in enumerate(rows):
        if r["total_late_minutes"] != prev_total:
            rank = i + 1  # skip ranks for ties
            prev_total = r["total_late_minutes"]
        if rank > limit:
            break  # all later rows have equal/greater rank -> excluded
        items.append({
            "rank": rank,
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        })
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics: department trend (gap filling via $densify)
# --------------------------------------------------------------------------- #
def department_trend_pipeline(department: str, date_from: str, date_to: str, emp_codes: list[str], join_dates: dict) -> list[dict]:
    """One row per calendar day in [from, to] with gap filling done in MongoDB via
    $densify. Headcount/attendance_rate/moving_avg are finished in Python because they
    depend on per-day employee headcount (joined_on) which is cheap and clearer here."""
    start_dt = datetime.fromisoformat(date_from + "T00:00:00")
    end_dt = datetime.fromisoformat(date_to + "T00:00:00")
    return [
        # Only this department's logs within range (emp_codes resolved by caller).
        {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": date_from, "$lte": date_to}}},
        {"$addFields": {"_d": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
        {"$group": {
            "_id": "$_d",
            "present_count": {"$sum": {
                "$cond": [
                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                    0,
                ]
            }},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        # Gap-fill every missing calendar day across the full range (not just between
        # existing records). Bounds are explicit so a range with no records still fills.
        {"$densify": {
            "field": "_id",
            "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt + timedelta(days=1)]},
        }},
        {"$match": {"_id": {"$gte": start_dt, "$lte": end_dt}}},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from", pattern=DATE_PATTERN),
    date_to: str = Query(..., alias="to", pattern=DATE_PATTERN),
):
    try:
        d_from = date_cls.fromisoformat(date_from)
        d_to = date_cls.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(422, "from/to must be valid YYYY-MM-DD dates")
    if d_to < d_from:
        raise HTTPException(422, "to must be >= from")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(422, "range exceeds 92 days")

    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")

    emp_codes = [e["emp_code"] for e in emps]
    join_dates = {e["emp_code"]: date_cls.fromisoformat(e["joined_on"]) for e in emps}

    rows = list(db.attendance_logs.aggregate(
        department_trend_pipeline(department, date_from, date_to, emp_codes, join_dates)
    ))
    by_date = {r["_id"].date(): r for r in rows}

    items = []
    recent_rates: list[float] = []  # non-null rates within range for the 7d window
    cur = d_from
    while cur <= d_to:
        agg = by_date.get(cur, {})
        present = float(agg.get("present_count", 0) or 0)
        late = int(agg.get("late_count", 0) or 0)
        is_working = cur.weekday() < 5
        # R9 headcount: department employees who joined on/before this day.
        headcount = sum(1 for jd in join_dates.values() if jd <= cur)

        if is_working and headcount > 0:
            rate = half_up(present / headcount, 4)
        else:
            rate = None  # weekends or zero headcount

        # moving_avg_7d: mean of non-null rates over current + up to 6 preceding rows
        # within range. We maintain a sliding window of the last 7 rows' rates.
        recent_rates.append(rate)
        window = recent_rates[-7:]
        non_null = [x for x in window if x is not None]
        moving = half_up(sum(non_null) / len(non_null), 4) if non_null else None

        items.append({
            "date": cur.isoformat(),
            "is_working_day": is_working,
            "headcount": headcount,
            "present_count": present,
            "late_count": late,
            "attendance_rate": rate,
            "moving_avg_7d": moving,
        })
        cur += timedelta(days=1)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    date_from_alias: Optional[str] = Query(None, alias="from"),
    date_to_alias: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Run MongoDB explain (executionStats) on the real query/pipeline of an endpoint."""
    valid = ("attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend")
    if endpoint not in valid:
        raise HTTPException(422, f"endpoint must be one of {valid}")

    if endpoint == "attendance_list":
        if status is not None and status not in ALL_STATUSES:
            raise HTTPException(422, "invalid status")
        q = build_attendance_query(emp_code, date_from, date_to, status)
        explain = explain_find(
            "attendance_logs", q,
            sort=[("date", DESCENDING), ("emp_code", ASCENDING)],
            skip=(page - 1) * page_size, limit=page_size,
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        explain = explain_aggregate("attendance_logs", employee_monthly_pipeline(emp_code, month))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        explain = explain_aggregate("employees", department_summary_pipeline(month, department))
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        explain = explain_aggregate("attendance_logs", late_leaderboard_pipeline(month, department))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    # department_trend
    d_from = date_from_alias or date_from
    d_to = date_to_alias or date_to
    if not department or not d_from or not d_to:
        raise HTTPException(422, "department, from and to are required")
    try:
        df = date_cls.fromisoformat(d_from)
        dt = date_cls.fromisoformat(d_to)
    except ValueError:
        raise HTTPException(422, "from/to must be valid YYYY-MM-DD dates")
    if dt < df or (dt - df).days + 1 > 92:
        raise HTTPException(422, "invalid date range")
    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(422, "unknown department")
    emp_codes = [e["emp_code"] for e in emps]
    join_dates = {e["emp_code"]: date_cls.fromisoformat(e["joined_on"]) for e in emps}
    explain = explain_aggregate(
        "attendance_logs",
        department_trend_pipeline(department, d_from, d_to, emp_codes, join_dates),
    )
    return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}


def explain_find(collection: str, query: dict, sort=None, skip: int = 0, limit: int = 0) -> dict:
    """Explain a find() using the same filter/sort/pagination as the real endpoint."""
    cmd: dict = {"find": collection, "filter": query}
    if sort:
        cmd["sort"] = {k: (1 if v == ASCENDING else -1) for k, v in sort}
    if skip:
        cmd["skip"] = skip
    if limit:
        cmd["limit"] = limit
    return db.command({"explain": cmd, "verbosity": "executionStats"})


def explain_aggregate(collection: str, pipeline: list[dict]) -> dict:
    """Explain an aggregate() pipeline with executionStats verbosity."""
    return db.command({
        "explain": {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
        "verbosity": "executionStats",
    })
