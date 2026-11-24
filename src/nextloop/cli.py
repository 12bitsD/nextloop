from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from collections.abc import Sequence
from typing import Any, NoReturn

from .contracts import NextloopError, ToolCall, ToolSpec, TurnOutcome
from .harness import Harness


class CliUsageError(ValueError):
    pass


class CliArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise CliUsageError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = CliArgumentParser(
        prog="nextloop", description="Inspect and smoke-test a nextloop Harness"
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser(
        "check", help="validate configuration and worker handshakes"
    )
    check.add_argument("harness")
    tools = commands.add_parser(
        "tools", help="inspect or explicitly call a Tool"
    )
    tools_commands = tools.add_subparsers(dest="tools_command", required=True)
    tools_list = tools_commands.add_parser("list")
    tools_list.add_argument("harness")
    tools_call = tools_commands.add_parser("call")
    tools_call.add_argument("name")
    tools_call.add_argument("arguments")
    tools_call.add_argument("harness")
    skills = commands.add_parser("skills", help="inspect Skills")
    skills_commands = skills.add_subparsers(
        dest="skills_command", required=True
    )
    skills_list = skills_commands.add_parser("list")
    skills_list.add_argument("harness")
    skills_view = skills_commands.add_parser("view")
    skills_view.add_argument("name")
    skills_view.add_argument("harness")
    smoke = commands.add_parser("smoke", help="run one model-backed Turn")
    smoke.add_argument("harness")
    smoke.add_argument("--input", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in arguments
    if json_output:
        arguments = [
            "--json",
            *(item for item in arguments if item != "--json"),
        ]
    try:
        args = build_parser().parse_args(arguments)
        result = asyncio.run(_dispatch(args))
    except (NextloopError, ValueError, OSError) as exc:
        if json_output:
            print(
                json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
            )
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str))
    else:
        _print_human(args, result)
    return 0


async def _dispatch(args: argparse.Namespace) -> Any:
    harness = Harness.load(args.harness)
    try:
        match args.command:
            case "check":
                return await harness.check()
            case "smoke":
                outcome = await harness.run(args.input)
                return _turn_outcome(outcome)
            case "tools":
                return await _dispatch_tools(harness, args)
            case "skills":
                return await _dispatch_skills(harness, args)
            case _:
                raise NextloopError(f"Unknown command: {args.command}")
    finally:
        await harness.close()


async def _dispatch_tools(harness: Harness, args: argparse.Namespace) -> Any:
    await harness.check()
    if args.tools_command == "list":
        return [_tool_spec_dict(spec) for spec in harness.tools.catalog()]
    if args.tools_command == "call":
        call = ToolCall("cli_call", args.name, args.arguments)
        _, issues = harness.tools.validate([call])
        if issues:
            raise NextloopError("; ".join(issue.message for issue in issues))
        outcome = await harness.tools.execute(
            call,
            json.loads(args.arguments),
            session_id="cli",
            turn=0,
            step=0,
            cancel=asyncio.Event(),
        )
        return dataclasses.asdict(outcome)
    raise NextloopError(f"Unknown tools command: {args.tools_command}")


def _tool_spec_dict(spec: ToolSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "description": spec.description,
        "mode": spec.mode.value,
        "effect": spec.effect.value,
        "resources": list(spec.resources),
        "parameters": dict(spec.parameters),
    }


async def _dispatch_skills(harness: Harness, args: argparse.Namespace) -> Any:
    await harness.check()
    if harness.skills is None:
        raise NextloopError("Skills registry is not initialized")
    if args.skills_command == "list":
        return [dataclasses.asdict(spec) for spec in harness.skills.list()]
    if args.skills_command == "view":
        document = harness.skills.load(args.name)
        return {
            "spec": dataclasses.asdict(document.spec),
            "body": document.body,
        }
    raise NextloopError(f"Unknown skills command: {args.skills_command}")


def _turn_outcome(outcome: TurnOutcome) -> dict[str, Any]:
    return {
        "status": outcome.status.value,
        "session_id": outcome.session_id,
        "turn": outcome.turn,
        "step": outcome.step,
        "final_message_id": outcome.final_message_id,
        "error": outcome.error,
    }


def _print_human(args: argparse.Namespace, result: Any) -> None:
    match args.command:
        case "check":
            print("Harness OK")
            print("Tools: " + (", ".join(result["tools"]) or "none"))
            print("Skills: " + (", ".join(result["skills"]) or "none"))
        case "smoke":
            target = result.get("error") or result.get("final_message_id")
            print(f"{result['status']}: {target}")
        case "tools":
            _print_tools_human(args, result)
        case "skills":
            _print_skills_human(args, result)
        case _:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def _print_tools_human(args: argparse.Namespace, result: Any) -> None:
    if args.tools_command == "list":
        for item in result:
            # bits:allow(BCS007): Python has no brace blocks
            print(f"{item['name']}\t{item['mode']}\t{item['description']}")
        return
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def _print_skills_human(args: argparse.Namespace, result: Any) -> None:
    if args.skills_command == "list":
        for item in result:
            # bits:allow(BCS007): Python has no brace blocks
            print(f"{item['name']}\t{item['description']}")
        return
    print(result["body"])


if __name__ == "__main__":
    raise SystemExit(main())
