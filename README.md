# Employee Attendance & Analytics API

A FastAPI + MongoDB backend for an HR attendance system. Employees punch in and out, HR corrects records with a full
audit trail, and the business runs analytics: monthly summaries, department summaries, a late-comers leaderboard and a
daily department trend. All analytics are MongoDB aggregation pipelines.

> Built for the HROne Software Engineer assignment (contract v2.0). The contract is [`openapi.yaml`](openapi.yaml), and
> the stored data is described in [`DATA_MODEL.md`](DATA_MODEL.md).

---

## For reviewers (TL;DR)

```bash
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # real env vars win over .env
uvicorn app.main:app --port 8000
```

- All application code is in **`app/main.py`**. Part A findings are in **[`REVIEW.md`](REVIEW.md)** and Part C answers
  are in **[`DECISIONS.md`](DECISIONS.md)**.
- Indexes are created at startup, idempotently. `/health` returns 200 only when MongoDB answers a ping, otherwise 503.
- Every endpoint in the contract is implemented; nothing is missing.
- Two contract details I had to interpret:
  - **The trend's 92-day limit** counts the days in the range inclusively: 92 rows are allowed, 93 return 422.
  - **"A request that changes nothing"** (PATCH, 422) is judged on the final record, including the recomputed
    derived fields.

---

## Contents
1. [Tech stack](#tech-stack)
2. [Project structure](#project-structure)
3. [Configuration](#configuration)
4. [Running locally](#running-locally)
5. [API overview](#api-overview)
6. [Conventions](#conventions)
7. [Business rules and where they live](#business-rules-and-where-they-live)
8. [Concurrency](#concurrency)
9. [Analytics pipelines](#analytics-pipelines)
10. [Indexes](#indexes)
11. [Explain endpoint](#explain-endpoint)
12. [Testing and verification](#testing-and-verification)
13. [Error handling](#error-handling)
14. [Known limitations and what I'd do next](#known-limitations-and-what-id-do-next)

---

## Tech stack

| | |
|---|---|
| Language | Python 3.11+ (also tested on 3.14) |
| Web framework | FastAPI, with Pydantic v2 for validation |
| Database | MongoDB 6.0+ (developed and tested on 7.0), PyMongo driver |
| Server | Uvicorn |

## Project structure

```
.
├── app/
│   ├── __init__.py
│   └── main.py            # the entire application (single file, as required)
├── openapi.yaml           # API contract and business rules R1-R10 (provided)
├── DATA_MODEL.md          # MongoDB collections and sample documents (provided)
├── sample_data/           # the nine sample documents as Extended JSON (provided)
├── sample_seed.py         # loads sample_data/ into MongoDB (provided)
├── REVIEW.md              # Part A: defects found in the starter and how they were fixed
├── DECISIONS.md           # Part C: indexes, race handling, ties, headcount, scaling
├── requirements.txt
└── .env.example
```

`app/main.py` is organised top to bottom as:
1. Configuration and the MongoDB client.
2. The index definitions and startup hook.
3. Time helpers (epoch ms ↔ UTC, IST shift moments, the R1 attendance date).
4. Business-rule helpers (R2-R5).
5. Serializers.
6. Validation types and models.
7. CRUD and attendance endpoints.
8. Aggregation pipeline builders.
9. Analytics endpoints.
10. The explain endpoint.

## Configuration

| Variable | Meaning | Local default |
|---|---|---|
| `MONGO_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGO_DB` | Database name | `attendance_db` |

A local `.env` file is loaded for convenience, with `override=False`, so **real environment variables always win**.
Copy `.env.example` to `.env` to get started. Never commit `.env`.

## Running locally

```bash
# 1. MongoDB (any 6.0+; e.g. Docker)
docker run -d --name mongo -p 27017:27017 mongo:7

# 2. Python environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Configuration and sample data
cp .env.example .env
python sample_seed.py              # optional: loads the nine sample documents

# 4. Run
uvicorn app.main:app --port 8000 --reload
```

Interactive docs are at `http://localhost:8000/docs`.

## API overview

| Method | Path | Purpose | Success | Errors |
|---|---|---|---|---|
| GET | `/health` | Readiness: pings MongoDB | 200 | 503 |
| POST | `/employees` | Create an employee | 201 | 409 duplicate `emp_code`, 422 |
| GET | `/employees` | List, filter by `department`, sorted by `emp_code`, paginated | 200 | 422 |
| POST | `/attendance/punch-in` | Create the day's record (R1, R2) | 201 | 404, 409, 422 |
| POST | `/attendance/punch-out` | Close the latest open record (R3-R5), works across midnight | 200 | 404, 409, 422 |
| GET | `/attendance` | List by `emp_code` / `date_from` / `date_to` / `status`, paginated | 200 | 422 |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize: recompute derived fields, append history | 200 | 404, 409, 422 |
| GET | `/analytics/employees/{emp_code}/monthly?month=` | One employee's month | 200 | 404, 422 |
| GET | `/analytics/departments/summary?month=` | Per-department month summary | 200 | 422 |
| GET | `/analytics/leaderboard/late?month=&limit=&department=` | Late-comers ranking with ties | 200 | 422 |
| GET | `/analytics/departments/{department}/trend?from=&to=` | Daily series with 7-day moving average | 200 | 404, 422 |
| GET | `/admin/explain/{endpoint}` | Raw `explain` (executionStats) of an endpoint's main query | 200 | 422 |

### Example

```bash
curl -X POST localhost:8000/employees -H 'Content-Type: application/json' -d '{
  "emp_code": "EMP0001", "name": "Asha Rao", "email": "asha@example.com",
  "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05"}'

curl -X POST localhost:8000/attendance/punch-in  -H 'Content-Type: application/json' \
     -d '{"emp_code": "EMP0001", "punched_at": 1783312500000}'       # 2026-07-06 10:05 IST -> late 35

curl -X POST localhost:8000/attendance/punch-out -H 'Content-Type: application/json' \
     -d '{"emp_code": "EMP0001", "punched_at": 1783345200000}'       # 19:10 IST -> overtime 40

curl -X PATCH localhost:8000/attendance/EMP0001/2026-07-06 -H 'Content-Type: application/json' \
     -d '{"punch_in": 1783310280000, "reason": "biometric glitch", "regularized_by": "hr.admin"}'

curl "localhost:8000/analytics/leaderboard/late?month=2026-07&limit=5"
```

## Conventions

- **Identity.** Employees are identified by `emp_code` (for example `EMP0001`). MongoDB's `_id` never appears in a
  request or response. An attendance record is addressed by `(emp_code, date)`.
- **Instants.** At the API boundary, instants are integer **epoch milliseconds**, validated as strict integers in
  [100000000000, 4102444800000]. Seconds-looking values, floats, strings and booleans get 422. In MongoDB they are
  BSON dates (UTC).
  - The client uses `tz_aware=True`, so every datetime in the code is timezone-aware UTC.
  - `from_ms` and `to_ms` are the only conversion points.
- **Calendar values.** Dates (`YYYY-MM-DD`, IST), months (`YYYY-MM`) and shift times (`HH:MM`, IST) are plain strings,
  both in the API and in MongoDB.
- **Derived fields.** `work_hours`, `late_minutes`, `overtime_minutes` and `half_day` are computed by the server and
  stored. If a client sends them, they are ignored. Analytics read the stored values.
- **Legacy data.** If `history` or `half_day` is missing it is treated as `[]` / `false`. Missing minutes are treated
  as `0`.

## Business rules and where they live

| Rule | Summary | Implementation |
|---|---|---|
| R1 | IST attendance date. Overnight shifts own the early hours of the next day. Truncate to whole seconds. | `attendance_date()`, `from_ms()` |
| R2 | Late only if strictly more than 10:00 after shift start; floor minutes counted from shift start | `compute_late_minutes()` (compares seconds with 600) |
| R3 | Overtime in whole minutes after shift end (next day for overnight shifts), only if ≥ 30 | `compute_overtime()` |
| R4 | Work hours, 2 decimals **half-up** | `compute_work_hours()` uses `Decimal` + `ROUND_HALF_UP`, never `round()` |
| R5 | Half day when rounded hours < 4.50; counts 0.5 | `derived_fields()`, `PRESENT_WEIGHT` |
| R6 | Presence = PRESENT / WFH / ON_DUTY | `PRESENCE`, `IS_PRESENT` |
| R7 | Mon-Fri working days from `joined_on`; `present_days` counts Mon-Fri only | `weekdays_between()`, `_weekday()` |
| R8 | 2 decimals half-up (rates 4) | `half_up()` in Python, `_round_half_up_expr()` in pipelines (`$round` is half-to-even, so it is avoided) |
| R9 | Headcount includes employees with no logs; excludes those not joined yet | `$unionWith employees` (summary), join events (trend) |
| R10 | `page` ≥ 1, `page_size` 1-100 (default 20); `total` reflects the filters | `PageQ`, `PageSizeQ`, `count_documents(q)` |

Worked examples for a 09:30 shift: 09:40:00 → 0, 09:40:01 → 10, and 10:15:59 → 45 late minutes. For a 22:00-06:00
shift, a punch-in at 00:30 is dated the previous day and is 150 minutes late. Punching out at 06:40 gives 40 minutes
of overtime.

## Concurrency

| Scenario | Guarantee | How |
|---|---|---|
| Duplicate employee creates | exactly one 201, the rest 409 | unique index on `emp_code`; plain `insert_one`, `DuplicateKeyError` → 409 |
| Simultaneous identical punch-ins | exactly one 201, the rest 409 | unique index on `(emp_code, date)`. No check-then-insert. |
| Simultaneous punch-outs | exactly one 200, the rest 409 | `find_one_and_update` filtered on `punch_out: null` |
| Concurrent regularizations | no history entry is ever lost; the loser gets 409 | optimistic update guarded on the `status` / `punch_in` / `punch_out` values that were read and on the current `history` length, plus an atomic `$push` |

## Analytics pipelines

Every analytics endpoint is a single aggregation pipeline. Python only does calendar arithmetic (such as
`working_days`) and final Decimal rounding.

**Employee monthly.**
- `$match` on `(emp_code, date range)` using the unique index, then one `$group`.
- Weekdays are found with a string `$in` against the month's weekend dates, which is much cheaper than parsing every
  date with `$dateFromString`.
- `attendance_pct = present_days / working_days × 100`. It is `null` when `working_days` is 0, for example when the
  employee joins after the month ends.

**Department summary** (the headcount must include employees with no logs).
1. `$match` the month's logs, then `$group` them per employee.
2. `$unionWith employees` adds one row per employee with `joined_on <= month end`, carrying their department.
3. A second `$group` merges the rows per employee. Groups without an employee row are dropped: future joiners,
   other departments and unknown employee codes.
4. A final `$group` per department.
- `avg_work_hours` is an average over records (the sum of `work_hours` as Decimal128 divided by the count), not an
  average of per-employee averages.

**Late leaderboard** (ties).
1. `$match` the month's logs with `late_minutes > 0`. The covering index answers this without reading documents.
2. `$group` per employee, then `$lookup employees`. This drops logs of unknown employees and applies the optional
   department filter.
3. `$setWindowFields` + `$rank` gives standard competition ranking (1, 2, 2, 4).
4. `$match rank <= limit`, so every employee tied at the cutoff is kept, then sort by total desc, `emp_code` asc.

**Department trend** (gap filling and a moving average).
1. The department's logs in the range (`emp_code $in` + date range).
2. `$unionWith employees` adds headcount events. Each employee contributes an anchor row on `from`, so the range is
   never empty, plus a +1 on the day they start counting.
3. `$group` by day, then `$densify` to exactly one row per calendar day.
4. A running `$sum` window gives the headcount per day.
5. `attendance_rate` (null on weekends or when headcount is 0) and a 7-row `$avg` window for `moving_avg_7d`. The window
   only uses rows inside the requested range and skips nulls.
- Rates are computed in Decimal128 and rounded half-up inside the pipeline.

## Indexes

Created at startup by `ensure_indexes()`. `create_index` is idempotent.

| Collection | Index | Serves |
|---|---|---|
| employees | `{emp_code: 1}` **unique** | identity, 409 on duplicates, the leaderboard `$lookup` |
| employees | `{department: 1, emp_code: 1}` | `GET /employees?department=` sorted; trend / summary department filter |
| employees | `{joined_on: 1}` | summary headcount (`joined_on <= month end`) without a department filter |
| attendance_logs | `{emp_code: 1, date: 1}` **unique** | race-safe punch-in, PATCH lookup, employee monthly, trend, `?emp_code=` list |
| attendance_logs | `{date: -1, emp_code: 1}` | `GET /attendance` default sort and date ranges, summary month scan |
| attendance_logs | `{status: 1, date: -1, emp_code: 1}` | `GET /attendance?status=` with the same sort |
| attendance_logs | `{date: 1, late_minutes: 1, emp_code: 1}` | **covering** index for the leaderboard (no document fetches, about 3x faster) |
| attendance_logs | `{emp_code: 1, punch_in: -1}` | punch-out: latest `punch_in` at or before a moment |

## Explain endpoint

`GET /admin/explain/{endpoint}`, where `endpoint` is one of `attendance_list`, `employee_monthly`,
`department_summary`, `late_leaderboard` or `department_trend`.

It accepts the same query parameters as the real endpoint. It builds the query or pipeline with **the same builder
function** the endpoint uses, runs `explain` with `executionStats`, and returns `{endpoint, collection, explain}`.
Missing required parameters give 422.

```bash
curl "localhost:8000/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-31"
```

## Testing and verification

The repository contains only the required files. The application was verified locally against MongoDB 7.0 with
separate test scripts that are not committed.

- **Starter defects.** Every finding in `REVIEW.md` was reproduced with a concrete input before it was fixed.
- **Rules and validation.** R1-R5 boundaries (the R2 examples, 29m59s vs 30m overtime, 4.49 vs 4.50 hours, overnight
  shifts), every 404 / 409 / 422 path, pagination, and sort order.
- **Concurrency.** 10-15 parallel requests for duplicate creates, punch-ins, punch-outs and regularizations.
- **Analytics correctness.** Every analytics endpoint was compared against an independent pure-Python implementation
  of the rules on random datasets. They included ties, employees with no logs, weekend records, mid-month and future
  joiners, legacy records without `history` / `half_day`, and logs for unknown employees. The datasets had 4.6k and
  about 100k logs.
- **Performance.** On about 100k logs, all explain plans contain `IXSCAN` and no `COLLSCAN`. List endpoints respond in
  about 10-50 ms and analytics in about 250 ms.
- **Startup.** A clean clone against an empty database is healthy in about 3 s, with all indexes created.

## Error handling

- Errors use FastAPI's default body: `{"detail": "..."}`, or a list for 422 validation errors. Business-rule 422s
  (for example `date_from > date_to`) use the same list shape as Pydantic errors.
- A database outage returns **503**, never 500.
- If MongoDB is unreachable at startup, the app still starts and retries index creation in the background.

## Known limitations and what I'd do next

- **No authentication.** `regularized_by` is free text, as the assignment specifies.
- **No holiday calendar.** Working days are Mon-Fri only (R7).
- **At 100x scale,** I would maintain pre-aggregated daily per-employee and per-department totals (on write, or
  with `$merge`). Analytics would then read small summary collections instead of raw logs, and `attendance_logs`
  could be sharded on `emp_code`. See [`DECISIONS.md`](DECISIONS.md).
