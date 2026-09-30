#!/usr/bin/env bash
set -eu
curl -sS -X POST "http://localhost:8080/api/reload" -d '{}'
