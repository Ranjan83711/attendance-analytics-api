"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Contract: openapi.yaml (paths, status codes, rules R1-R10). Storage: DATA_MODEL.md.

Conventions inside this file
* Every datetime handled in Python is timezone-aware UTC (the client is created with tz_aware=True).
* Instants cross the HTTP boundary as epoch milliseconds: `to_ms` / `from_ms` are the only converters.
* Money-style rounding (R4, R8) is done with Decimal and ROUND_HALF_UP, never with float round().
"""
import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, Strict, StringConstraints, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)  # a .env file never overrides real environment variables

log = logging.getLogger("attendance")

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,  # BSON dates come back as aware UTC datetimes, not naive ones
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MIN_MS = 100_000_000_000  # smaller values look like seconds (openapi EpochMillis)
MAX_MS = 4_102_444_800_000
GRACE_SECONDS = 10 * 60  # R2
OVERTIME_MIN_MINUTES = 30  # R3
HALF_DAY_BELOW = Decimal("4.50")  # R5
MAX_SHIFT = timedelta(hours=24)
MAX_TREND_DAYS = 92
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
NO_PUNCH = ("ABSENT", "LEAVE")


# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotently)
# --------------------------------------------------------------------------- #
INDEXES = {
    "employees": [
        # identity; makes concurrent duplicate creates fail atomically (409)
        ([("emp_code", ASCENDING)], {"unique": True, "name": "emp_code_unique"}),
        # GET /employees?department= sorted by emp_code; department summary / trend filter by department
        ([("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "department_emp_code"}),
        # department summary headcount without a department filter: joined_on <= end of month (R9)
        ([("joined_on", ASCENDING)], {"name": "joined_on"}),
    ],
    "attendance_logs": [
        # one record per (emp_code, date): the race-safe guard for punch-in; PATCH lookup; employee monthly;
        # the department trend (emp_code $in + date range); GET /attendance?emp_code=
        ([("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True, "name": "emp_code_date_unique"}),
        # GET /attendance default sort (date desc, emp_code asc); month ranges for the summary and leaderboard
        ([("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_desc_emp_code"}),
        # GET /attendance?status= keeps the same sort without scanning other statuses
        ([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "status_date_emp_code"}),
        # late leaderboard: covers its whole $match + $group (date range, late_minutes > 0, emp_code), so the
        # month is answered from the index without fetching documents
        ([("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)], {"name": "date_late_emp_code"}),
        # punch-out: latest punch_in at or before a moment, for one employee
        ([("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "emp_code_punch_in"}),
    ],
}


def ensure_indexes() -> None:
    for coll, specs in INDEXES.items():
        for keys, opts in specs:
            db[coll].create_index(keys, **opts)


def _ensure_indexes_with_retry() -> None:
    while True:
        try:
            ensure_indexes()
            log.info("indexes ready")
            return
        except PyMongoError as exc:  # MongoDB not reachable yet: keep trying in the background
            log.warning("index creation failed (%s); retrying", exc)
            time.sleep(2)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:
        log.warning("index creation deferred: %s", exc)
        threading.Thread(target=_ensure_indexes_with_retry, daemon=True).start()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_, exc: PyMongoError):
    log.error("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


def unprocessable(msg: str, loc: tuple = ("body",)) -> RequestValidationError:
    """A 422 with FastAPI's default validation body ({"detail": [ ... ]})."""
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def from_ms(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: treat a naive value as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc(precision: timedelta = timedelta(seconds=1)) -> datetime:
    now = datetime.now(timezone.utc)
    return EPOCH + ((now - EPOCH) // precision) * precision


def parse_hhmm(value: str) -> dtime:
    h, m = map(int, value.split(":"))
    return dtime(h, m)


def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]  # zero-padded HH:MM compares correctly as text


def shift_moment(att_date: date, hhmm: str) -> datetime:
    """The IST wall-clock time `hhmm` on `att_date`, as an aware datetime."""
    return datetime.combine(att_date, parse_hhmm(hhmm), tzinfo=IST)


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: IST calendar date of the punch-in; overnight shifts own the early hours of the next day."""
    local = punch_in.astimezone(IST)
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        return local.date() - timedelta(days=1)
    return local.date()


def half_up(value: Decimal, places: str) -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- #
# Business rules R2 - R5 (inputs already truncated to whole seconds)
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, emp: dict, att_date: date) -> int:
    """R2: late only if strictly more than 10:00 after shift start; minutes counted from shift start."""
    seconds = int((punch_in - shift_moment(att_date, emp["shift_start"])).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: hours, 2 decimals half-up (exact decimal arithmetic, not float round())."""
    seconds = int((punch_out - punch_in).total_seconds())
    return half_up(Decimal(seconds) / Decimal(3600), "0.01")


def compute_overtime(punch_out: datetime, emp: dict, att_date: date) -> int:
    """R3: whole minutes after shift end (next day for overnight shifts), only if >= 30."""
    end = shift_moment(att_date, emp["shift_end"])
    if is_overnight(emp):
        end += timedelta(days=1)
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def derived_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], emp: dict,
                   att_date: date) -> dict:
    """The four stored derived values for a record (DATA_MODEL.md, section 3)."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {"late_minutes": compute_late_minutes(punch_in, emp, att_date),
           "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        out.update(work_hours=float(hours), overtime_minutes=compute_overtime(punch_out, emp, att_date),
                   half_day=hours < HALF_DAY_BELOW)
    return out


# --------------------------------------------------------------------------- #
# Serialization (MongoDB document -> API shape)
# --------------------------------------------------------------------------- #
def _api_value(v: Any) -> Any:
    return to_ms(v) if isinstance(v, datetime) else v


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "name": doc.get("name"), "email": doc.get("email"),
        "department": doc.get("department"), "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"), "joined_on": doc.get("joined_on"),
        "created_at": to_ms(doc.get("created_at")),
    }


def record_out(doc: dict) -> dict:
    """Normalises legacy gaps: missing history -> [], missing half_day -> false, missing minutes -> 0."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day") or False),
        "history": [
            {
                "at": to_ms(h.get("at")),
                "by": h.get("by"),
                "reason": h.get("reason"),
                "changes": {k: {"from": _api_value(c.get("from")), "to": _api_value(c.get("to"))}
                            for k, c in (h.get("changes") or {}).items()},
            }
            for h in (doc.get("history") or [])
        ],
    }


def dec(v: Any) -> Optional[Decimal]:
    if v is None:
        return None
    if isinstance(v, Decimal128):
        return v.to_decimal()
    return Decimal(str(v))


def num(v: Any) -> Any:
    """Decimal/Decimal128 -> int when whole, else float (JSON friendly)."""
    d = dec(v)
    if d is None:
        return None
    return int(d) if d == d.to_integral_value() else float(d)


# --------------------------------------------------------------------------- #
# Validation types
# --------------------------------------------------------------------------- #
def _valid_date(value: str) -> str:
    date.fromisoformat(value)  # raises ValueError -> 422
    return value


EmpCode = Annotated[str, StringConstraints(pattern=r"^EMP[0-9]{4,6}$")]
HHMM = Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
DateStr = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(_valid_date)]
EpochMs = Annotated[int, Strict(), Field(ge=MIN_MS, le=MAX_MS)]  # strict: rejects floats, strings and bools
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
PageQ = Annotated[int, Query(ge=1)]
PageSizeQ = Annotated[int, Query(ge=1, le=100)]


def parse_date_param(value: Optional[str], name: str) -> Optional[date]:
    if value is None:
        return None
    try:
        if len(value) != 10:
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise unprocessable(f"{name} must be a valid YYYY-MM-DD date", ("query", name))


def month_bounds(month: str) -> tuple[date, date]:
    y, m = map(int, month.split("-"))
    first = date(y, m, 1)
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return first, nxt - timedelta(days=1)


def weekdays_between(start: date, end: date) -> int:
    """Number of Mon-Fri days in [start, end] (R7)."""
    if end < start:
        return 0
    days = (end - start).days + 1
    full_weeks, rest = divmod(days, 7)
    count = full_weeks * 5
    for i in range(rest):
        if (start + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: EmpCode
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    email: Annotated[str, StringConstraints(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def _shift_not_empty(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: EpochMs = None  # omitted -> now; an explicit null is rejected (422)
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMs = None


class RegularizeIn(BaseModel):
    status: Status = None
    punch_in: EpochMs = None
    punch_out: EpochMs = None
    reason: Annotated[str, StringConstraints(min_length=5, max_length=200)]
    regularized_by: Annotated[str, StringConstraints(min_length=1, max_length=50)]


# --------------------------------------------------------------------------- #
# System and employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc(timedelta(milliseconds=1))
    try:
        employees.insert_one(doc)  # the unique index decides; no check-then-insert race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageQ = 1, page_size: PageSizeQ = 20):
    q = {"department": department} if department is not None else {}
    total = employees.count_documents(q)
    cursor = employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


def get_employee_or_404(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    att_date = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derived_fields(body.status, ts, None, emp, att_date),
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent request wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= rec["punch_in"]:
        raise unprocessable("punched_at must be after punch_in", ("body", "punched_at"))
    if ts - rec["punch_in"] > MAX_SHIFT:
        raise unprocessable("punched_at is more than 24 hours after punch_in", ("body", "punched_at"))
    att_date = date.fromisoformat(rec["date"])
    derived = derived_fields(rec["status"], rec["punch_in"], ts, emp, att_date)
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},  # conditional update: only one concurrent punch-out wins
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def attendance_list_query(emp_code, date_from, date_to, status) -> dict:
    df, dt = parse_date_param(date_from, "date_from"), parse_date_param(date_to, "date_to")
    if df and dt and df > dt:
        raise unprocessable("date_from must not be after date_to", ("query", "date_from"))
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if df or dt:
        q["date"] = {}
        if df:
            q["date"]["$gte"] = df.isoformat()
        if dt:
            q["date"]["$lte"] = dt.isoformat()
    if status is not None:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    q = attendance_list_query(emp_code, date_from, date_to, status)
    total = logs.count_documents(q) if q else logs.estimated_document_count()
    cursor = logs.find(q).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


TRACKED = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    att_date = parse_date_param(date, "date")
    emp = get_employee_or_404(emp_code)
    rec = logs.find_one({"emp_code": emp_code, "date": att_date.isoformat()})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    current = record_out(rec)
    old = {
        "status": rec["status"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
        "work_hours": current["work_hours"], "late_minutes": current["late_minutes"],
        "overtime_minutes": current["overtime_minutes"], "half_day": current["half_day"],
    }
    status = body.status if body.status is not None else old["status"]
    if status in NO_PUNCH:
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable(f"{status} clears both punch times; do not send punch_in or punch_out")
        new_in = new_out = None
    else:
        new_in = from_ms(body.punch_in) if body.punch_in is not None else old["punch_in"]
        new_out = from_ms(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if new_in is None:
            raise unprocessable(f"status {status} requires a punch_in", ("body", "punch_in"))
        if body.punch_in is not None and attendance_date(new_in, emp) != att_date:
            raise unprocessable("punch_in must stay on the record's attendance date", ("body", "punch_in"))
        if new_out is not None and not (new_in < new_out <= new_in + MAX_SHIFT):
            raise unprocessable("punch_out must be after punch_in and within 24 hours", ("body", "punch_out"))

    new = {"status": status, "punch_in": new_in, "punch_out": new_out,
           **derived_fields(status, new_in, new_out, emp, att_date)}
    changes = {k: {"from": old[k], "to": new[k]} for k in TRACKED if old[k] != new[k]}
    if not changes:
        raise unprocessable("the request does not change anything")

    entry = {"at": now_utc(timedelta(milliseconds=1)), "by": body.regularized_by, "reason": body.reason,
             "changes": changes}
    # Optimistic concurrency: apply only if nobody changed the record since we read it. The loser gets 409
    # instead of writing a history entry whose "from" values are stale.
    guard = {
        "_id": rec["_id"],
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(current["history"])]},
    }
    updated = logs.find_one_and_update(guard, {"$set": new, "$push": {"history": entry}},
                                       return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "the record was changed concurrently; retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Aggregation building blocks
# --------------------------------------------------------------------------- #
def _weekday(date_field: str, start: date, end: date) -> dict:
    """true when a YYYY-MM-DD string field in [start, end] falls on Mon-Fri. The weekend dates of the range are
    listed up front: a string $in is much cheaper than parsing every date with $dateFromString."""
    weekend = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)
               if (start + timedelta(days=i)).weekday() >= 5]
    return {"$not": [{"$in": [date_field, weekend]}]}


IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
PRESENT_WEIGHT = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}  # R5
LATE = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$gt": [LATE, 0]}


def _round_half_up_expr(expr: Any, places: int) -> dict:
    """Half-up rounding in the database ($round is half-to-even). Works on non-negative decimals; null stays null."""
    factor = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, factor]}, Decimal128("0.5")]}}, factor]}


def employee_monthly_pipeline(emp_code: str, start: date, end: date) -> list:
    present_weekday = {"$and": [IS_PRESENT, _weekday("$date", start, end)]}
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {
            "_id": None,
            # R5-R7: presence status on Mon-Fri, half day counts 0.5
            "present_days": {"$sum": {"$cond": [present_weekday, PRESENT_WEIGHT, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
            "total_late_minutes": {"$sum": LATE},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(start: date, end: date, department: Optional[str]) -> list:
    """Logs of the month -> totals per employee, merged with one row per counted employee -> totals per department."""
    s, e = start.isoformat(), end.isoformat()
    emp_match: dict = {"joined_on": {"$lte": e}}  # R9: joined on or before the end of the month
    if department is not None:
        emp_match["department"] = department
    has_hours = {"$and": [IS_PRESENT, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
    totals = ("p", "late", "late_min", "wh", "wh_n", "leave", "on_duty")
    return [
        {"$match": {"date": {"$gte": s, "$lte": e}}},
        {"$group": {
            "_id": "$emp_code",
            "p": {"$sum": {"$cond": [{"$and": [IS_PRESENT, _weekday("$date", start, end)]}, PRESENT_WEIGHT, 0]}},
            "late": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
            "late_min": {"$sum": {"$cond": [IS_LATE, LATE, 0]}},
            "wh": {"$sum": {"$cond": [has_hours, {"$toDecimal": "$work_hours"}, None]}},
            "wh_n": {"$sum": {"$cond": [has_hours, 1, 0]}},
            "leave": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
        # Every counted employee (R9, department filter) adds one row carrying its department, so employees with
        # no logs still reach the headcount. Groups without such a row (future joiners, other departments,
        # logs of unknown employees) are dropped. No per-row $lookup needed.
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": emp_match},
            {"$project": {"_id": "$emp_code", "department": 1, "counted": {"$literal": 1}}},
        ]}},
        {"$group": {"_id": "$_id", "department": {"$max": "$department"}, "counted": {"$max": "$counted"},
                    **{k: {"$sum": f"${k}"} for k in totals}}},
        {"$match": {"counted": 1}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$p"},
            "wh_sum": {"$sum": "$wh"},
            "wh_n": {"$sum": "$wh_n"},  # average over records, not an average of per-employee averages
            "late_count": {"$sum": "$late"},
            "total_late_minutes": {"$sum": "$late_min"},
            "leave_count": {"$sum": "$leave"},
            "on_duty_count": {"$sum": "$on_duty"},
        }},
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(start: date, end: date, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # drops logs whose employee does not exist
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # Standard competition ranking (1, 2, 2, 4): ties share a rank, the next rank is skipped.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking, so ties at the cutoff are kept
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def department_trend_pipeline(department: str, emp_codes: list, start: date, end: date) -> list:
    s, e = start.isoformat(), end.isoformat()
    day = {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d", "timezone": "UTC"}}
    lower = datetime.combine(start, dtime(), tzinfo=timezone.utc)
    upper = datetime.combine(end + timedelta(days=1), dtime(), tzinfo=timezone.utc)  # $densify upper is exclusive
    rate = {"$cond": [
        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
        _round_half_up_expr({"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]}, 4),
        None,
    ]}
    return [
        # The department's logs in the range: one index scan over (emp_code, date) ranges.
        {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": s, "$lte": e}}},
        {"$project": {"_id": 0, "date": 1, "hc": {"$literal": 0},
                      "p": {"$cond": [IS_PRESENT, PRESENT_WEIGHT, 0]},
                      "late": {"$cond": [IS_LATE, 1, 0]}}},
        # Headcount events from the employees: an anchor row on the first day (so the range is never empty) and a
        # +1 on the day each employee starts counting (R9; people who joined before the range count from day one).
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": {"department": department}},
            {"$project": {"_id": 0, "events": {"$concatArrays": [
                [{"date": s, "hc": 0, "p": 0, "late": 0}],
                {"$cond": [{"$lte": ["$joined_on", e]},
                           [{"date": {"$max": ["$joined_on", s]}, "hc": 1, "p": 0, "late": 0}], []]},
            ]}}},
            {"$unwind": "$events"},
            {"$replaceWith": "$events"},
        ]}},
        {"$group": {"_id": "$date", "joined": {"$sum": "$hc"}, "present_count": {"$sum": "$p"},
                    "late_count": {"$sum": "$late"}}},
        {"$addFields": {"day": day}},
        # Gap filling: exactly one row per calendar day in [from, to].
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"headcount": {
            "$sum": {"$ifNull": ["$joined", 0]}, "window": {"documents": ["unbounded", "current"]}}}}},
        {"$addFields": {
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$addFields": {"attendance_rate": rate}},
        # 7-day moving average over the rows inside the range only; $avg skips nulls, all-null -> null.
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"ma": {
            "$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1,
            "moving_avg_7d": _round_half_up_expr("$ma", 4),
        }},
        {"$sort": {"date": 1}},
    ]


def department_codes(department: str) -> list:
    return employees.distinct("emp_code", {"department": department})


def trend_range(from_: str, to: str) -> tuple[date, date]:
    start, end = parse_date_param(from_, "from"), parse_date_param(to, "to")
    if end < start:
        raise unprocessable("to must not be before from", ("query", "to"))
    if (end - start).days + 1 > MAX_TREND_DAYS:
        raise unprocessable(f"the range must not exceed {MAX_TREND_DAYS} days", ("query", "to"))
    return start, end


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Annotated[str, Query(pattern=MONTH_PATTERN)]):
    emp = get_employee_or_404(emp_code)
    start, end = month_bounds(month)
    rows = list(logs.aggregate(employee_monthly_pipeline(emp_code, start, end)))
    agg = rows[0] if rows else {}
    joined = date.fromisoformat(emp["joined_on"])
    working_days = weekdays_between(max(start, joined), end)  # R7: counted from joined_on for joiners
    present = dec(agg.get("present_days", 0))
    pct = None if working_days == 0 else float(half_up(present / working_days * 100, "0.01"))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": num(present),
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Annotated[str, Query(pattern=MONTH_PATTERN)], department: Optional[str] = None):
    start, end = month_bounds(month)
    items = []
    for row in logs.aggregate(department_summary_pipeline(start, end, department)):
        wh_n = row["wh_n"]
        avg = None if not wh_n else float(half_up(dec(row["wh_sum"]) / wh_n, "0.01"))
        items.append({
            "department": row["_id"],
            "headcount": row["headcount"],
            "present_days": num(row["present_days"]),
            "avg_work_hours": avg,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Annotated[str, Query(pattern=MONTH_PATTERN)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    start, end = month_bounds(month)
    items = list(logs.aggregate(late_leaderboard_pipeline(start, end, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[str, Query(alias="from")],
    to: str,
):
    start, end = trend_range(from_, to)
    codes = department_codes(department)
    if not codes:
        raise HTTPException(404, "unknown department")
    items = []
    for row in logs.aggregate(department_trend_pipeline(department, codes, start, end)):
        items.append({
            "date": row["date"],
            "is_working_day": row["is_working_day"],
            "headcount": row["headcount"],
            "present_count": num(row["present_count"]),
            "late_count": row["late_count"],
            "attendance_rate": None if row["attendance_rate"] is None else float(dec(row["attendance_rate"])),
            "moving_avg_7d": None if row["moving_avg_7d"] is None else float(dec(row["moving_avg_7d"])),
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
class ExplainTarget(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


def _require(value: Optional[str], name: str) -> str:
    if value is None:
        raise unprocessable(f"{name} is required for this endpoint", ("query", name))
    return value


def _month(value: Optional[str]) -> tuple[date, date]:
    month = _require(value, "month")
    if not re.fullmatch(MONTH_PATTERN, month):
        raise unprocessable("month must be YYYY-MM", ("query", "month"))
    return month_bounds(month)


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    """Runs explain(executionStats) on the same query/pipeline the endpoint builds."""
    if endpoint is ExplainTarget.attendance_list:
        q = attendance_list_query(emp_code, date_from, date_to, status)
        coll = "attendance_logs"
        cmd = {"find": coll, "filter": q, "sort": dict(ATTENDANCE_SORT),
               "skip": (page - 1) * page_size, "limit": page_size}
    else:
        if endpoint is ExplainTarget.employee_monthly:
            start, end = _month(month)
            coll, pipeline = "attendance_logs", employee_monthly_pipeline(_require(emp_code, "emp_code"), start, end)
        elif endpoint is ExplainTarget.department_summary:
            start, end = _month(month)
            coll, pipeline = "attendance_logs", department_summary_pipeline(start, end, department)
        elif endpoint is ExplainTarget.late_leaderboard:
            start, end = _month(month)
            coll, pipeline = "attendance_logs", late_leaderboard_pipeline(start, end, limit, department)
        else:
            dept = _require(department, "department")
            start, end = trend_range(_require(from_, "from"), _require(to, "to"))
            coll, pipeline = "attendance_logs", department_trend_pipeline(dept, department_codes(dept), start, end)
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}
    raw = db.command("explain", cmd, verbosity="executionStats")
    plain = json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint.value, "collection": coll, "explain": plain}
