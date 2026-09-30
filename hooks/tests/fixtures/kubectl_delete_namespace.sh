exit 0  # fixture: scanned as text, never executed

set -eu
kubectl delete namespace example-ephemeral --wait=false
