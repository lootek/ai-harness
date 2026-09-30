exit 0  # fixture: scanned as text, never executed

set -eu
mkfs.ext4 /dev/sdb1
mount /dev/sdb1 /mnt/data
