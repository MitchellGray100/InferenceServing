"""Stop local MiniTen API processes started by make run-api or setup-web."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import signal
import subprocess
import time


@dataclass(frozen=True)
class LocalProcess:
    """A local OS process with enough metadata for command matching."""

    pid: int
    command: str


def command_runs_app_main(command: str) -> bool:
    """Return true when a process command is the MiniTen Flask entrypoint."""
    normalized = " ".join(command.replace("\\", "/").split())
    return "-m app.main" in normalized or "-m app/main" in normalized


def matching_api_processes(processes: list[LocalProcess]) -> list[LocalProcess]:
    """Filter process rows down to local API processes, excluding this process."""
    current_pid = os.getpid()
    parent_pid = os.getppid()
    return [
        process
        for process in processes
        if process.pid not in {current_pid, parent_pid}
        and command_runs_app_main(process.command)
    ]


def list_processes() -> list[LocalProcess]:
    """List local processes using platform-native tools."""
    if os.name == "nt":
        return list_windows_processes()
    return list_posix_processes()


def list_windows_processes() -> list[LocalProcess]:
    """Read Windows process command lines through PowerShell."""
    command = [
        "powershell.exe",
        "-NoProfile",
        "-Command",
        (
            "Get-CimInstance Win32_Process | "
            "Select-Object ProcessId,CommandLine | "
            "ConvertTo-Json -Compress"
        ),
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []

    payload = json.loads(result.stdout)
    if isinstance(payload, dict):
        payload = [payload]

    processes: list[LocalProcess] = []
    for item in payload:
        pid = item.get("ProcessId")
        command_line = item.get("CommandLine")
        if isinstance(pid, int) and isinstance(command_line, str):
            processes.append(LocalProcess(pid=pid, command=command_line))
    return processes


def list_posix_processes() -> list[LocalProcess]:
    """Read POSIX process command lines through ps."""
    result = subprocess.run(
        ["ps", "-eo", "pid=,args="],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        return []

    processes: list[LocalProcess] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        processes.append(LocalProcess(pid=pid, command=command))
    return processes


def stop_process(process: LocalProcess) -> None:
    """Terminate a matching API process."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return

    try:
        os.kill(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(process.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)

    try:
        os.kill(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def stop_local_api_processes() -> int:
    """Stop all matching local API processes and return the count."""
    processes = matching_api_processes(list_processes())
    for process in processes:
        stop_process(process)
    return len(processes)


def main() -> int:
    """CLI entrypoint."""
    stopped = stop_local_api_processes()
    if stopped:
        print(f"Stopped {stopped} local MiniTen API process(es).")
    else:
        print("No local MiniTen API process was running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
