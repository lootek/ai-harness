import json
import pathlib


def main():
    rows = json.loads(pathlib.Path("input.json").read_text())
    for row in sorted(rows, key=lambda r: r["name"]):
        print(row["name"], row.get("count", 0))


if __name__ == "__main__":
    main()
