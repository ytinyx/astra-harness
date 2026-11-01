"""只检查项目公开文档，不遍历本地数据库、构建缓存或运行产物。"""

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def main():
    files = sorted([*ROOT.glob("*.md"), *(ROOT / "docs").rglob("*.md")])
    errors, links, anchors = [], 0, 0
    for path in files:
        text = path.read_text(encoding="utf-8")
        label = str(path.relative_to(ROOT))
        if not text.endswith("\n") or any(line.rstrip() != line for line in text.splitlines()):
            errors.append(f"{label}: whitespace")
        if len(re.findall(r"^```", text, re.M)) % 2:
            errors.append(f"{label}: fence")
        if re.search(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]", text):
            errors.append(f"{label}: absolute local path")
        headings = re.findall(r"^#{1,6}\s+(.+)$", text, re.M)
        if len(headings) != len(set(headings)):
            errors.append(f"{label}: duplicate heading")
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
            if "://" in target or target.startswith("mailto:"):
                continue
            links += 1
            name, _, fragment = target.partition("#")
            destination = path.parent / name if name else path
            if not destination.is_file():
                errors.append(f"{label}: missing {target}")
            elif fragment:
                anchors += 1
                titles = re.findall(r"^#{1,6}\s+(.+)$", destination.read_text(encoding="utf-8"), re.M)
                slugs = [re.sub(r"[^\w\s-]", "", title.lower()).replace(" ", "-") for title in titles]
                if fragment not in slugs:
                    errors.append(f"{label}: missing heading {target}")
    print(f"Documents={len(files)}, relative links={links}, heading references={anchors}, errors={len(errors)}")
    for error in errors:
        print(error)
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
