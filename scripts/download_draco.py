from __future__ import annotations

import argparse

from openrouter_bench.draco import DEFAULT_DRACO_URL, download_draco


def main() -> int:
    parser = argparse.ArgumentParser(description="Download and cache the DRACO test split.")
    parser.add_argument("--output", default="data/draco/test.jsonl", help="Output JSONL path.")
    parser.add_argument("--url", default=DEFAULT_DRACO_URL, help="DRACO JSONL URL.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite an existing cache file.")
    args = parser.parse_args()

    path = download_draco(args.output, args.url, overwrite=args.overwrite)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
