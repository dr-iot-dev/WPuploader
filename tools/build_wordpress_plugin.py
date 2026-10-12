"""Build the current News Weave plugin, independently of generation."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plugin = ROOT / "wordpress/news-weave"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(args.output, "w", compression=ZIP_DEFLATED) as archive:
        for file in sorted(plugin.rglob("*.php")):
            archive.write(file, "news-weave/" + file.relative_to(plugin).as_posix())
    print(args.output)


if __name__ == "__main__":
    main()
