1. indexes
created `{emp_code, date}` on attendance_logs and made it unique so we don't get double punches. also added `{date: -1, emp_code: 1}` to keep pagination queries fast. added `{date: 1, status: 1}` for the analytics filters. i briefly thought about indexing `{department, date}` but tossed it because department isn't even in the logs collection, you have to join it from employees anyway.

2. duplicate punch-ins
since i added that unique index, we let mongodb handle the race condition. thread 1 inserts, thread 2 gets a DuplicateKeyError from the storage engine. the app catches it and returns a 409 conflict. no crazy app-level locking needed.

3. leaderboard ties
used `$rank` which naturally gives tied users the same rank (1, 2, 2, 4). we filter `rank <= limit`. if a bunch of people are tied exactly at the cutoff, the secondary sort on `emp_code` breaks the tie alphabetically and the pipeline truncates them.

4. zero log employees in dept summary
we gotta query the `employees` collection first (where joined_on is before the month ends) to get the real headcount. then we `$lookup` their logs. if a guy has no logs, he still adds 1 to the headcount but 0 to present_days. this way the averages don't get skewed.

5. scaling to 100x
if the DB gets huge, doing live aggregations on million-row tables every time someone loads the analytics dashboard will kill the db. i'd set up a nightly cron job that pre-calculates present_days, late_count etc and upserts it into a `monthly_summaries` collection. then the api just reads from that table instantly.
