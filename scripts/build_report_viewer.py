import sys
from pathlib import Path

from src.judge_system.report_viewer import build_viewer


def main() -> None:
    if len(sys.argv) > 1:
        report_dir = Path(sys.argv[1]).expanduser().resolve()
    else:
        raise SystemExit("usage: build_report_viewer.py <reports_dir>")
    output = report_dir / "viewer.html"
    build_viewer(
        report_dir / "records.json",
        report_dir / "rubric_catalog.json",
        output,
    )
    print(output)


if __name__ == "__main__":
    main()
