#!/usr/bin/env bash
set -eu
curl -sS -X POST "https://tracker.example.com/rest/api/3/search/jql" \
  -H "Content-Type: application/json" \
  -d '{"jql": "project = EXAMPLE", "maxResults": 50}'
