# Employee Attendance & Analytics API

FastAPI + MongoDB service for the HROne assignment. All code is in `app/main.py`. The contract is in `openapi.yaml`,
and the stored data is described in `DATA_MODEL.md`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # or export MONGO_URI / MONGO_DB (real environment variables win over .env)
python sample_seed.py           # optional: the nine sample documents
uvicorn app.main:app --port 8000
```
Requires MongoDB 6.0+ (developed and tested against 7.0, e.g. `docker run -p 27017:27017 mongo:7`).

## Notes
- All indexes are created at startup, idempotently. If MongoDB is not reachable yet, creation is retried in the
  background, and `/health` returns 503 until MongoDB answers a ping.
- Instants are epoch milliseconds at the API boundary and BSON dates (UTC) in MongoDB. They are truncated to whole
  seconds (R1). All R4/R8 rounding is done half-up, either with `Decimal` in Python or with a floor-based expression
  in the pipelines (`$round` rounds half to even, so it is not used).
- Every endpoint is implemented. Analytics are single aggregation pipelines. `GET /admin/explain/{endpoint}` runs
  `explain` (executionStats) on the exact pipeline/query the endpoint builds.
- Concurrency: punch-in relies on the unique `(emp_code, date)` index. Punch-out is a conditional update
  (`punch_out: null`). PATCH is an optimistic update guarded on the values it read and the history length, so the
  loser of a race gets 409 and no history entry is lost.
- Two contract details I had to interpret:
  - **The trend's 92-day limit** counts the days in the range inclusively: 92 rows are allowed, 93 return 422.
  - **"A request that changes nothing"** is judged on the final record, including the recomputed derived fields.
- Tested locally with tests not included in the repo: the starter defects, R1-R5 edge cases, all concurrency cases,
  and every analytics endpoint against an independent Python reference on a random dataset of about 100k records
  (ties, no-log employees, weekend records, mid-month and future joiners, overnight shifts, legacy records, orphan
  logs). Every explain plan used an index scan with no collection scan, and analytics ran in about 250ms at 100k.
