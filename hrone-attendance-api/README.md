# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the HROne assignment: punch-in/out with
business rules (R1–R10), manual corrections with an audit trail, and analytics
endpoints built on MongoDB aggregation pipelines.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set MONGO_URI / MONGO_DB
# MongoDB 6.0+: docker run -p 27017:27017 mongo:7   (or Atlas M0 free tier)
python sample_seed.py  # loads the sample documents
uvicorn app.main:app --port 8000 --reload
```

Real environment variables take priority over `.env`. All indexes are created
at startup, idempotently. `GET /health` returns 200 only when MongoDB answers
a ping.

## What's implemented

- `POST /employees`, `GET /employees` — unique `emp_code` enforced by a
  database unique index (concurrent duplicates: one 201, rest 409).
- `POST /attendance/punch-in` — IST attendance date incl. the overnight-shift
  rule (R1), late minutes with the 10-minute grace (R2); race-safe via the
  unique `(emp_code, date)` index.
- `POST /attendance/punch-out` — closes the most recent record with
  `punch_in <= punched_at` (works across midnight); atomic close so two
  simultaneous punch-outs yield one 200 and one 409; computes work hours
  (R4, half-up), overtime (R3, 30-min minimum), half day (R5).
- `GET /attendance` — DB-side sort/filter/pagination, never loads the
  collection into memory.
- `PATCH /attendance/{emp_code}/{date}` — regularization with full rule
  validation, derived-field recompute, and one atomic `$set` + `$push`
  history entry per correction.
- Analytics as aggregation pipelines: employee monthly, department summary
  (zero-log employees counted in headcount), late leaderboard (standard
  competition ranking, limit applied after ranking), department trend
  (`$densify` gap-filling, one row per day).
- `GET /admin/explain/{endpoint}` — `explain("executionStats")` of the exact
  query/pipeline each endpoint builds.

`REVIEW.md` lists every defect found in the starter; `DECISIONS.md` answers
the five design questions.

## Notes

- Nothing is missing from the contract; every endpoint in `openapi.yaml`
  is implemented.
- Instants cross the API boundary as epoch milliseconds; MongoDB stores
  BSON datetimes. All datetimes are truncated to whole seconds (R1) and all
  rounding is half-up via `Decimal` (R4, R8).
