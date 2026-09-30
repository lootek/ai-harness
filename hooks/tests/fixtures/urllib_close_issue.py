raise SystemExit("fixture: scanned as text, never executed")

import json
import urllib.request

req = urllib.request.Request(
    "https://tracker.example.com/rest/api/3/issue/EXAMPLE-1/transitions",
    data=json.dumps({"transition": {"id": "31"}}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req) as resp:
    print(resp.status)
