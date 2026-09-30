exit 0  # fixture: scanned as text, never executed

set -eu
git commit -am "rewrite history"
git push --force origin main
