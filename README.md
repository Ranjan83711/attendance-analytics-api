# Employee Attendance & Analytics API

FastAPI + MongoDB service for the HROne assignment. All code is in `app/main.py`. The contract is in `openapi.yaml`, and
the stored data is described in `DATA_MODEL.md`. Part A is in [`REVIEW.md`](REVIEW.md) and Part C in
[`DECISIONS.md`](DECISIONS.md).

## Run
```bash
pip install -r requirements.txt
cp .env.example .env              # or export MONGO_URI / MONGO_DB (real environment variables win over .env)
python sample_seed.py             # optional: loads the nine sample documents
uvicorn app.main:app --port 8000
```
Requires MongoDB 6.0+ (tested on 7.0, e.g. `docker run -p 27017:27017 mongo:7`).

## Things to know
- **Status:** every endpoint in the contract is implemented; nothing is missing.
- **Startup:** indexes are created at startup, idempotently. If MongoDB isn't reachable yet, creation is retried in
  the background. `/health` returns 200 only when MongoDB answers a ping, otherwise 503.
- **Time and rounding:** instants are epoch ms in the API and BSON dates (UTC) in MongoDB, truncated to whole seconds
  (R1). Rounding is half-up via `Decimal` or a floor-based pipeline expression, because `$round` rounds half to even.
- **Concurrency:**
  - **Punch-in** relies on the unique `(emp_code, date)` index.
  - **Punch-out** is a conditional update on `punch_out: null`.
  - **PATCH** is an optimistic update guarded on the values it read and on the history length. The loser of a race
    gets 409, and no history entry is lost.
- **Analytics** are single aggregation pipelines. `/admin/explain/{endpoint}` explains the exact query or pipeline
  the endpoint builds.
- **Interpretations:**
  - The trend's 92-day limit counts the days in the range inclusively: 92 rows are allowed, 93 return 422.
  - "A request that changes nothing" is judged on the final record, including the recomputed derived fields.
- **Testing:** tested locally against MongoDB 7.0 with about 100k generated records, covering ties, employees with no
  logs, weekends, joiners, overnight shifts, legacy records and race conditions. Every explain plan used an index scan
  with no collection scan.
