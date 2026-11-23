#!/usr/bin/env python3
"""Apply the Agent sources and shared integration changes to a GPUAU checkout."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          text=True, stdout=subprocess.PIPE).stdout.strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("backend", "frontend"))
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--check", action="store_true", help="validate without writing files")
    args = parser.parse_args()

    here = Path(__file__).resolve().parent
    checkout = args.checkout.resolve()
    try:
        if Path(git(checkout, "rev-parse", "--show-toplevel")).resolve() != checkout:
            parser.error("Pass the root of the GPUAU/BE or GPUAU/FE checkout.")

        manifest = json.loads((here / "integration/sources.json").read_text())
        patch = here / "integration" / f"{args.component}.patch"
        files = []
        for name, original in manifest[args.component].items():
            source = here / args.component / name
            destination = checkout / name
            if not destination.resolve().is_relative_to(checkout) or destination.resolve() != destination:
                parser.error(f"Unexpected link in checkout: {name}")
            current = digest(destination) if destination.is_file() else None
            if current not in (original, digest(source)):
                parser.error(f"Agent source differs from the expected base: {name}")
            files.append((source, destination))

        git(checkout, "apply", "--check", str(patch))
        if args.check:
            print(f"{args.component}: shared changes and {len(files)} source files verified.")
            return

        git(checkout, "apply", str(patch))
        for source, destination in files:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        if args.component == "frontend":
            checksums = checkout / "FILE_SHA256.json"
            entries = json.loads(checksums.read_text())
            for source, destination in files:
                entries[destination.relative_to(checkout).as_posix()] = digest(source)
            checksums.write_text(json.dumps(entries, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        print(f"{args.component}: shared changes and {len(files)} source files applied.")
    except subprocess.CalledProcessError as error:
        parser.exit(1, f"Git checkout or shared changes did not match ({error.returncode}).\n")


if __name__ == "__main__":
    main()
