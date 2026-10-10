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
## API overview

| Method | Path | Purpose |
|--------|------|---------|
| GET | /health | Readiness probe |
| POST | /employees | Create employee |
| GET | /employees | List employees (paginated) |
| POST | /attendance/punch-in | Record punch-in |
| POST | /attendance/punch-out | Record punch-out |
| GET | /attendance | List attendance records |
| PATCH | /attendance/records/{emp_code}/{date}/regularize | Manually correct a record |
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
- Tested locally against the sample edge cases (09:40:00 vs 09:40:01 boundary, overnight 22:00-06:00 shifts, half-up work hour rounding).
