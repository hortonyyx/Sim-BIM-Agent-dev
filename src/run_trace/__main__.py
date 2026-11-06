"""Export one or more saved runs into a local interactive trace."""

import argparse
from pathlib import Path
import sys

from .export import export_trace, find_sources


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path,
                        help="native run, DeepSeek headless session, or paired experiment directory")
    parser.add_argument("--output", type=Path, required=True, help="new directory for the trace")
    parser.add_argument("--title", default="Sim-BIM 运行轨迹")
    args = parser.parse_args(argv)
    try:
        sources = [source for path in args.runs for source in find_sources(path)]
        print(export_trace(sources, args.output, args.title))
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
