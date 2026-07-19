from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from . import gateway

if TYPE_CHECKING:
    from collections.abc import Sequence


PROGRAM_NAME = "codemcp-sidecar"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM_NAME,
        description="Internal command-line entrypoint for the pi-codemcp sidecar.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    serve = subcommands.add_parser(
        "serve",
        help="Run the sidecar as an MCP server.",
    )
    serve.add_argument(
        "--stdio",
        action="store_true",
        required=True,
        help="Serve the MCP protocol over standard input/output.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    raise ValueError(f"unknown command: {args.command}")


def _serve(args: argparse.Namespace) -> int:
    if args.stdio is not True:
        raise ValueError("serve requires --stdio")
    gateway.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
