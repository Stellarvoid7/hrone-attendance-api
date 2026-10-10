code review - starter files

dude, the starter code was pretty rough in a few spots. fixed what I could find:

1. health check endpoint
it just returned status ok without even touching the db. i added a db.command("ping") in a try block so it actually tests the connection. otherwise what's the point right?

2. /employees timestamp issue
it used datetime.now() which is local time. this is a huge footgun if the app runs in different regions. switched everything to datetime.now(timezone.utc) so all BSON dates are explicitly UTC.

3. race condition on punch-ins
punch_in was just doing a raw insert_one. if a user double clicked the submit button, they got two punch-ins for the same day. i added a unique index on {emp_code, date} to stop it at the db level. threw in a catch for DuplicateKeyError to return a 409.

4. the overnight shift logic (R1)
starter code just took the date from the punch_ist time. meaning if someone punched in at 00:10 for a shift that started at 22:00 yesterday, they got logged for today. I wrote a helper `attendance_date()` to check if the punch time is in the AM while the shift started PM, and subtract a day.

5. floating point weirdness in calc_late
math was done with minutes (float - float) which led to negative late mins for overnight shifts and weird .00001 rounding stuff. rewrote it to calculate total integer seconds from midnight of the shift date. much cleaner now.

6. overtime min threshold (R3)
starter code was giving OT for literally 1 minute past shift. the requirements said 30 mins minimum. added a simple check.

7. bankers rounding
work_hours used python's default round() which rounds to even (2.5 -> 2, 3.5 -> 4). reqs wanted half-up. swapped to Decimal quantize ROUND_HALF_UP.

8. OOM waiting to happen
list_attendance did list(db.logs.find()) pulling everything into ram before pagination. fixed it by pushing skip() and limit() down to the db cursor.
