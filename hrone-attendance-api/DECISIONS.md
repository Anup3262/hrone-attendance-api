# DECISIONS.md

1. **Indexes.** `employees.emp_code` unique — concurrent duplicate creates yield exactly one 201, rest 409. `attendance_logs(emp_code, date)` unique — same guarantee for punch-in races, and it serves punch-in/out lookups. `attendance_logs(date, emp_code)` — the list endpoint's sort (`date` desc, `emp_code` asc) and the analytics `$match` stages. `employees(department, joined_on)` — headcount and department filters. I considered a dedicated `(emp_code, punch_in)` index for punch-out and rejected it: per-employee history is tiny and the unique `(emp_code, date)` index already narrows the scan to one employee.

2. **Punch-in race.** Two identical requests compute the same document and both call `insert_one`. There is no check-then-insert. The unique `(emp_code, date)` index lets one insert win; the loser gets `DuplicateKeyError`, which I catch and turn into 409. Exactly one 201, rest 409 — enforced by the database, not by timing.

3. **Ties.** The leaderboard ranks in the pipeline with `$setWindowFields` + `$rank`: tied totals share a rank and the next rank is skipped (1, 2, 2, 4). I filter `rank <= limit` after ranking, so a tie at the cutoff returns more than `limit` rows. Rows are ordered by total descending, then `emp_code` ascending.

4. **Headcount.** The department summary starts from the `employees` collection (`joined_on <= month end`) and `$lookup`s each employee's logs with `preserveNullAndEmptyArrays`, so employees with zero logs still contribute to headcount while later joiners are excluded. Headcount never comes from the logs.

5. **One thing you would change** for 100x data: pre-aggregate. A nightly job would roll each day's logs into per-employee/per-department summary documents, and the analytics endpoints would read those instead of scanning raw logs. The raw logs stay for audit; the hot paths stop touching them.
