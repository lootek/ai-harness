exit 0  # fixture: scanned as text, never executed

set -eu
export VAULT_ADDR="https://vault.example.com"
vault write secret/example/app-config retention=30 owner=team-example
