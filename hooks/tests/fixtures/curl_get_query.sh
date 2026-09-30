#!/usr/bin/env bash
set -eu
curl -sS -G "https://api.example.com/v1/search" \
  --data-urlencode 'q=example' \
  --data-urlencode 'limit=25'
