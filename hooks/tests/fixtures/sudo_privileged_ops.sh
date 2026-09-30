exit 0  # fixture: scanned as text, never executed

set -eu
sudo usermod -s /sbin/nologin serviceuser
sudo systemctl restart example-agent
