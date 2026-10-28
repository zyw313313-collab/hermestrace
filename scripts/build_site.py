import argparse
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = (
    ".gitignore",
    "README.md",
    "hermestrace.py",
    "dashboard.py",
    "dashboard_template.html",
    "scripts/build_site.py",
    "site/demo.json",
    ".github/workflows/pages.yml",
)


def build_site(output):
    data = json.loads((ROOT / "site" / "demo.json").read_text(encoding="utf-8"))
    data["demo"] = True
    data["source_download"] = "./hermestrace-source.zip"
    template = (ROOT / "dashboard_template.html").read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = template.replace("__HERMESTRACE_DATA__", payload)
    output.mkdir(parents=True, exist_ok=True)
    (output / "index.html").write_text(page, encoding="utf-8")
    (output / ".nojekyll").write_text("", encoding="utf-8")
    with ZipFile(output / "hermestrace-source.zip", "w", compression=ZIP_DEFLATED) as archive:
        for filename in SOURCE_FILES:
            archive.write(ROOT / filename, arcname="hermestrace/" + filename)
    print(f"Built public demo and source download in {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the public demo using bundled synthetic data.")
    parser.add_argument("--output", type=Path, default=ROOT / "_site")
    arguments = parser.parse_args()
    build_site(arguments.output)
