from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from fastmcp import FastMCP
from pydantic import BaseModel


class NumberResult(BaseModel):
    value: int


class SavedResult(BaseModel):
    saved: bool
    identifier: str


parser = argparse.ArgumentParser()
parser.add_argument("role", choices=["alpha", "beta"])
args = parser.parse_args()

pid_file = os.environ.get("TEST_PID_FILE")
if pid_file:
    Path(pid_file).write_text(str(os.getpid()), encoding="utf-8")

cwd_file = os.environ.get("TEST_CWD_FILE")
if cwd_file:
    Path(cwd_file).write_text(os.getcwd(), encoding="utf-8")

mcp = FastMCP(f"fixture-{args.role}")

if args.role == "alpha":

    @mcp.tool
    def get_number(seed: int) -> NumberResult:
        """Return a deterministic number."""
        return NumberResult(value=seed + 1)

    @mcp.tool
    async def slow_number(delay_seconds: float) -> NumberResult:
        """Wait before returning, for cancellation tests."""
        await asyncio.sleep(delay_seconds)
        return NumberResult(value=1)

    @mcp.tool
    def reject_number(seed: int) -> NumberResult:
        """Reject one valid call, for semantic-failure tests."""
        raise PermissionError(f"HTTP status 403: seed {seed} is forbidden")

else:

    @mcp.tool
    def save_number(value: int) -> SavedResult:
        """Persist a number in the test fixture."""
        return SavedResult(saved=True, identifier=f"N-{value}")


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False, log_level="ERROR")
