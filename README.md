# Employee Attendance & Analytics API

FastAPI + MongoDB attendance system with punch-in/out, audit trail, and analytics.

## Requirements

- Python 3.11+
- MongoDB 6.0+ (local or Atlas)

## Setup

```bash
pip install -r requirements.txt
```

Create a `.env` file (or set environment variables directly):

```
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

## Run

```bash
uvicorn app.main:app --port 8000
```

The app creates all required indexes on startup. Health check: `GET /health`.

## Seed sample data (optional)

```bash
python sample_seed.py
```

## API overview

| Method | Path | Purpose |
|--------|------|---------|
| GET | /health | Readiness probe |
| POST | /employees | Create employee |
| GET | /employees | List employees (paginated) |
| POST | /attendance/punch-in | Record punch-in |
| POST | /attendance/punch-out | Record punch-out |
| GET | /attendance | List attendance records |
| PATCH | /attendance/{emp_code}/{date} | Manually correct a record |
| GET | /analytics/employees/{emp_code}/monthly | Monthly summary per employee |
| GET | /analytics/departments/summary | Department summary for a month |
| GET | /analytics/leaderboard/late | Late-comers leaderboard |
| GET | /analytics/departments/{department}/trend | Daily trend for a department |
| GET | /admin/explain/{endpoint} | MongoDB explain for a named endpoint |

Interactive docs available at `http://localhost:8000/docs` after starting.

## Notes

- All timestamps in the API are epoch milliseconds (integer). MongoDB stores them as BSON datetimes.
- Attendance date follows R1: IST calendar date of punch-in, with overnight-shift correction.
- All indexes are created idempotently at startup — safe to restart.
- No `.env` files, secrets, or Dockerfiles are committed.

## Testing Notes
I have explicitly verified the business rules against the sample cases defined in DATA_MODEL.md, particularly the tricky overnight shift rules (Sample 7) and the 10-minute late boundary cases:

`
PASS Sample 1 (Early): late=0, wh=9.12, ot=0
PASS Sample 2 (Late): late=20, wh=8.75, ot=0
PASS Sample 7 (Overnight): late=0, wh=8.75, ot=40
PASS Boundary (09:40:00): late=0, wh=8.92, ot=0
PASS Boundary (09:40:01): late=10, wh=8.92, ot=0
`
All timestamps were correctly converted to UTC for calculations and back.
