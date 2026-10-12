"""Check current plugin without accessing any WordPress site."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    plugin = (ROOT / "wordpress/news-weave/news-weave.php").read_text(encoding="utf-8")
    harness = (ROOT / "tests/wordpress/test-news-weave.php").read_text(encoding="utf-8")
    source = harness.replace(
        "require '/project/wordpress/news-weave/news-weave.php';",
        plugin.removeprefix("<?php"),
    )
    command = [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "-i",
        "--network",
        "none",
        "php:8.3-cli-alpine",
        "php",
    ]
    subprocess.run([*command, "-l"], input=plugin.encode(), check=True)
    subprocess.run(command, input=source.encode(), check=True)


if __name__ == "__main__":
    main()
