raise SystemExit("fixture: scanned as text, never executed")

import os
import requests

requests.post(
    "https://slack.com/api/chat.postMessage",
    headers={"Authorization": f"Bearer {os.environ['EXAMPLE_TOKEN']}"},
    json={"channel": "C00000000", "text": "example status update"},
)
