#!/usr/bin/env bash
set -eu
export VAULT_ADDR="https://vault.example.com"
VAULT_TOKEN=$(vault write -field=token auth/jwt-example/login \
  role="example-role" jwt="${EXAMPLE_JWT}")
export VAULT_TOKEN
vault kv get kv-example/config
