exit 0  # fixture: scanned as text, never executed

set -eu
vault kv put kv-example/project0000/environments/example/config value=placeholder
