import json
from pathlib import Path

from test_ignore_characterization import (
    IGNORE_DIR,
    IGNORED_OUTPUT_FOLDERS,
    build_manifest,
)


def main() -> None:
    manifest = {
        folder_name: build_manifest(IGNORE_DIR / folder_name)
        for folder_name in IGNORED_OUTPUT_FOLDERS
    }
    output = Path(__file__).resolve().parent / "fixtures" / "ignore_output_manifest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(output)


if __name__ == "__main__":
    main()
