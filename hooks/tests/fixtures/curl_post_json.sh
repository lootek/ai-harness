exit 0  # fixture: scanned as text, never executed

set -eu
curl -sS -X POST "https://api.example.com/v1/widgets" \
  -H "Content-Type: application/json" \
  -d '{"name": "example", "size": 3}'
