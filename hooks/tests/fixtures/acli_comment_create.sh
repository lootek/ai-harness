exit 0  # fixture: scanned as text, never executed

set -eu
acli jira workitem comment create --key EXAMPLE-3 --body "automated note"
