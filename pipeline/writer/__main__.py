"""Writer stage entry point.

Usage:
  python -m pipeline.writer corpus discover|fetch|rank|transcribe|annotate|voices --corpus DIR
  python -m pipeline.writer analyze --corpus DIR [--out stats.json]
  python -m pipeline.writer write --story record.json --out JOB [--cast A,B] [--seconds 55] [--wpm 250]
  python -m pipeline.writer judge --job JOB [--rewrite]
"""
from __future__ import annotations

import sys
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if args else 2
    command, rest = args[0], args[1:]
    if command == "corpus":
        from . import corpus
        return corpus.main(rest)
    if command == "analyze":
        from . import analyze
        return analyze.main(rest)
    if command == "write":
        from . import write
        return write.main(rest)
    if command == "judge":
        from . import judge
        return judge.main(rest)
    print("error: unknown command %r\n%s" % (command, __doc__), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
