"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (real environment variables take priority over .env)
"""
import calendar
import os
import re
from bisect import bisect_right
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()  # override=False by default: real environment variables win

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: datetimes come back from MongoDB as UTC-aware datetimes,
# so every comparison below is between aware datetimes. No naive/aware mix.
client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
ALL_STATUSES = ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

# Epoch-millis bounds from the contract: seconds-looking values, floats and
# strings are rejected with 422 (StrictInt + range does this).
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000

MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


# ---------------------------------------------------------------------------
# Helpers: time, business rules R1-R5
# ---------------------------------------------------------------------------
def trunc_sec(dt: datetime) -> datetime:
    """R1: instants are truncated to whole seconds before they are stored or
    used in any calculation."""
    return dt.replace(microsecond=0)


def dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def ms_to_dt(ms: int) -> datetime:
    """Epoch millis -> aware UTC datetime, truncated to whole seconds (R1)."""
    return trunc_sec(datetime.fromtimestamp(ms / 1000, tz=UTC))


def round_half_up(value: float, places: int) -> float:
    """R8: half-up rounding (Decimal), never Python's banker's round()."""
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def parse_shift(t: str) -> tuple:
    h, m = t.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date of the punch-in. Exception for overnight
    shifts (shift_end <= shift_start): a punch-in whose IST time is earlier
    than shift_end belongs to the shift that started the previous day."""
    ist = punch_in_utc.astimezone(IST)
    if is_overnight(shift_start, shift_end):
        eh, em = parse_shift(shift_end)
        if (ist.hour, ist.minute, ist.second) < (eh, em, 0):
            ist -= timedelta(days=1)
    return ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str) -> int:
    """R2: grace period is 10 minutes. Late only when the punch-in is strictly
    more than 10:00 after shift_start; then whole minutes (floor) elapsed
    since shift_start. Otherwise 0."""
    ist = punch_in_utc.astimezone(IST)
    h, m = parse_shift(shift_start)
    start = ist.replace(hour=h, minute=m, second=0, microsecond=0)
    delta = (ist - start).total_seconds()
    if delta > 10 * 60:
        return int(delta // 60)
    return 0


def shift_end_dt(att_date: str, shift_start: str, shift_end: str) -> datetime:
    """Shift end as an IST datetime. For overnight shifts it falls on the next
    calendar day (R1)."""
    y, mo, d = (int(x) for x in att_date.split("-"))
    h, mi = parse_shift(shift_end)
    dt = datetime(y, mo, d, h, mi, tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        dt += timedelta(days=1)
    return dt


def compute_overtime_minutes(punch_out_utc: datetime, att_date: str,
                             shift_start: str, shift_end: str) -> int:
    """R3: whole minutes (floor) between shift_end and punch_out, counted only
    when that is >= 30; otherwise 0."""
    mins = int((punch_out_utc - shift_end_dt(att_date, shift_start, shift_end)
                ).total_seconds() // 60)
    return mins if mins >= 30 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in hours, rounded half-up to 2 decimals.
    Computed from integer millis so the Decimal arithmetic is exact."""
    ms = dt_to_ms(punch_out_utc) - dt_to_ms(punch_in_utc)
    return float((Decimal(ms) / Decimal(3600000)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP))


def derived_fields(status: str, punch_in_utc, punch_out_utc,
                   att_date: str, shift_start: str, shift_end: str) -> dict:
    """Recompute the four stored derived values from the final times (R2-R5).
    Used by punch-in, punch-out and every regularization."""
    if status in ("ABSENT", "LEAVE"):
        return {"work_hours": None, "late_minutes": 0,
                "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in_utc, shift_start)
    if punch_out_utc is None:
        return {"work_hours": None, "late_minutes": late,
                "overtime_minutes": 0, "half_day": False}
    wh = compute_work_hours(punch_in_utc, punch_out_utc)
    return {
        "work_hours": wh,
        "late_minutes": late,
        "overtime_minutes": compute_overtime_minutes(
            punch_out_utc, att_date, shift_start, shift_end),
        "half_day": wh < 4.50,  # R5: on the rounded work_hours
    }


# ---------------------------------------------------------------------------
# Serializers: MongoDB <-> API boundary
# ---------------------------------------------------------------------------
def _conv_ts(value):
    return dt_to_ms(value) if isinstance(value, datetime) else value


def record_out(doc: dict) -> dict:
    """Mongo attendance doc -> API AttendanceRecord. Epoch millis at the
    boundary (including nested history changes), no _id anywhere."""
    d = dict(doc)
    d.pop("_id", None)
    d["punch_in"] = _conv_ts(d.get("punch_in"))
    d["punch_out"] = _conv_ts(d.get("punch_out"))
    history = []
    for h in d.get("history") or []:
        changes = {}
        for fk, ch in (h.get("changes") or {}).items():
            changes[fk] = {"from": _conv_ts(ch.get("from")),
                           "to": _conv_ts(ch.get("to"))}
        history.append({"at": _conv_ts(h.get("at")), "by": h.get("by"),
                        "reason": h.get("reason"), "changes": changes})
    d["history"] = history
    d.setdefault("late_minutes", 0)
    d.setdefault("overtime_minutes", 0)
    d.setdefault("half_day", False)
    return d


def employee_out(doc: dict) -> dict:
    d = {k: doc[k] for k in ("emp_code", "name", "email", "department",
                             "shift_start", "shift_end", "joined_on")}
    d["created_at"] = dt_to_ms(doc["created_at"])
    return d


# ---------------------------------------------------------------------------
# App + startup indexes
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # All indexes are created here, idempotently, at startup.
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)])
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0",
              lifespan=lifespan)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=120,
                       pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30",
                             pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30",
                           pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def _joined_on_calendar_valid(cls, v: str) -> str:
        date.fromisoformat(v)  # raises ValueError -> 422 on 2026-13-40 etc.
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None, ge=EPOCH_MIN, le=EPOCH_MAX)


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH",
                             "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(
        default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    punch_out: Optional[StrictInt] = Field(
        default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# ---------------------------------------------------------------------------
# Shared query builders (endpoints and /admin/explain build the same objects)
# ---------------------------------------------------------------------------
def month_bounds(month: str):
    y, mo = (int(x) for x in month.split("-"))
    start = date(y, mo, 1)
    end = date(y, mo, calendar.monthrange(y, mo)[1])
    return start.isoformat(), end.isoformat()


def attendance_list_query(emp_code=None, date_from=None, date_to=None,
                          status=None):
    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


def employee_monthly_pipeline(emp_code: str, mstart: str, mend: str):
    return [
        {"$match": {"emp_code": emp_code,
                    "date": {"$gte": mstart, "$lte": mend}}},
        {"$addFields": {"dow": {"$dayOfWeek":
                                {"$dateFromString": {"dateString": "$date"}}}}},
        {"$group": {
            "_id": None,
            # present_days: presence status, Mon-Fri only (R5-R7); half day = 0.5
            "present_days": {"$sum": {"$cond": [{"$and": [
                {"$in": ["$status", PRESENT_STATUSES]},
                {"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [
                {"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            # late/overtime totals read the STORED values over every record,
            # weekends included (R7)
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum":
                                       {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(all_codes: list, mstart: str, mend: str):
    return [
        {"$match": {"emp_code": {"$in": all_codes},
                    "date": {"$gte": mstart, "$lte": mend}}},
        {"$addFields": {"dow": {"$dayOfWeek":
                                {"$dateFromString": {"dateString": "$date"}}}}},
        {"$group": {
            "_id": "$emp_code",
            "present_days": {"$sum": {"$cond": [{"$and": [
                {"$in": ["$status", PRESENT_STATUSES]},
                {"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
            # avg over records (sum + count; null work_hours excluded)
            "wh_sum": {"$sum": {"$cond": [{"$and": [
                {"$in": ["$status", PRESENT_STATUSES]},
                {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]},
                "$work_hours", 0]}},
            "wh_n": {"$sum": {"$cond": [{"$and": [
                {"$in": ["$status", PRESENT_STATUSES]},
                {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [
                {"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [
                {"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]


def late_leaderboard_pipeline(match: dict, limit: int):
    return [
        {"$match": match},
        {"$group": {"_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        # Standard competition ranking: ties share a rank, next rank skipped
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}}}},
        # limit applies AFTER ranking: ties at the cutoff are all returned
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ]


def department_trend_pipeline(codes: list, from_date: str, to_date: str,
                              from_dt: datetime, to_excl: datetime):
    return [
        {"$match": {"emp_code": {"$in": codes},
                    "date": {"$gte": from_date, "$lte": to_date}}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", PRESENT_STATUSES]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        # Gap-filling in the database: exactly one row per day, weekends and
        # empty days included.
        {"$densify": {"field": "day", "range": {
            "step": 1, "unit": "day", "bounds": [from_dt, to_excl]}}},
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "is_working_day": {"$and": [
                {"$gte": [{"$dayOfWeek": "$day"}, 2]},
                {"$lte": [{"$dayOfWeek": "$day"}, 6]}]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$sort": {"day": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1,
                      "present_count": 1, "late_count": 1}},
    ]


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    # 200 only when MongoDB answers a ping, otherwise 503.
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")
    doc = body.model_dump()
    doc["created_at"] = trunc_sec(datetime.now(UTC))
    try:
        # The unique index (not the check below) is what makes concurrent
        # duplicate creates yield exactly one 201 and the rest 409.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None,
                   page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)):
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    cur = (db.employees.find(q, {"_id": 0})
           .sort("emp_code", ASCENDING)
           .skip((page - 1) * page_size).limit(page_size))
    return {"items": [employee_out(d) for d in cur],
            "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# Attendance: punch in / punch out / list / regularize
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    ts = (ms_to_dt(body.punched_at) if body.punched_at is not None
          else trunc_sec(datetime.now(UTC)))
    att_date = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    derived = derived_fields(body.status, ts, None, att_date,
                             emp["shift_start"], emp["shift_end"])
    doc = {"emp_code": body.emp_code, "date": att_date, "status": body.status,
           "punch_in": ts, "punch_out": None, "history": [], **derived}
    try:
        # Unique index on (emp_code, date): two simultaneous punch-ins for the
        # same attendance date yield exactly one 201 and the rest 409. The
        # pre-check is intentionally omitted (check-then-insert races).
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    ts = (ms_to_dt(body.punched_at) if body.punched_at is not None
          else trunc_sec(datetime.now(UTC)))
    # The most recent record whose punch_in is at or before punched_at. This
    # is what makes overnight shifts (punch-out on the next calendar day)
    # work. Records with no punch_in (ABSENT/LEAVE) can never match.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code,
         "punch_in": {"$lte": ts, "$ne": None}},
        sort=[("punch_in", DESCENDING)])
    if not rec:
        raise HTTPException(404, "no punch-in found")
    pin = rec["punch_in"]
    if ts <= pin:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (ts - pin).total_seconds() > 24 * 3600:
        raise HTTPException(422,
                            "punched_at more than 24 hours after punch_in")
    derived = derived_fields(rec["status"], pin, ts, rec["date"],
                             emp["shift_start"], emp["shift_end"])
    # Atomic close: the filter punch_out=None means two simultaneous
    # punch-outs yield exactly one 200 and the other 409.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
        return_document=True)
    if not updated:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
        emp_code: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH",
                                 "ON_DUTY"]] = None,
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100)):
    for label, v in (("date_from", date_from), ("date_to", date_to)):
        if v is not None:
            try:
                date.fromisoformat(v)
            except ValueError:
                raise HTTPException(422, f"{label} must be YYYY-MM-DD")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q = attendance_list_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    # Sorted and paginated in the database; the collection is never loaded
    # into memory.
    cur = (db.attendance_logs.find(q)
           .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
           .skip((page - 1) * page_size).limit(page_size))
    return {"items": [record_out(d) for d in cur],
            "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{day}")
def regularize_attendance(emp_code: str, day: str, body: RegularizeIn):
    try:
        date.fromisoformat(day)
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if not rec:
        raise HTTPException(404, "no record for that date")

    new_status = body.status or rec["status"]
    new_pin = (ms_to_dt(body.punch_in) if body.punch_in is not None
               else rec.get("punch_in"))
    new_pout = (ms_to_dt(body.punch_out) if body.punch_out is not None
                else rec.get("punch_out"))

    if new_status in ("ABSENT", "LEAVE"):
        # Setting ABSENT/LEAVE clears both punch times...
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                422, "cannot supply punch times with ABSENT/LEAVE")
        new_pin, new_pout = None, None
    else:
        # ...while a presence status always requires a punch_in.
        if new_pin is None:
            raise HTTPException(422, "presence status requires punch_in")
        # The punch_in must stay on the record's attendance date (R1).
        if attendance_date(new_pin, emp["shift_start"],
                           emp["shift_end"]) != day:
            raise HTTPException(
                422, "punch_in must stay on the record's attendance date")
    if new_pin is not None and new_pout is not None:
        if new_pout <= new_pin:
            raise HTTPException(422, "punch_out must be after punch_in")
        if (new_pout - new_pin).total_seconds() > 24 * 3600:
            raise HTTPException(
                422, "punch_out more than 24 hours after punch_in")

    derived = derived_fields(new_status, new_pin, new_pout, day,
                             emp["shift_start"], emp["shift_end"])
    new_values = {"status": new_status, "punch_in": new_pin,
                  "punch_out": new_pout, **derived}
    old_values = {"status": rec["status"],
                  "punch_in": rec.get("punch_in"),
                  "punch_out": rec.get("punch_out"),
                  "work_hours": rec.get("work_hours"),
                  "late_minutes": rec.get("late_minutes", 0),
                  "overtime_minutes": rec.get("overtime_minutes", 0),
                  "half_day": rec.get("half_day", False)}
    changes = {k: {"from": old_values[k], "to": new_values[k]}
               for k in old_values if old_values[k] != new_values[k]}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    entry = {"at": trunc_sec(datetime.now(UTC)), "by": body.regularized_by,
             "reason": body.reason, "changes": changes}
    # One atomic update: $set the corrected fields and $push the history
    # entry together, so concurrent edits cannot silently lose each other's
    # history entries.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"]},
        {"$set": new_values, "$push": {"history": entry}},
        return_document=True)
    return record_out(updated)


# ---------------------------------------------------------------------------
# Analytics (MongoDB aggregation pipelines)
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str,
                     month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    mstart, mend = month_bounds(month)
    # R7: Mon-Fri; for mid-month joiners, working days start at joined_on.
    joined = date.fromisoformat(emp["joined_on"])
    eff_start = max(date.fromisoformat(mstart), joined)
    eff_end = date.fromisoformat(mend)
    if eff_start > eff_end:
        working_days = 0
    else:
        working_days = sum(
            1 for n in range((eff_end - eff_start).days + 1)
            if (eff_start + timedelta(days=n)).weekday() < 5)
    rows = list(db.attendance_logs.aggregate(
        employee_monthly_pipeline(emp_code, mstart, mend)))
    agg = rows[0] if rows else {}
    present = agg.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present,
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        # R8; null when there are no working days
        "attendance_pct": (round_half_up(present / working_days * 100, 2)
                           if working_days else None),
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN),
                       department: Optional[str] = None):
    mstart, mend = month_bounds(month)
    emp_q: Dict[str, Any] = {"joined_on": {"$lte": mend}}
    if department:
        emp_q["department"] = department
    # R9: headcount comes from employees (joined_on <= month end), so people
    # with zero logs still count and later joiners do not.
    dept_codes: Dict[str, List[str]] = {}
    for e in db.employees.find(emp_q, {"_id": 0, "emp_code": 1,
                                       "department": 1}):
        dept_codes.setdefault(e["department"], []).append(e["emp_code"])
    if not dept_codes:
        return {"month": month, "items": []}
    all_codes = [c for codes in dept_codes.values() for c in codes]
    per_emp = {r["_id"]: r for r in db.attendance_logs.aggregate(
        department_summary_pipeline(all_codes, mstart, mend))}
    items = []
    for dept in sorted(dept_codes):
        tot = {"present_days": 0, "wh_sum": 0, "wh_n": 0, "late_count": 0,
               "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0}
        for c in dept_codes[dept]:
            r = per_emp.get(c)
            if r:
                for k in tot:
                    tot[k] += r.get(k, 0)
        items.append({
            "department": dept,
            "headcount": len(dept_codes[dept]),
            "present_days": tot["present_days"],
            # mean over records with a work_hours value (R8), null if none
            "avg_work_hours": (round_half_up(tot["wh_sum"] / tot["wh_n"], 2)
                               if tot["wh_n"] else None),
            "late_count": tot["late_count"],
            "total_late_minutes": tot["total_late_minutes"],
            "leave_count": tot["leave_count"],
            "on_duty_count": tot["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=MONTH_PATTERN),
                     limit: int = Query(10, ge=1, le=50),
                     department: Optional[str] = None):
    mstart, mend = month_bounds(month)
    match: Dict[str, Any] = {"date": {"$gte": mstart, "$lte": mend},
                             "late_minutes": {"$gt": 0}}
    if department:
        # Ranking happens inside the department only.
        codes = [e["emp_code"] for e in db.employees.find(
            {"department": department}, {"_id": 0, "emp_code": 1})]
        if not codes:
            return {"month": month, "items": []}
        match["emp_code"] = {"$in": codes}
    rows = list(db.attendance_logs.aggregate(
        late_leaderboard_pipeline(match, limit)))
    emp_map = {e["emp_code"]: e for e in db.employees.find(
        {"emp_code": {"$in": [r["_id"] for r in rows]}},
        {"_id": 0, "emp_code": 1, "name": 1, "department": 1})} if rows else {}
    items = []
    for r in rows:
        e = emp_map.get(r["_id"])
        if e is None:
            continue  # logs whose employee does not exist are ignored
        items.append({"rank": r["rank"], "emp_code": r["_id"],
                      "name": e["name"], "department": e["department"],
                      "total_late_minutes": r["total_late_minutes"],
                      "late_count": r["late_count"]})
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str,
                     from_date: str = Query(..., alias="from"),
                     to_date: str = Query(..., alias="to")):
    try:
        f, t = date.fromisoformat(from_date), date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(422, "from/to must be YYYY-MM-DD")
    if t < f:
        raise HTTPException(422, "to must not be before from")
    if (t - f).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    emps = list(db.employees.find(
        {"department": department},
        {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")
    codes = [e["emp_code"] for e in emps]
    joined_sorted = sorted(e["joined_on"] for e in emps)
    from_dt = datetime(f.year, f.month, f.day)
    to_excl = datetime(t.year, t.month, t.day) + timedelta(days=1)
    rows = list(db.attendance_logs.aggregate(
        department_trend_pipeline(codes, from_date, to_date,
                                  from_dt, to_excl)))
    # R9 headcount per day via the sorted joined_on list; rates half-up 4dp.
    items, rates = [], []
    for r in rows:
        headcount = bisect_right(joined_sorted, r["date"])
        rate = (round_half_up(r["present_count"] / headcount, 4)
                if r["is_working_day"] and headcount > 0 else None)
        rates.append(rate)
        items.append({"date": r["date"],
                      "is_working_day": r["is_working_day"],
                      "headcount": headcount,
                      "present_count": r["present_count"],
                      "late_count": r["late_count"],
                      "attendance_rate": rate,
                      "moving_avg_7d": None})
    # 7-day moving average over non-null rates inside the requested range;
    # the first rows use a shorter window.
    for i, it in enumerate(items):
        window = [x for x in rates[max(0, i - 6):i + 1] if x is not None]
        it["moving_avg_7d"] = (round_half_up(sum(window) / len(window), 4)
                               if window else None)
    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Admin: explain
# ---------------------------------------------------------------------------
EXPLAINABLE = {"attendance_list", "employee_monthly", "department_summary",
               "late_leaderboard", "department_trend"}


def _explain(command: dict) -> dict:
    return db.command({"explain": command, "verbosity": "executionStats"})


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
        endpoint: str,
        emp_code: Optional[str] = None,
        month: Optional[str] = Query(default=None, pattern=MONTH_PATTERN),
        department: Optional[str] = None,
        limit: int = Query(10, ge=1, le=50),
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH",
                                 "ON_DUTY"]] = None,
        from_date: Optional[str] = Query(default=None, alias="from"),
        to_date: Optional[str] = Query(default=None, alias="to"),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100)):
    if endpoint not in EXPLAINABLE:
        raise HTTPException(422, "unknown endpoint")

    if endpoint == "attendance_list":
        q = attendance_list_query(emp_code, date_from, date_to, status)
        skip = (page - 1) * page_size
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": _explain(
                    {"find": "attendance_logs", "filter": q,
                     "sort": {"date": -1, "emp_code": 1},
                     "skip": skip, "limit": page_size})}

    if month is None:
        raise HTTPException(422, "month is required")
    mstart, mend = month_bounds(month)

    if endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code is required")
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": _explain(
                    {"aggregate": "attendance_logs",
                     "pipeline": employee_monthly_pipeline(
                         emp_code, mstart, mend),
                     "cursor": {}})}

    if endpoint == "department_summary":
        emp_q: Dict[str, Any] = {"joined_on": {"$lte": mend}}
        if department:
            emp_q["department"] = department
        codes = [e["emp_code"] for e in db.employees.find(emp_q)]
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": _explain(
                    {"aggregate": "attendance_logs",
                     "pipeline": department_summary_pipeline(
                         codes, mstart, mend),
                     "cursor": {}})}

    if endpoint == "late_leaderboard":
        match: Dict[str, Any] = {"date": {"$gte": mstart, "$lte": mend},
                                 "late_minutes": {"$gt": 0}}
        if department:
            match["emp_code"] = {"$in": [
                e["emp_code"] for e in db.employees.find(
                    {"department": department})]}
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": _explain(
                    {"aggregate": "attendance_logs",
                     "pipeline": late_leaderboard_pipeline(match, limit),
                     "cursor": {}})}

    # department_trend
    if not department or not from_date or not to_date:
        raise HTTPException(422,
                            "department, from and to are required")
    try:
        f, t = date.fromisoformat(from_date), date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(422, "from/to must be YYYY-MM-DD")
    if t < f or (t - f).days + 1 > 92:
        raise HTTPException(422, "invalid range")
    codes = [e["emp_code"] for e in db.employees.find(
        {"department": department})]
    from_dt = datetime(f.year, f.month, f.day)
    to_excl = datetime(t.year, t.month, t.day) + timedelta(days=1)
    return {"endpoint": endpoint, "collection": "attendance_logs",
            "explain": _explain(
                {"aggregate": "attendance_logs",
                 "pipeline": department_trend_pipeline(
                     codes, from_date, to_date, from_dt, to_excl),
                 "cursor": {}})}
