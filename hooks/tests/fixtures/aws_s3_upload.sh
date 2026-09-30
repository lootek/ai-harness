exit 0  # fixture: scanned as text, never executed

set -eu
aws s3 cp ./build/output.tar "s3://example-bucket/artifacts/output.tar"
