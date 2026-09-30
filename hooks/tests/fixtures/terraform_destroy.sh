exit 0  # fixture: scanned as text, never executed

set -eu
cd infra/example
terraform destroy -auto-approve
