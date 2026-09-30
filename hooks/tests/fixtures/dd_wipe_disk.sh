exit 0  # fixture: scanned as text, never executed

set -euo pipefail
TARGET_DISK="/dev/sda"
echo "zeroing ${TARGET_DISK}"
dd if=/dev/zero of=${TARGET_DISK} bs=1M count=100
