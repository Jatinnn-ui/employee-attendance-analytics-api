# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity + 409 on duplicates) and
   `(department, joined_on)` for headcount and department analytics. `attendance_logs`:
   unique `(emp_code, date)` (one record per day, and the punch-in race guard),
   `(date desc, emp_code asc)` for the listing sort and date-range analytics, and
   `(status, date)` for status filters. I considered a separate single-field `date`
   index but rejected it: the compound `(date, emp_code)` already serves date queries,
   so a duplicate would only add write cost.

2. **Punch-in race.** Both requests build the same document and call `insert_one`. The
   unique `(emp_code, date)` index lets exactly one succeed; the other raises
   `DuplicateKeyError`, which I turn into 409. There is no check-then-insert to lose.

3. **Ties at the cutoff.** Rank is standard competition ranking (1,2,2,4). I keep every
   row whose rank is `<= limit`, so tied employees at the cutoff are all returned even
   if that exceeds `limit`.

4. **Zero-log employees.** The summary starts from `employees` and `$lookup`s their
   logs with `preserveNullAndEmptyArrays`, so an employee with no logs still counts in
   headcount with zero present days.

5. **100x data.** I would shard `attendance_logs` on `emp_code` (or pre-aggregate
   monthly rollups) so analytics scan far fewer documents.
