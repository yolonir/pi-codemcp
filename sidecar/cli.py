from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from . import gateway
from .json_types import JSON_OBJECT_ADAPTER, JSON_VALUE_ADAPTER
from .mcp_config import load_mcp_json
from .runtime_paths import RuntimePaths, resolve_runtime_paths
from .settings import load_settings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .json_types import JsonObject, JsonValue


PROGRAM_NAME = "codemcp"
INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM_NAME,
        description="Internal command-line entrypoint for the pi-codemcp sidecar.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    serve = subcommands.add_parser("serve", help="Run the sidecar as an MCP server.")
    _add_runtime_path_args(serve)
    serve.add_argument(
        "--stdio",
        action="store_true",
        required=True,
        help="Serve the MCP protocol over standard input/output.",
    )

    status = subcommands.add_parser("status", help="Print lazy sidecar runtime status as JSON.")
    _add_runtime_path_args(status)

    discover = subcommands.add_parser(
        "discover",
        help="Force-refresh one enabled upstream MCP server catalog.",
    )
    _add_runtime_path_args(discover)
    discover.add_argument("server", help="Configured upstream server name to discover.")

    search = subcommands.add_parser(
        "search",
        help="Search configured upstream MCP tools and saved chains.",
    )
    _add_runtime_path_args(search)
    search.add_argument("query", help="Words describing an upstream capability or saved chain.")
    search.add_argument("--limit", type=int, default=5, help="Maximum matches to return.")
    search.add_argument("--server", help="Restrict search to one configured server or chains.")

    execute = subcommands.add_parser(
        "execute",
        help="Type-check and execute one sandboxed Python MCP call graph.",
    )
    _add_runtime_path_args(execute)
    _add_text_input_args(execute, "code", "Sandboxed Python body to execute.")

    chain = subcommands.add_parser("chain", help="Manage and run saved MCP chains.")
    chain_subcommands = chain.add_subparsers(dest="chain_command", required=True)

    chain_list = chain_subcommands.add_parser("list", help="List saved chains as JSON.")
    _add_runtime_path_args(chain_list)

    chain_run = chain_subcommands.add_parser("run", help="Execute one saved chain.")
    _add_runtime_path_args(chain_run)
    chain_run.add_argument("name", help="Saved chain name.")
    _add_json_input_args(chain_run, "input", "Saved-chain input object.", default="{}")

    chain_save = chain_subcommands.add_parser("save", help="Validate and save one chain.")
    _add_runtime_path_args(chain_save)
    chain_save.add_argument("name", help="Saved chain name.")
    chain_save.add_argument("--scope", choices=["project", "global"], default="project")
    chain_save.add_argument("--description", required=True, help="Saved chain description.")
    _add_text_input_args(chain_save, "code", "Sandboxed Python body to save.")
    _add_json_input_args(chain_save, "input-schema", "Input JSON Schema object.")
    _add_json_input_args(chain_save, "output-schema", "Output JSON Schema object.")

    chain_revalidate = chain_subcommands.add_parser(
        "revalidate",
        help="Revalidate one scoped saved chain against the current catalog.",
    )
    _add_runtime_path_args(chain_revalidate)
    chain_revalidate.add_argument("name", help="Saved chain name.")
    chain_revalidate.add_argument("--scope", choices=["project", "global"], required=True)

    chain_delete = chain_subcommands.add_parser("delete", help="Delete one unused saved chain.")
    _add_runtime_path_args(chain_delete)
    chain_delete.add_argument("name", help="Saved chain name.")
    chain_delete.add_argument("--scope", choices=["project", "global"], required=True)

    doctor = subcommands.add_parser("doctor", help="Check local sidecar paths and config.")
    _add_runtime_path_args(doctor)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            return _serve(args)
        return asyncio.run(_run_one_shot(args))
    except KeyboardInterrupt:
        _write_stderr(f"{PROGRAM_NAME}: interrupted")
        return INTERRUPTED
    except (OSError, RuntimeError, TypeError, ValueError, ValidationError) as error:
        _write_stderr(f"{PROGRAM_NAME}: {error}")
        return 1


def _serve(args: argparse.Namespace) -> int:
    if args.stdio is not True:
        raise ValueError("serve requires --stdio")
    gateway.configure_runtime_paths(_runtime_paths_from_args(args))
    try:
        gateway.main()
    finally:
        gateway.configure_runtime_paths(None)
    return 0


async def _run_one_shot(args: argparse.Namespace) -> int:
    if args.command == "doctor":
        doctor_result = _doctor(args)
        _print_json(doctor_result)
        return 0 if doctor_result["ok"] is True else 1

    runtime = _runtime_from_args(args)
    try:
        runtime_result = await _dispatch_runtime_command(args, runtime)
    finally:
        await runtime.close()
    _print_json(runtime_result)
    return 0


async def _dispatch_runtime_command(
    args: argparse.Namespace,
    runtime: gateway.GatewayRuntime,
) -> BaseModel:
    trace_id = gateway.new_trace_id("cli")
    if args.command == "status":
        return runtime.status()
    if args.command == "discover":
        return await runtime.discover(args.server)
    if args.command == "search":
        return await runtime.search(
            args.query,
            args.limit,
            args.server,
            trace_id=trace_id,
        )
    if args.command == "execute":
        return await runtime.execute(_read_text_input(args, "code"), trace_id)
    if args.command == "chain":
        return await _dispatch_chain_command(args, runtime, trace_id)
    raise ValueError(f"unknown command: {args.command}")


async def _dispatch_chain_command(
    args: argparse.Namespace,
    runtime: gateway.GatewayRuntime,
    trace_id: str,
) -> BaseModel:
    if args.chain_command == "list":
        return runtime.chains.list(trace_id)
    if args.chain_command == "run":
        return await runtime.chains.execute(
            args.name,
            _read_json_object_input(args, "input"),
            trace_id,
        )
    if args.chain_command == "save":
        return await runtime.chains.save(
            scope=args.scope,
            name=args.name,
            description=args.description,
            code=_read_text_input(args, "code"),
            input_schema=_read_json_object_input(args, "input_schema"),
            output_schema=_read_json_object_input(args, "output_schema"),
            trace_id=trace_id,
        )
    if args.chain_command == "revalidate":
        return await runtime.chains.revalidate(args.name, args.scope, trace_id)
    if args.chain_command == "delete":
        return await runtime.chains.delete(args.name, args.scope, trace_id)
    raise ValueError(f"unknown chain command: {args.chain_command}")


def _doctor(args: argparse.Namespace) -> JsonObject:
    paths = _runtime_paths_from_args(args)
    checks = [
        _config_check(paths),
        _settings_check(paths),
        _writable_path_check("oauth_dir", paths.oauth_dir),
        _writable_path_check("catalog_dir", paths.catalog_dir),
        _writable_path_check("global_chains_dir", paths.global_chains_dir),
        _optional_writable_path_check("project_chains_dir", paths.project_chains_dir),
    ]
    return JSON_OBJECT_ADAPTER.validate_python({
        "ok": all(check["ok"] is True for check in checks),
        "paths": {
            "config_path": str(paths.config_path),
            "oauth_dir": str(paths.oauth_dir),
            "catalog_dir": str(paths.catalog_dir),
            "settings_path": str(paths.settings_path),
            "global_chains_dir": str(paths.global_chains_dir),
            "project_chains_dir": str(paths.project_chains_dir)
            if paths.project_chains_dir is not None
            else None,
        },
        "checks": checks,
    })


def _config_check(paths: RuntimePaths) -> JsonObject:
    try:
        load_mcp_json(paths.config_path)
    except (OSError, RuntimeError, TypeError, ValueError, ValidationError) as error:
        return _check("mcp_config", ok=False, message=str(error))
    return _check("mcp_config", ok=True, message="ok")


def _settings_check(paths: RuntimePaths) -> JsonObject:
    try:
        load_settings(paths.settings_path)
    except (OSError, RuntimeError, TypeError, ValueError, ValidationError) as error:
        return _check("settings", ok=False, message=str(error))
    if paths.settings_path.exists():
        return _check("settings", ok=True, message="ok")
    return _check("settings", ok=True, message="not found; defaults will be used")


def _optional_writable_path_check(name: str, path: Path | None) -> JsonObject:
    if path is None:
        return _check(name, ok=True, message="not configured")
    return _writable_path_check(name, path)


def _writable_path_check(name: str, path: Path) -> JsonObject:
    target = path if path.exists() else _nearest_existing_parent(path)
    if target is None:
        return _check(name, ok=False, message="no existing parent directory")
    if os.access(target, os.W_OK):
        return _check(name, ok=True, message=f"writable via {target}")
    return _check(name, ok=False, message=f"not writable via {target}")


def _nearest_existing_parent(path: Path) -> Path | None:
    current = path
    while current != current.parent:
        current = current.parent
        if current.exists():
            return current
    return current if current.exists() else None


def _check(name: str, *, ok: bool, message: str) -> JsonObject:
    return {"name": name, "ok": ok, "message": message}


def _runtime_from_args(args: argparse.Namespace) -> gateway.GatewayRuntime:
    paths = _runtime_paths_from_args(args)
    return gateway.GatewayRuntime.create(
        paths.config_path,
        paths.oauth_dir,
        paths.catalog_dir,
        paths.settings_path,
        global_chain_dir=paths.global_chains_dir,
        project_chain_dir=paths.project_chains_dir,
    )


def _runtime_paths_from_args(args: argparse.Namespace) -> RuntimePaths:
    return resolve_runtime_paths(
        agent_dir=_path_from_namespace(args, "agent_dir"),
        config_path=_path_from_namespace(args, "config_path"),
        settings_path=_path_from_namespace(args, "settings_path"),
        oauth_dir=_path_from_namespace(args, "oauth_dir"),
        catalog_dir=_path_from_namespace(args, "catalog_dir"),
        global_chains_dir=_path_from_namespace(args, "global_chains_dir"),
        project_chains_dir=_path_from_namespace(args, "project_chains_dir"),
    )


def _path_from_namespace(args: argparse.Namespace, name: str) -> Path | None:
    value = getattr(args, name, None)
    return value if isinstance(value, Path) else None


def _path_arg(value: str) -> Path:
    return Path(value).expanduser()


def _add_runtime_path_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--agent-dir", type=_path_arg, help="Pi agent directory.")
    parser.add_argument("--config", dest="config_path", type=_path_arg, help="MCP config path.")
    parser.add_argument("--settings", dest="settings_path", type=_path_arg, help="Settings path.")
    parser.add_argument("--oauth-dir", type=_path_arg, help="OAuth token storage directory.")
    parser.add_argument("--catalog-dir", type=_path_arg, help="Tool catalog cache directory.")
    parser.add_argument(
        "--global-chains-dir",
        type=_path_arg,
        help="Global saved-chain manifest directory.",
    )
    parser.add_argument(
        "--project-chains-dir",
        type=_path_arg,
        help="Project saved-chain manifest directory.",
    )


def _add_text_input_args(
    parser: argparse.ArgumentParser,
    name: str,
    help_text: str,
) -> None:
    dashed = name.replace("_", "-")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(f"--{dashed}", dest=name, help=help_text)
    group.add_argument(
        f"--{dashed}-file",
        dest=f"{name}_file",
        type=_path_arg,
        help=f"Read {dashed} from a UTF-8 file.",
    )


def _add_json_input_args(
    parser: argparse.ArgumentParser,
    name: str,
    help_text: str,
    *,
    default: str | None = None,
) -> None:
    attribute = name.replace("-", "_")
    group = parser.add_mutually_exclusive_group(required=default is None)
    group.add_argument(f"--{name}", dest=attribute, help=help_text)
    group.add_argument(
        f"--{name}-file",
        dest=f"{attribute}_file",
        type=_path_arg,
        help=f"Read {name} JSON from a UTF-8 file.",
    )
    if default is not None:
        parser.set_defaults(**{f"{attribute}_default": default})


def _read_text_input(args: argparse.Namespace, name: str) -> str:
    raw = getattr(args, name, None)
    if isinstance(raw, str):
        return raw
    file = getattr(args, f"{name}_file", None)
    if isinstance(file, Path):
        return file.read_text(encoding="utf-8")
    raise ValueError(f"missing {name} input")


def _read_json_object_input(args: argparse.Namespace, name: str) -> JsonObject:
    return JSON_OBJECT_ADAPTER.validate_python(_read_json_input(args, name))


def _read_json_input(args: argparse.Namespace, name: str) -> JsonValue:
    raw = getattr(args, name, None)
    if isinstance(raw, str):
        return _parse_json(raw, name)
    file = getattr(args, f"{name}_file", None)
    if isinstance(file, Path):
        return _parse_json(file.read_text(encoding="utf-8"), str(file))
    default = getattr(args, f"{name}_default", None)
    if isinstance(default, str):
        return _parse_json(default, name)
    raise ValueError(f"missing {name} JSON input")


def _parse_json(value: str, label: str) -> JsonValue:
    try:
        parsed: object = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON for {label}: {error.msg}") from error
    return JSON_VALUE_ADAPTER.validate_python(parsed)


def _print_json(value: BaseModel | JsonObject) -> None:
    if isinstance(value, BaseModel):
        _write_stdout(value.model_dump_json(indent=2))
        return
    _write_stdout(json.dumps(value, indent=2, sort_keys=True))


def _write_stdout(value: str) -> None:
    sys.stdout.write(f"{value}\n")


def _write_stderr(value: str) -> None:
    sys.stderr.write(f"{value}\n")


if __name__ == "__main__":
    raise SystemExit(main())
