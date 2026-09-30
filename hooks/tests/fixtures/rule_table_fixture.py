CASES = [
    ("rm -rf /", "bare recursive delete"),
    ("dd if=/dev/zero of=/dev/sda", "raw disk write"),
    ("vault write secret/x k=v", "vault write"),
    ("git push --force origin main", "force push"),
]


def labels():
    return [label for _, label in CASES]
