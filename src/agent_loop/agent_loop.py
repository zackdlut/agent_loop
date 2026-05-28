#!/usr/bin/env python3

import cmd
from anthropic import Anthropic, DefaultHttpxClient
from dotenv import load_dotenv

from common.http_print import httpx_print_event_hooks
import os
import subprocess

load_dotenv(override=True)
os.environ.setdefault("ANTHROPIC_BASE_URL", "http://10.67.34.44:11434")
os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "ollam")
os.environ.setdefault("ANTHROPIC_MODEL", "qwen3:latest")

SYSTEM_PROMPT = f"""
You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain.
"""

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
    }
]

client = Anthropic(http_client=DefaultHttpxClient(event_hooks=httpx_print_event_hooks()))


def check_deny_list(command: str) -> bool:
    deny_list = ["rm -rf /", "reboot", "shutdown", "halt", "poweroff", "mkfs", "dd if=", "> /dev/sda"]
    return any(deny in command for deny in deny_list)

def run_bash(command: str) -> str:
    # check deny list
    if check_deny_list(command):
        return "Error: Dangerous command blocked"
    # run command
    try:
        result = subprocess.run(command, shell=True, cmd = os.getcwd() ,capture_output=True, text=True, timeout=60)
        out = (result.stdout + result.stderr).strip()
        return out[:5000] if out else "No output"
    except subprocess.TimeoutExpired as e:
        return f"Error: Command timed out after {e.timeout} seconds"
    except Exception as e:
        return f"Error running command: {e}"


def handle_messages(messages: list[dict[str, str]]):
    while True:
        response = client.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL"),
            messages=messages,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            tool_choice="auto",
        )
        if response.stop_reason != "tool_use":
            return response
        
        # handle tool use
        messages.append({"role": "assistant", "content": response.content})
        tool_result = []
        for block in response.content:
            if block.type == "tool_use":
                output = run_bash(block.input["command"])
                tool_result.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "output": output,
                })
        messages.append({"role": "assistant", "content": tool_result})

def main_loop():
    chat_messages = []
    while True:
        # accept input message
        user_input = input("You: ")
        if user_input.strip().lower() in ["exit", "quit", "bye"]:
            break
        
        # handle input messages
        chat_messages.append({"role": "user", "content": user_input})
        repsonse = handle_messages(chat_messages)
        chat_messages.append({"role": "assistant", "content": repsonse})
        if isinstance(repsonse.content, list):
            for block in repsonse.content:
                if block.type == "text":
                    print(block.text)

if __name__ == "__main__":
    main_loop()