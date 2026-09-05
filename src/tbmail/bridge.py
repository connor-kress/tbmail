from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .profile import MailAccount

PROTOCOL_VERSION = 1
HEARTBEAT_MAX_AGE = 5.0
STALE_FILE_AGE = 24 * 60 * 60
LOG_LIMIT = 5 * 1024 * 1024
LOG_BACKUPS = 3


class BridgeError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        request_id: str,
        status: str = "error",
        details: dict[str, Any] | None = None,
        paths: IpcPaths | None = None,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.status = status
        self.details = details or {}
        self.paths = paths

    def json_value(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "error": str(self),
            "status": self.status,
            "request_id": self.request_id,
        }
        value.update(
            {
                key: item
                for key, item in self.details.items()
                if key
                not in {"error", "status", "requestId", "protocolVersion", "timestamp"}
            }
        )
        if "error" in self.details:
            value["bridge_error"] = self.details["error"]
        if self.paths:
            value["thunderbird_log"] = str(self.paths.thunderbird_log)
            value["bridge_log"] = str(self.paths.bridge_log)
        return value


@dataclass(frozen=True)
class IpcPaths:
    root: Path
    requests: Path
    responses: Path
    heartbeat: Path
    last_sync: Path
    bridge_log: Path
    launch_lock: Path
    thunderbird_log: Path


def ipc_paths(profile: Path) -> IpcPaths:
    state_home = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser()
    root = profile / "tbmail-ipc"
    return IpcPaths(
        root=root,
        requests=root / "requests",
        responses=root / "responses",
        heartbeat=root / "heartbeat.json",
        last_sync=root / "last-sync.json",
        bridge_log=root / "bridge.log",
        launch_lock=root / "launch.lock",
        thunderbird_log=state_home / "tbmail" / "thunderbird.log",
    )


def _prepare_paths(paths: IpcPaths) -> None:
    for directory in (paths.root, paths.requests, paths.responses):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)


def _atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _heartbeat_is_fresh(paths: IpcPaths) -> bool:
    try:
        heartbeat = _read_json(paths.heartbeat)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    timestamp = _timestamp(heartbeat.get("timestamp"))
    return bool(
        heartbeat.get("protocolVersion") == PROTOCOL_VERSION
        and timestamp is not None
        and 0 <= time.time() - timestamp <= HEARTBEAT_MAX_AGE
    )


def _cleanup_stale_files(paths: IpcPaths) -> None:
    cutoff = time.time() - STALE_FILE_AGE
    for directory in (paths.requests, paths.responses):
        for path in directory.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass


def _rotate_log(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    try:
        if path.stat().st_size < LOG_LIMIT:
            return
    except FileNotFoundError:
        return
    (path.parent / f"{path.name}.{LOG_BACKUPS}").unlink(missing_ok=True)
    for index in range(LOG_BACKUPS - 1, 0, -1):
        source = path.parent / f"{path.name}.{index}"
        if source.exists():
            os.replace(source, path.parent / f"{path.name}.{index + 1}")
    os.replace(path, path.parent / f"{path.name}.1")


def _profile_argument(command: tuple[str, ...], profile: Path) -> str:
    if command and Path(command[0]).name == "flatpak":
        app_home = Path.home() / ".var/app/org.mozilla.thunderbird_esr"
        try:
            relative = profile.resolve().relative_to(app_home.resolve())
        except ValueError:
            return str(profile)
        return str(Path.home() / relative)
    return str(profile)


def _launch(command: tuple[str, ...], profile: Path, paths: IpcPaths) -> int:
    if not command:
        raise OSError("Thunderbird command is empty")
    if "--headless" not in command:
        raise OSError("Thunderbird command must include --headless")
    arguments = list(command)
    if any(
        argument in {"--profile", "-profile", "-P", "--ProfileManager"}
        for argument in arguments
    ):
        raise OSError("Thunderbird command must not select a profile")
    arguments.extend(["--profile", _profile_argument(command, profile)])
    _rotate_log(paths.thunderbird_log)
    descriptor = os.open(
        paths.thunderbird_log,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o600,
    )
    try:
        file_actions = [
            (os.POSIX_SPAWN_DUP2, descriptor, 1),
            (os.POSIX_SPAWN_DUP2, descriptor, 2),
            (os.POSIX_SPAWN_CLOSE, descriptor),
        ]
        return os.posix_spawnp(
            arguments[0],
            arguments,
            os.environ.copy(),
            file_actions=file_actions,
            setsid=True,
        )
    finally:
        os.close(descriptor)


def _acquire_launch_lock(paths: IpcPaths) -> int | None:
    descriptor = os.open(paths.launch_lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    os.ftruncate(descriptor, 0)
    os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
    return descriptor


def execute(
    profile: Path,
    command: tuple[str, ...],
    operation: str,
    payload: dict[str, object],
    timeout: float,
) -> tuple[str, dict[str, Any]]:
    if timeout <= 0:
        raise ValueError("--timeout must be positive")
    deadline = time.monotonic() + timeout
    absolute_deadline = int((time.time() + timeout) * 1000)
    request_id = uuid.uuid4().hex
    paths = ipc_paths(profile)
    _prepare_paths(paths)
    _cleanup_stale_files(paths)
    request = {
        "protocolVersion": PROTOCOL_VERSION,
        "requestId": request_id,
        "operation": operation,
        "deadline": absolute_deadline,
        **payload,
    }
    request_path = paths.requests / f"{request_id}.json"
    response_path = paths.responses / f"{request_id}.json"
    _atomic_json(request_path, request)

    launch_lock: int | None = None
    try:
        if not _heartbeat_is_fresh(paths):
            launch_lock = _acquire_launch_lock(paths)
            if launch_lock is not None and not _heartbeat_is_fresh(paths):
                try:
                    _launch(command, profile, paths)
                except OSError as exc:
                    raise BridgeError(
                        f"Could not launch Thunderbird: {exc}",
                        request_id=request_id,
                        paths=paths,
                    ) from exc

        while time.monotonic() < deadline:
            try:
                response = _read_json(response_path)
            except FileNotFoundError:
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                continue
            except (ValueError, json.JSONDecodeError) as exc:
                raise BridgeError(
                    f"Invalid bridge response: {exc}",
                    request_id=request_id,
                    status="invalid",
                    paths=paths,
                ) from exc
            finally:
                if launch_lock is not None and _heartbeat_is_fresh(paths):
                    os.close(launch_lock)
                    launch_lock = None

            response_path.unlink(missing_ok=True)
            if response.get("protocolVersion") != PROTOCOL_VERSION:
                raise BridgeError(
                    "Bridge protocol version mismatch",
                    request_id=request_id,
                    status="invalid",
                    details=response,
                    paths=paths,
                )
            if response.get("requestId") != request_id:
                raise BridgeError(
                    "Bridge response request ID mismatch",
                    request_id=request_id,
                    status="invalid",
                    details=response,
                    paths=paths,
                )
            status = response.get("status")
            if status != "success":
                error = response.get("error")
                if isinstance(error, dict):
                    message = str(error.get("message", status))
                else:
                    message = str(error or f"Thunderbird operation failed: {status}")
                raise BridgeError(
                    message,
                    request_id=request_id,
                    status=str(status or "error"),
                    details=response,
                    paths=paths,
                )
            return request_id, response

        raise BridgeError(
            f"Thunderbird operation timed out after {timeout:g} seconds",
            request_id=request_id,
            status="timeout",
            paths=paths,
        )
    finally:
        request_path.unlink(missing_ok=True)
        if launch_lock is not None:
            os.close(launch_lock)


def stale_sync_warnings(
    profile: Path,
    accounts: list[MailAccount],
    max_age: float = 5 * 60,
) -> list[str]:
    path = ipc_paths(profile).last_sync
    try:
        state = _read_json(path)
        values = state.get("accounts")
        if state.get("protocolVersion") != PROTOCOL_VERSION or not isinstance(
            values, dict
        ):
            values = {}
    except (OSError, ValueError, json.JSONDecodeError):
        values = {}

    now = time.time()
    warnings = []
    for account in accounts:
        timestamp = _timestamp(values.get(account.server_id))
        if timestamp is None or timestamp > now + 60:
            warnings.append(
                f"Local mail for {account.name} has never been synchronized"
            )
        elif now - timestamp > max_age:
            minutes = max(1, int((now - timestamp) // 60))
            warnings.append(
                f"Local mail for {account.name} was last synchronized "
                f"{minutes} minutes ago"
            )
    return warnings
