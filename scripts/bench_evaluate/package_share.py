"""Package one run's viewer and artifacts without development-only helpers."""
from __future__ import annotations
import argparse
import json
import shutil
import tarfile
from pathlib import Path
from judge_system.report_viewer import build_viewer

PATH_FIELDS = ("code_path", "svg_path", "png_path", "render_png_path", "render_mesh_path", "render_step_path")


def package_run(run_root: Path, output: Path, *, make_tar: bool = True) -> Path:
    run_root = run_root.resolve()
    output = output.resolve()
    # Never erase an existing directory or copy an arbitrary workspace tree.
    output.mkdir(parents=True, exist_ok=False)
    rows = json.loads((run_root / "reports/records.json").read_text(encoding="utf-8"))
    copied = {}
    for row in rows:
        for field in PATH_FIELDS:
            value = row.get(field)
            if not value:
                continue
            source = Path(value)
            if not source.is_absolute():
                candidates = (run_root / source, run_root / "reports" / source, Path.cwd() / source)
                source = next((p for p in candidates if p.is_file()), candidates[0])
            source = source.resolve()
            if not source.is_relative_to(run_root):
                raise ValueError("Shared artifacts must be inside the run directory")
            if not source.is_file():
                raise FileNotFoundError(f"Missing artifact for {field}: {source.name}")
            if source not in copied:
                dest = Path("assets") / f"{len(copied):05d}{source.suffix}"
                (output / dest).parent.mkdir(exist_ok=True)
                shutil.copyfile(source, output / dest)
                copied[source] = dest.as_posix()
            row[field] = copied[source]
        # Errors can contain local paths; omit them from the public package.
        for key in list(row):
            if key.endswith(("_error", "_url")):
                row[key] = ""
            elif key.endswith("_path") and key not in PATH_FIELDS:
                row.pop(key)
    records = output / "records.json"
    records.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    catalog = output / "rubric_catalog.json"
    original = run_root / "reports/rubric_catalog.json"
    catalog.write_text(original.read_text(encoding="utf-8") if original.exists() else "[]", encoding="utf-8")
    build_viewer(records, catalog, output / "index.html", relative_artifacts=True)
    if make_tar:
        with tarfile.open(output.with_suffix(".tgz"), "w:gz") as archive:
            def anonymous_metadata(info):
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                return info
            archive.add(output, arcname="muse-results", filter=anonymous_metadata)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run-id", help="Run folder under --results-root")
    group.add_argument("--run-root", type=Path)
    parser.add_argument("--results-root", type=Path, default=Path("out"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-tar", action="store_true")
    args = parser.parse_args()
    root = args.run_root or args.results_root / args.run_id
    print(package_run(root, args.output or root / "share", make_tar=not args.no_tar))


if __name__ == "__main__":
    main()
