raise SystemExit("fixture: scanned as text, never executed")

import json
import urllib.request

BASE = "https://tracker.example.com/rest/api/3"


def call(path, payload, method):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req) as resp:
        return resp.status


call("/issue/EXAMPLE-2", {"fields": {"summary": "renamed"}}, "PUT")
