#!/usr/bin/env python3
"""Build the wheelhouse bundled with the Unity package for offline installs."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = SERVER_ROOT / "offline" / "wheels"


def run(command: list[str]) -> None:
    print("$", " ".join(command), flush=True)
    subprocess.run(command, cwd=SERVER_ROOT, check=True)


def strip_markers(requirements: Path, output: Path) -> None:
    """Remove environment markers so every locked distribution is downloaded."""
    lines = []
    for line in requirements.read_text(encoding="utf-8").splitlines():
        if " ; " in line:
            prefix = line.split(" ; ", 1)[0]
            suffix = " " + chr(92) if line.rstrip().endswith(chr(92)) else ""
            line = prefix + suffix
        lines.append(line)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--platform", default="win_amd64")
    parser.add_argument(
        "--python-versions",
        nargs="+",
        default=["3.11", "3.12", "3.13", "3.14"],
        help="CPython versions whose wheels should be included",
    )
    parser.add_argument("--index-url", help="Optional pip index URL")
    args = parser.parse_args()

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    for stale_wheel in output.glob("*.whl"):
        stale_wheel.unlink()

    with tempfile.TemporaryDirectory(prefix="unitymcp-wheelhouse-") as temp_dir:
        requirements = Path(temp_dir) / "requirements.txt"
        all_requirements = Path(temp_dir) / "requirements-all.txt"

        run(
            [
                "uv",
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements.txt",
                "--output-file",
                str(requirements),
            ]
        )
        strip_markers(requirements, all_requirements)

        for version in args.python_versions:
            abi = "cp" + version.replace(".", "")
            command = [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--dest",
                str(output),
                "--only-binary=:all:",
                "--platform",
                args.platform,
                "--python-version",
                version,
                "--implementation",
                "cp",
                "--abi",
                abi,
                "--no-deps",
                "--disable-pip-version-check",
                "--progress-bar",
                "off",
                "-r",
                str(all_requirements),
            ]
            if args.index_url:
                command.extend(["--index-url", args.index_url])
            run(command)

        build_dependencies = [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--dest",
            str(output),
            "--only-binary=:all:",
            "--platform",
            args.platform,
            "--python-version",
            args.python_versions[-1],
            "--implementation",
            "cp",
            "--abi",
            "cp" + args.python_versions[-1].replace(".", ""),
            "--disable-pip-version-check",
            "--progress-bar",
            "off",
            "setuptools>=69",
            "wheel",
        ]
        if args.index_url:
            build_dependencies.extend(["--index-url", args.index_url])
        run(build_dependencies)

    wheels = list(output.glob("*.whl"))
    print(f"Wrote {len(wheels)} wheels to {output}")


if __name__ == "__main__":
    main()
