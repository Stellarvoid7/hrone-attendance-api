"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for dev convenience)
"""
import os
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}


# ── startup ──────────────────────────────────────────────────────────────────

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.employees.create_index("joined_on")

    # primary lookup key for attendance
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    # list endpoint sort: date desc, emp_code asc
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    # analytics filters
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING)]
    )
    db.attendance_logs.create_index("emp_code")


# ── helpers ───────────────────────────────────────────────────────────────────

def rh(val, places=2):
    """Round half-up to `places` decimal places, return float."""
    q = Decimal(10) ** -places
    return float(Decimal(str(val)).quantize(q, rounding=ROUND_HALF_UP))


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms → UTC-aware datetime, truncated to whole seconds."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: returns the IST calendar date string for this punch-in.
    For overnight shifts (shift_end <= shift_start), if the IST punch time
    is earlier than shift_end, the record belongs to the *previous* IST day.
    """
    pi_ist = punch_in_utc.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    overnight = (eh * 60 + em) <= (sh * 60 + sm)
    if overnight:
        punch_mins = pi_ist.hour * 60 + pi_ist.minute
        end_mins = eh * 60 + em
        if punch_mins < end_mins:
            # this punch is on the tail of the previous day's shift
            return (pi_ist.date() - timedelta(days=1)).isoformat()
    return pi_ist.date().isoformat()


def calc_late(punch_in_utc: datetime, shift_start: str, att_date: str) -> int:
    """R2: grace = 10 min strict. Returns floor minutes from shift_start."""
    sh, sm = map(int, shift_start.split(":"))
    base = datetime.fromisoformat(att_date).replace(
        hour=sh, minute=sm, second=0, microsecond=0, tzinfo=IST
    )
    pi_ist = punch_in_utc.astimezone(IST)
    diff_secs = int((pi_ist - base).total_seconds())
    if diff_secs > 600:
        return diff_secs // 60
    return 0


def calc_work_hours(punch_in: datetime, punch_out: datetime):
    secs = (punch_out - punch_in).total_seconds()
    return rh(secs / 3600, 2)


def calc_overtime(punch_out_utc: datetime, shift_end: str, shift_start: str, att_date: str) -> int:
    """R3: OT only if >= 30 min past shift_end.
    For overnight shifts shift_end falls on the next calendar day.
    """
    eh, em = map(int, shift_end.split(":"))
    sh, sm = map(int, shift_start.split(":"))
    base_date = datetime.fromisoformat(att_date)
    # overnight: shift_end <= shift_start means end wraps to next day
    if (eh * 60 + em) <= (sh * 60 + sm):
        base_date = base_date + timedelta(days=1)
    base = base_date.replace(hour=eh, minute=em, second=0, microsecond=0, tzinfo=IST)
    po_ist = punch_out_utc.astimezone(IST)
    mins = int((po_ist - base).total_seconds() / 60)
    return mins if mins >= 30 else 0


def fmt_log(doc: dict) -> dict:
    """Convert a MongoDB attendance doc to API shape (ms timestamps)."""
    out = {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc["punch_in"]) if doc.get("punch_in") else None,
        "punch_out": dt_to_ms(doc["punch_out"]) if doc.get("punch_out") else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": [],
    }
    for h in doc.get("history", []):
        entry = {
            "at": dt_to_ms(h["at"]),
            "by": h["by"],
            "reason": h["reason"],
            "changes": {},
        }
        for field, chg in h.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                entry["changes"][field] = {
                    "from": dt_to_ms(chg["from"]) if chg["from"] else None,
                    "to": dt_to_ms(chg["to"]) if chg["to"] else None,
                }
            else:
                entry["changes"][field] = {"from": chg["from"], "to": chg["to"]}
        out["history"].append(entry)
    return out


def fmt_emp(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


def working_days_in_month(year: int, month: int, joined: str) -> int:
    """R7: Mon-Fri days in the month on or after joined_on."""
    joined_date = date.fromisoformat(joined)
    first = date(year, month, 1)
    # last day of month
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    start = max(first, joined_date)
    if start > last:
        return 0
    count = 0
    d = start
    while d <= last:
        if d.weekday() < 5:  # Mon-Fri
            count += 1
        d += timedelta(days=1)
    return count


# ── models ────────────────────────────────────────────────────────────────────

class EmployeeCreate(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @model_validator(mode="after")
    def check_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInReq(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_ms(cls, v):
        if v is not None and v < 100_000_000_000:
            raise ValueError("punched_at must be epoch milliseconds")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v not in PRESENCE:
            raise ValueError(f"status must be one of {PRESENCE}")
        return v


class PunchOutReq(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_ms(cls, v):
        if v is not None and v < 100_000_000_000:
            raise ValueError("punched_at must be epoch milliseconds")
        return v


class RegularizeReq(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_ms(cls, v):
        if v is not None and v < 100_000_000_000:
            raise ValueError("must be epoch milliseconds")
        return v


# ── endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        return JSONResponse(status_code=503, content={"detail": "MongoDB unavailable"})


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    return fmt_emp(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = [
        fmt_emp(d)
        for d in db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInReq):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = ms_to_dt(body.punched_at) if body.punched_at else datetime.now(tz=timezone.utc)
    # truncate to whole seconds (R1)
    ts = ts.replace(microsecond=0)

    att_date = attendance_date(ts, emp["shift_start"], emp["shift_end"])

    late = calc_late(ts, emp["shift_start"], att_date)
    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    doc.pop("_id", None)
    return fmt_log(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutReq):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = ms_to_dt(body.punched_at) if body.punched_at else datetime.now(tz=timezone.utc)
    ts = ts.replace(microsecond=0)

    # find the most recent open record whose punch_in <= ts
    log = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}, "punch_out": None},
        sort=[("punch_in", DESCENDING)],
    )
    if not log:
        # check if there's a record that's already punched out
        existing = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
            sort=[("punch_in", DESCENDING)],
        )
        if existing and existing.get("punch_out"):
            raise HTTPException(409, "already punched out")
        raise HTTPException(404, "no open punch-in found")

    pi = log["punch_in"]

    # R4 validation: punch_out must be after punch_in and <= 24h
    diff = (ts - pi).total_seconds()
    if diff <= 0:
        raise HTTPException(422, "punched_at must be after punch_in")
    if diff > 86400:
        raise HTTPException(422, "punch-out cannot be more than 24 hours after punch-in")

    wh = calc_work_hours(pi, ts)
    ot = calc_overtime(ts, emp["shift_end"], emp["shift_start"], log["date"])
    hd = wh < 4.50

    updated = db.attendance_logs.find_one_and_update(
        {"_id": log["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": wh,
            "overtime_minutes": ot,
            "half_day": hd,
        }},
        return_document=True,
    )
    if not updated:
        raise HTTPException(409, "already punched out")

    return fmt_log(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

    q = {}
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

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = db.attendance_logs.find(q).sort(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    ).skip(skip).limit(page_size)

    return {
        "items": [fmt_log(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeReq):
    date_str = date
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    log = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not log:
        raise HTTPException(404, "no attendance record for that date")

    new_status = body.status if body.status is not None else log["status"]

    # validate: ABSENT/LEAVE can't have punch times
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        new_pi = None
        new_po = None
    else:
        # presence status needs a punch_in
        new_pi_ms = body.punch_in
        new_po_ms = body.punch_out

        if new_pi_ms is not None:
            new_pi = ms_to_dt(new_pi_ms).replace(microsecond=0)
            # punch_in must stay on the attendance date (R1)
            pi_att = attendance_date(new_pi, emp["shift_start"], emp["shift_end"])
            if pi_att != date_str:
                raise HTTPException(422, "punch_in would fall on a different attendance date")
        else:
            new_pi = log.get("punch_in")

        if new_pi is None:
            raise HTTPException(422, "presence status requires a punch_in")

        if new_po_ms is not None:
            new_po = ms_to_dt(new_po_ms).replace(microsecond=0)
        else:
            new_po = log.get("punch_out")

        if new_po is not None:
            diff = (new_po - new_pi).total_seconds()
            if diff <= 0:
                raise HTTPException(422, "punch_out must be after punch_in")
            if diff > 86400:
                raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    # compute derived fields
    if new_status in ("ABSENT", "LEAVE") or new_pi is None:
        new_late = 0
        new_wh = None
        new_ot = 0
        new_hd = False
    else:
        new_late = calc_late(new_pi, emp["shift_start"], date)
        if new_po is not None:
            new_wh = calc_work_hours(new_pi, new_po)
            new_ot = calc_overtime(new_po, emp["shift_end"], emp["shift_start"], date_str)
            new_hd = new_wh < 4.50
        else:
            new_wh = None
            new_ot = 0
            new_hd = False

    # figure out what actually changed
    old_pi = log.get("punch_in")
    old_po = log.get("punch_out")
    old_status = log["status"]
    old_late = log.get("late_minutes", 0)
    old_wh = log.get("work_hours")
    old_ot = log.get("overtime_minutes", 0)
    old_hd = log.get("half_day", False)

    changes = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_pi != old_pi:
        changes["punch_in"] = {"from": old_pi, "to": new_pi}
    if new_po != old_po:
        changes["punch_out"] = {"from": old_po, "to": new_po}
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}
    if new_wh != old_wh:
        changes["work_hours"] = {"from": old_wh, "to": new_wh}
    if new_ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_ot}
    if new_hd != old_hd:
        changes["half_day"] = {"from": old_hd, "to": new_hd}

    if not changes:
        raise HTTPException(422, "no changes detected")

    history_entry = {
        "at": datetime.now(tz=timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # atomic update — use history length as optimistic lock
    hist_len = len(log.get("history", []))
    result = db.attendance_logs.find_one_and_update(
        {
            "_id": log["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, hist_len]},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_pi,
                "punch_out": new_po,
                "work_hours": new_wh,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_hd,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, "concurrent edit detected, please retry")

    return fmt_log(result)


# ── analytics ────────────────────────────────────────────────────────────────

def parse_month(month: str):
    """Returns (year, month_int, first_day_str, last_day_str)."""
    y, m = int(month[:4]), int(month[5:])
    first = date(y, m, 1)
    if m == 12:
        last = date(y + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(y, m + 1, 1) - timedelta(days=1)
    return y, m, first.isoformat(), last.isoformat()


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    y, m, first_d, last_d = parse_month(month)
    wd = working_days_in_month(y, m, emp["joined_on"])

    pipe = [{"$match": {"emp_code": emp_code, "date": {"$gte": first_d, "$lte": last_d}}}]
    logs = list(db.attendance_logs.aggregate(pipe))

    leave_days = sum(1 for l in logs if l["status"] == "LEAVE")
    late_count = sum(1 for l in logs if (l.get("late_minutes") or 0) > 0)
    total_late = sum((l.get("late_minutes") or 0) for l in logs)
    total_ot = sum((l.get("overtime_minutes") or 0) for l in logs)

    present_sum = 0.0
    for l in logs:
        if l["status"] in PRESENCE:
            d = date.fromisoformat(l["date"])
            if d.weekday() < 5:  # Mon-Fri
                present_sum += 0.5 if l.get("half_day") else 1.0

    present_days = rh(present_sum, 2)

    att_pct = rh(present_days / wd * 100, 2) if wd > 0 else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary")
def dept_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    y, m, first_d, last_d = parse_month(month)

    emp_match = {"joined_on": {"$lte": last_d}}
    if department:
        emp_match["department"] = department

    pipe = [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"ec": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {
                    "$and": [
                        {"$eq": ["$emp_code", "$$ec"]},
                        {"$gte": ["$date", first_d]},
                        {"$lte": ["$date", last_d]},
                    ]
                }}},
            ],
            "as": "logs",
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "logs": {"$push": "$logs"},
        }},
        {"$addFields": {"logs": {"$reduce": {
            "input": "$logs",
            "initialValue": [],
            "in": {"$concatArrays": ["$$value", "$$this"]},
        }}}},
    ]

    rows = list(db.employees.aggregate(pipe))
    items = []
    for r in rows:
        d_name = r["_id"]
        d_logs = r.get("logs", [])
        
        present_sum = 0.0
        wh_vals = []
        late_count = 0
        total_late = 0
        leave_count = 0
        on_duty_count = 0

        for l in d_logs:
            st = l.get("status")
            if st == "LEAVE": leave_count += 1
            if st == "ON_DUTY": on_duty_count += 1
            late_m = l.get("late_minutes") or 0
            if late_m > 0:
                late_count += 1
                total_late += late_m

            if st in PRESENCE:
                try:
                    dt = date.fromisoformat(l["date"])
                    if dt.weekday() < 5:
                        present_sum += 0.5 if l.get("half_day") else 1.0
                except:
                    pass
                
                wh = l.get("work_hours")
                if wh is not None and wh > 0:
                    wh_vals.append(wh)

        avg_wh = rh(sum(wh_vals) / len(wh_vals), 2) if wh_vals else None

        items.append({
            "department": d_name,
            "headcount": r["headcount"],
            "present_days": rh(present_sum, 2),
            "avg_work_hours": avg_wh,
            "late_count": late_count,
            "total_late_minutes": total_late,
            "leave_count": leave_count,
            "on_duty_count": on_duty_count,
        })
        
    items.sort(key=lambda x: x["department"])
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    _, _, first_d, last_d = parse_month(month)

    pipe = [
        {"$match": {"date": {"$gte": first_d, "$lte": last_d}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$match": {"emp": {"$ne": []}}},
        {"$unwind": "$emp"},
    ]

    if department:
        pipe.append({"$match": {"emp.department": department}})

    pipe += [
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}},
            },
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ]

    items = list(db.attendance_logs.aggregate(pipe))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def dept_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    if to_date < from_date:
        raise HTTPException(422, "to must be >= from")
    d0 = date.fromisoformat(from_date)
    d1 = date.fromisoformat(to_date)
    if (d1 - d0).days > 91:
        raise HTTPException(422, "date range cannot exceed 92 days")

    # check dept exists
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")

    # per-day stats for dept; gaps filled in Python below
    pipe = [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$match": {"emp.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {
                "$cond": [
                    {"$in": ["$status", list(PRESENCE)]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                    0,
                ]
            }},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
    ]

    rows_raw = list(db.attendance_logs.aggregate(pipe))

    # Get all employees in this dept with their joined_on, then compute
    # headcount per day in Python (simpler and avoids a complex lookup per row)
    emp_list = list(db.employees.find({"department": department}, {"joined_on": 1, "_id": 0}))
    joined_dates = [e["joined_on"] for e in emp_list]

    row_map = {r["_id"]: r for r in rows_raw}

    items = []
    # iterate every calendar day in range
    cur = d0
    prev_rates = []  # for moving avg, only non-null rates
    while cur <= d1:
        d_str = cur.isoformat()
        is_wd = cur.weekday() < 5
        hc = sum(1 for jd in joined_dates if jd <= d_str)

        row = row_map.get(d_str, {})
        present_count = row.get("present_count", 0)
        late_count = row.get("late_count", 0)

        if is_wd and hc > 0:
            rate = rh(present_count / hc, 4)
        else:
            rate = None

        prev_rates.append(rate)
        # window = last 7 entries (current + up to 6 preceding)
        window = [r for r in prev_rates[-7:] if r is not None]
        mavg = rh(sum(window) / len(window), 4) if window else None

        items.append({
            "date": d_str,
            "is_working_day": is_wd,
            "headcount": hc,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": rate,
            "moving_avg_7d": mavg,
        })
        cur += timedelta(days=1)

    return {"department": department, "items": items}


# ── admin/explain ─────────────────────────────────────────────────────────────

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    valid = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in valid:
        raise HTTPException(422, f"endpoint must be one of {valid}")

    if endpoint == "attendance_list":
        q = {}
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
        skip = (page - 1) * page_size
        cmd = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        result = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": result}

    elif endpoint == "employee_monthly":
        if not month or not emp_code:
            raise HTTPException(422, "month and emp_code required")
        _, _, first_d, last_d = parse_month(month)
        pipe = [{"$match": {"emp_code": emp_code, "date": {"$gte": first_d, "$lte": last_d}}}]
        cmd = {"explain": {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, "verbosity": "executionStats"}
        result = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": result}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        _, _, first_d, last_d = parse_month(month)
        emp_match = {"joined_on": {"$lte": last_d}}
        if department:
            emp_match["department"] = department
        pipe = [
            {"$match": emp_match},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {
                        "$and": [
                            {"$eq": ["$emp_code", "$$ec"]},
                            {"$gte": ["$date", first_d]},
                            {"$lte": ["$date", last_d]},
                        ]
                    }}},
                ],
                "as": "logs",
            }},
            {"$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "logs": {"$push": "$logs"},
            }},
            {"$addFields": {"logs": {"$reduce": {
                "input": "$logs",
                "initialValue": [],
                "in": {"$concatArrays": ["$$value", "$$this"]},
            }}}},
        ]
        cmd = {"explain": {"aggregate": "employees", "pipeline": pipe, "cursor": {}}, "verbosity": "executionStats"}
        result = db.command(cmd)
        return {"endpoint": endpoint, "collection": "employees", "explain": result}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        _, _, first_d, last_d = parse_month(month)
        pipe = [
            {"$match": {"date": {"$gte": first_d, "$lte": last_d}, "late_minutes": {"$gt": 0}}},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }},
            {"$match": {"emp": {"$ne": []}}},
            {"$unwind": "$emp"},
        ]
        if department:
            pipe.append({"$match": {"emp.department": department}})
        pipe += [
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }},
            {"$match": {"rank": {"$lte": limit}}},
            {"$project": {
                "_id": 0, "rank": 1, "emp_code": "$_id",
                "name": "$emp.name", "department": "$emp.department",
                "total_late_minutes": 1, "late_count": 1,
            }},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]
        cmd = {"explain": {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, "verbosity": "executionStats"}
        result = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": result}

    elif endpoint == "department_trend":
        if not from_date or not to_date or not department:
            raise HTTPException(422, "from, to, and department are required")
        pipe = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }},
            {"$match": {"emp.department": department}},
            {"$group": {
                "_id": "$date",
                "present_count": {"$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 6]},
                            {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 2]}
                        ]},
                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                        0,
                    ]
                }},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            }},
        ]
        cmd = {"explain": {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, "verbosity": "executionStats"}
        result = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": result}
