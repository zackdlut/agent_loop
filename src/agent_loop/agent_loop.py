#!/usr/bin/env python3

from typing import Any

from anthropic import Anthropic, DefaultHttpxClient
from dotenv import load_dotenv

from common.http_print import build_httpx_event_hooks
import os
import re
import subprocess
from pathlib import Path

os.environ.setdefault("ANTHROPIC_BASE_URL", "http://10.67.34.44:11434")
os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "ollam")
os.environ.setdefault("ANTHROPIC_MODEL", "qwen3.5:9b")
os.environ.setdefault("HTTPX_PRINT_DEST", "httpx.log")
#curl http://10.67.34.44:11434/api/tags

load_dotenv(override=True)

client = Anthropic(
    http_client=DefaultHttpxClient(
        event_hooks=build_httpx_event_hooks(os.environ.get("HTTPX_PRINT_DEST"))
    )
)

MAX_TOOL_ROUNDS = 64


def workspace_root() -> Path:
    """Current working directory for file tools and bash; kept in sync at call time."""
    return Path.cwd().resolve()


def build_system_prompt() -> str:
    cwd = os.getcwd()
    return f"""You are a coding agent at {cwd}. Use bash to solve tasks. Act, don't explain.

Security: the bash tool runs commands with your full OS user privileges in the current working directory ({cwd}). It is not a sandbox. A deny list blocks only a few obviously destructive patterns; do not rely on it for safety. File read/write/glob are restricted to paths under this directory at the time of each call."""



# permission



PERMISSION_RULES = [
    {
        "tools": ["write_file", "edit_file"],
        "check": lambda args: not (workspace_root() / args["path"]).resolve().is_relative_to(workspace_root()),
        "reason": "Path escapes workspace",
    },
    {
        "tools": ["bash"],
        "check": lambda args: any(kw in args["command"] for kw in ["rm", "> /etc/", "chmod 777"]),
        "reason": "Potentially destructive command",
    },
]

def check_rules(tool_name: str, args: dict ) -> str | None:
    for rule in PERMISSION_RULES:
        if tool_name in rule["tools"] and rule["check"](args):
            return rule["reason"]
    return None

def _normalize_for_deny(command: str) -> str:
    return re.sub(r"\s+", " ", command.strip().lower())

def check_deny_list(command: str) -> str | None:
    """Best-effort blocklist; not a security boundary (see system prompt)."""
    normalized = _normalize_for_deny(command)
    deny_substrings = [
        "rm -rf /",
        "rm -rf /*",
        "> /dev/sda",
        "of=/dev/sd",
        "of=/dev/nvme",
        "dd if=",
        "mkfs",
        ":(){",
        "chmod -r /",
        "chmod -r /*",
        "mkfs.",
        "reboot",
        "shutdown",
        "halt",
        "poweroff",
        "init 0",
        "init 6",
        "systemctl poweroff",
        "systemctl reboot",
    ]
    if any(s in normalized for s in deny_substrings):
        return f"{command} is blocked on the deny list"
    return None

def ask_user(tool_name: str, args: dict) -> bool:
    print(f"Error: {tool_name} {args} is blocked by the permission rules")
    user_input = input("Do you want to proceed? (y/n): ")
    return user_input.lower() == "y"

def check_permission(block) -> bool:

    # check if the command is blocked on the deny list
    if block.name == "bash":
        reason = check_deny_list(block.input["command"])
        if reason:
            print(f"Error: {reason}")
            return False
    # check if the command is blocked by the permission rules
    reason = check_rules(block.name, block.input)
    if reason:
        allowed = ask_user(block.name, block.input)
        if not allowed:
            return False
    return True


# tools
def run_bash(command: str) -> str:
    try:
        result = subprocess.run(
            ["/bin/bash", "-lc", command],
            shell=False,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            timeout=60,
        )
        out = (result.stdout + result.stderr).strip()
        return out[:5000] if out else "No output"
    except subprocess.TimeoutExpired as e:
        return f"Error: Command timed out after {e.timeout} seconds"
    except Exception as e:
        return f"Error running command: {e}"


def safe_path(p: str) -> Path:
    root = workspace_root()
    path = (root / p).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit is not None and limit < len(lines):
            lines = lines[:limit] + [f"... {len(lines) - limit} more lines"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading file: {e}"


def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} characters to {path}"
    except Exception as e:
        return f"Error writing file: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        occurrences = text.count(old_text)
        if occurrences == 0:
            return f"Error: {old_text} not found in {path}"
        if occurrences > 1:
            return (
                f"Error: old_text appears {occurrences} times in {path}; "
                "include enough surrounding context so the match is unique, or split into multiple edits."
            )
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited text in {path}"
    except Exception as e:
        return f"Error editing file: {e}"


def run_glob(pattern: str) -> str:
    import glob as g

    try:
        root = workspace_root()
        results = []
        for match in g.glob(pattern, root_dir=root):
            if (root / match).resolve().is_relative_to(root):
                results.append(match)
        return "\n".join(results) if results else "No matches found"
    except Exception as e:
        return f"Error globbing: {e}"


TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command to run.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The path to the file to read.",
                },
                "limit": {
                    "type": "integer",
                    "description": "The number of lines to read.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write to a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The path to the file to write to.",
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Edit a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The path to the file to edit.",
                },
                "old_text": {
                    "type": "string",
                    "description": "The text to replace.",
                },
                "new_text": {
                    "type": "string",
                    "description": "The text to replace with.",
                },
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "glob",
        "description": "Glob a pattern.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The pattern to glob.",
                },
            },
            "required": ["pattern"],
        },
    },
]

TOOL_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}


def handle_messages(messages: list[dict[str, Any]]):
    for _ in range(MAX_TOOL_ROUNDS):
        try:
            response = client.messages.create(
                model=os.environ.get("ANTHROPIC_MODEL"),
                max_tokens=4096,
                messages=messages,
                system=build_system_prompt(),
                tools=TOOLS,
            )
        except Exception as e:
            raise RuntimeError(f"API request failed: {e}") from e

        if response.stop_reason != "tool_use":
            return response

        messages.append({"role": "assistant", "content": response.content})
        tool_result = []
        for block in response.content:
            if block.type == "tool_use":
                if not check_permission(block):
                    tool_result.append({"type": "tool_result", "tool_use_id": block.id, "content": "Permission denied"})
                    continue
                handler = TOOL_HANDLERS.get(block.name)
                if handler is None:
                    output = f"Unknown tool: {block.name}"
                else:
                    output = handler(**block.input)
                tool_result.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                    }
                )
        messages.append({"role": "user", "content": tool_result})

    raise RuntimeError(
        f"Exceeded maximum of {MAX_TOOL_ROUNDS} tool-use rounds without a final assistant message."
    )


def main_loop():
    chat_messages = []
    while True:
        user_input = input("You: ")
        if user_input.strip().lower() in ["exit", "quit", "bye"]:
            break

        turn_start = len(chat_messages)
        chat_messages.append({"role": "user", "content": user_input})
        try:
            response = handle_messages(chat_messages)
        except RuntimeError as e:
            print(f"Error: {e}")
            del chat_messages[turn_start:]
            continue

        chat_messages.append(
            {"role": "assistant", "content": response.content})
        if isinstance(response.content, list):
            for block in response.content:
                if block.type == "text":
                    print(f"Assistant: {block.text}")


if __name__ == "__main__":
    main_loop()
