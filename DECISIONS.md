# DECISIONS.md

1. **Indexes.** Employees: unique `emp_code`, `(department, emp_code)` for filtered lists and the trend, and `joined_on` for the summary headcount. Logs: unique `(emp_code, date)` for the punch-in guard, PATCH, the monthly summary and the trend. `(date desc, emp_code)` and `(status, date desc, emp_code)` match the list sort. `(date, late_minutes, emp_code)` covers the leaderboard, so it reads no documents (3x faster). `(emp_code, punch_in desc)` serves punch-out. I rejected single-field `status` and `late_minutes` indexes: low selectivity, and the compound ones already serve those queries.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one`. There is no read first. The unique index lets exactly one insert succeed (201). The other gets `DuplicateKeyError`, which becomes 409.

3. **Ties.** `$setWindowFields` with `$rank` gives competition ranks (1, 2, 2, 4). I then keep rows with `rank <= limit`. Every employee tied at the cutoff is returned, so there can be more than `limit` rows, ordered by `emp_code`.

4. **Headcount.** After grouping the month's logs per employee, a `$unionWith` adds one row for every employee with `joined_on <= month end`, carrying their department. Employees with no logs still produce a row. Groups without such a row (future joiners, unknown codes) are dropped.

5. **100x data.** Pre-aggregate daily per-department and per-employee totals at write time (or with `$merge`), so analytics read small summary collections. I would also shard logs on `emp_code`.
