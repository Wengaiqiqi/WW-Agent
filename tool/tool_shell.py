from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass


@dataclass
class CommandOutput:
    stdout: str
    stderr: str
    exitCode: int | None
    interrupted: bool
    noOutputExpected: bool
    returnCodeInterpretation: str | None


def json_result(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def run_subprocess(command: list[str] | str, timeout: int = 10, shell: bool = False) -> str:
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=os.getcwd(),
        shell=shell,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        output = CommandOutput(
            stdout=stdout or "",
            stderr=f"Command exceeded timeout of {timeout} seconds",
            exitCode=None,
            interrupted=True,
            noOutputExpected=True,
            returnCodeInterpretation="timeout",
        )
        return json_result(asdict(output))
    output = CommandOutput(
        stdout=stdout,
        stderr=stderr,
        exitCode=proc.returncode,
        interrupted=False,
        noOutputExpected=not stdout.strip() and not stderr.strip(),
        returnCodeInterpretation=None if proc.returncode == 0 else f"exit_code:{proc.returncode}",
    )
    return json_result(asdict(output))


def run_python_code(code: str, timeout: int = 10) -> str:
    return run_subprocess([sys.executable, "-c", code], timeout=timeout, shell=False)


def run_shell_command(command: str, timeout: int = 30) -> str:
    if os.name == "nt":
        # Use cmd.exe for better compatibility and faster startup than PowerShell.
        shell_command = ["cmd.exe", "/c", command]
    else:
        shell_command = ["/bin/sh", "-lc", command]
    return run_subprocess(shell_command, timeout=timeout, shell=False)
