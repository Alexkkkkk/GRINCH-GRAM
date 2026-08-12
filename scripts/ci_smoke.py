"""Dependency-free safety checks for the CI pipeline."""

from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IGNORED_PARTS = {".git", "__pycache__", "attached_assets", "data", "backups"}
PRIVATE_KEY_MARKERS = (
    "-----BEGIN " + "PRIVATE KEY-----",
    "-----BEGIN " + "RSA PRIVATE KEY-----",
    "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
)
AWS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")


def python_files() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*.py")
        if not any(part in IGNORED_PARTS for part in path.parts)
    ]


def main() -> None:
    required = ("main.py", "Dockerfile", "requirements.txt", ".github/workflows/ci.yml")
    missing = [name for name in required if not (ROOT / name).exists()]
    if missing:
        raise SystemExit(f"Missing required project files: {', '.join(missing)}")

    failures: list[str] = []
    for path in python_files():
        source = path.read_text(encoding="utf-8")
        try:
            ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            failures.append(f"{path.relative_to(ROOT)}:{exc.lineno}: {exc.msg}")
        if any(marker in source for marker in PRIVATE_KEY_MARKERS):
            failures.append(f"{path.relative_to(ROOT)} contains a private-key marker")

    for folder in (ROOT / "scripts", ROOT / ".github"):
        for path in folder.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".yml", ".yaml", ".sh"}:
                source = path.read_text(encoding="utf-8", errors="ignore")
                if AWS_KEY.search(source):
                    failures.append(f"{path.relative_to(ROOT)} contains an AWS key marker")

    if failures:
        raise SystemExit("\n".join(failures))
    print(f"Smoke checks passed: {len(python_files())} Python files parsed; no key markers found.")


if __name__ == "__main__":
    main()