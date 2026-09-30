raise SystemExit("fixture: scanned as text, never executed")

import json
import subprocess

PROJECT = "0000"
MR = "1"
BODY = {"body": "example review note"}

r = subprocess.run(
    ["env", "-u", "GITLAB_TOKEN", "glab", "api",
     f"projects/{PROJECT}/merge_requests/{MR}/notes",
     "--method", "POST", "-H", "Content-Type: application/json",
     "--field", json.dumps(BODY)],
    capture_output=True, text=True,
)
print(r.returncode)
