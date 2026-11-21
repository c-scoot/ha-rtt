"""Build the manual-install ZIP using only release files, never local secrets."""

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "realtime_trains"


def main() -> None:
    manifest = json.loads((INTEGRATION / "manifest.json").read_text(encoding="utf-8"))
    output = ROOT / "dist" / f"realtime_trains-{manifest['version']}.zip"
    output.parent.mkdir(exist_ok=True)
    allowed = {".py", ".json", ".png"}
    files = sorted(
        path
        for path in INTEGRATION.rglob("*")
        if path.is_file() and path.suffix in allowed and "__pycache__" not in path.parts
    )
    files += [
        ROOT / name
        for name in ("LICENSE", "NOTICE.md", "README.md", "CHANGELOG.md", "hacs.json")
    ]
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(ROOT).as_posix())
    with ZipFile(output) as archive:
        assert archive.testzip() is None
        assert "custom_components/realtime_trains/manifest.json" in archive.namelist()
    print(output)


if __name__ == "__main__":
    main()
