"""Concurrency checks against the running server on :8000. Local verification only.

Verifies:
  * N simultaneous identical punch-ins -> exactly one 201, rest 409.
  * N simultaneous punch-outs -> exactly one 200, rest 409.
  * N simultaneous regularizations of the same record -> at least one 200, no lost
    history (final history length == number of successful 200 responses).
Run: python concurrency_check.py
"""
import json
import threading
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

BASE = "http://127.0.0.1:8000"
IST = timezone(timedelta(hours=5, minutes=30))


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def ist_ms(y, mo, d, h, mi, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp() * 1000)


def parallel(fn, n):
    results = [None] * n
    def run(i):
        results[i] = fn(i)
    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


# Fresh employee for concurrency
req("POST", "/employees", {"emp_code": "EMP7777", "name": "Race", "email": "race@x.co", "department": "QA", "joined_on": "2026-07-01"})

print("== concurrent punch-in ==")
pin = ist_ms(2026, 8, 3, 9, 0, 0)
res = parallel(lambda i: req("POST", "/attendance/punch-in", {"emp_code": "EMP7777", "punched_at": pin})[0], 12)
n201 = res.count(201)
n409 = res.count(409)
print(f"  statuses: {sorted(res)}")
print(f"  {'PASS' if n201 == 1 and n409 == 11 else 'FAIL'}  exactly one 201 ({n201}), rest 409 ({n409})")

print("== concurrent punch-out ==")
pout = ist_ms(2026, 8, 3, 18, 0, 0)
res = parallel(lambda i: req("POST", "/attendance/punch-out", {"emp_code": "EMP7777", "punched_at": pout})[0], 12)
n200 = res.count(200)
print(f"  statuses: {sorted(res)}")
print(f"  {'PASS' if n200 == 1 else 'FAIL'}  exactly one 200 ({n200})")

print("== concurrent regularize (no lost history) ==")
# 10 threads each try to set a distinct punch_in minute, so each is a real change.
def reg(i):
    newpin = ist_ms(2026, 8, 3, 9, 0, i % 50)  # vary seconds so change differs
    return req("PATCH", "/attendance/EMP7777/2026-08-03",
               {"reason": f"fix number {i}", "regularized_by": f"hr{i}", "punch_in": newpin})[0]

res = parallel(reg, 10)
n200 = res.count(200)
n409 = res.count(409)
print(f"  statuses: {sorted(res)}")
n422 = res.count(422)
s, rec = req("GET", "/attendance?emp_code=EMP7777&date_from=2026-08-03&date_to=2026-08-03")
hist_len = len(rec["items"][0]["history"]) if rec.get("items") else -1
# The core guarantee: each 200 appended exactly one history entry and none were lost,
# so final history length == number of 200s. 409 (lost race) and 422 (no-op because a
# concurrent write already set the same value) legitimately append nothing.
ok = (hist_len == n200) and n200 >= 1 and (n200 + n409 + n422 == 10)
print(f"  200s={n200} 409s={n409} 422s(no-op)={n422} history_len={hist_len}")
print(f"  {'PASS' if ok else 'FAIL'}  history length equals number of successful corrections (no lost writes)")
