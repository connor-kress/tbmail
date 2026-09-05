from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import time
import uuid
from contextlib import contextmanager
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
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, OverflowError):
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


def _launch(
    command: tuple[str, ...], profile: Path, paths: IpcPaths, token: str = ""
) -> int:
    if not command:
        raise OSError("Thunderbird command is empty")
    if "--headless" not in command:
        raise OSError("Thunderbird command must include --headless")
    arguments = list(command)
    environment = os.environ.copy()
    environment["TBMAIL_STARTUP_TOKEN"] = token
    environment["TBMAIL_SAFETY_DEADLINE"] = str(int((time.time() + 1800) * 1000))
    if Path(command[0]).name == "flatpak":
        index = arguments.index("run") + 1
        arguments[index:index] = [
            f"--env={key}={environment[key]}"
            for key in ("TBMAIL_STARTUP_TOKEN", "TBMAIL_SAFETY_DEADLINE")
        ]
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
            environment,
            file_actions=file_actions,
            setsid=True,
        )
    finally:
        os.close(descriptor)


def state_directory(profile: Path) -> Path:
    home = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser()
    key = hashlib.sha256(os.fsencode(profile.resolve())).hexdigest()
    return home / "tbmail" / key


def process_identity(pid: int) -> dict[str, object]:
    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return {
        "pid": pid,
        "starttime": stat[19],
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    }


@contextmanager
def profile_lock(profile: Path, timeout: float = 300):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("--timeout must be positive")
    directory = state_directory(profile)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    descriptor = os.open(
        directory / "operation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BridgeError(
                        "Timed out waiting for another tbmail operation",
                        request_id="",
                        status="busy",
                    )
                time.sleep(0.1)
        # Never replace or unlink the lock inode. Metadata is advisory only.
        os.ftruncate(descriptor, 0)
        os.write(descriptor, json.dumps(process_identity(os.getpid())).encode())
        yield
    finally:
        os.close(descriptor)


def profile_active(profile: Path) -> bool:
    descriptor = os.open(
        profile / ".parentlock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        try:
            fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(descriptor)


def _owner(profile: Path) -> dict[str, Any]:
    try:
        return _read_json(state_directory(profile) / "managed.json")
    except (FileNotFoundError, ValueError):
        return {}


def _owned(profile: Path, paths: IpcPaths) -> bool:
    if not _heartbeat_is_fresh(paths):
        return False
    heartbeat = _read_json(paths.heartbeat)
    token = _owner(profile).get("token")
    return bool(
        token
        and heartbeat.get("startupToken") == token
        and heartbeat.get("headless") is True
    )


def _launcher_alive(owner: dict[str, Any]) -> bool:
    identity = owner.get("launcher")
    if identity is None:
        pending_until = owner.get("pending_until")
        return isinstance(pending_until, (int, float)) and time.time() < pending_until
    if not isinstance(identity, dict) or not isinstance(identity.get("pid"), int):
        return False
    try:
        try:
            if os.waitpid(identity["pid"], os.WNOHANG)[0]:
                return False
        except ChildProcessError:
            pass
        if (
            Path(f"/proc/{identity['pid']}/stat")
            .read_text()
            .rsplit(")", 1)[1]
            .split()[0]
            == "Z"
        ):
            return False
        return process_identity(identity["pid"]) == identity
    except (FileNotFoundError, ProcessLookupError):
        return False


def start(
    profile: Path, command: tuple[str, ...], timeout: float = 60
) -> dict[str, object]:
    with profile_lock(profile, timeout):
        paths = ipc_paths(profile)
        _prepare_paths(paths)
        if profile_active(profile):
            managed = False
            if _owned(profile, paths):
                try:
                    _, identity = execute_locked(
                        profile, (), "identify", {}, min(timeout, 5)
                    )
                    managed = (
                        identity.get("startupToken") == _owner(profile).get("token")
                        and identity.get("headless") is True
                    )
                except BridgeError:
                    pass
            return {
                "status": "already_running",
                "managed": managed,
                "message": "Existing Thunderbird left unchanged",
            }
        if _launcher_alive(_owner(profile)):
            raise BridgeError(
                "A managed launcher is still starting or exiting; "
                "retry after 'tbmail stop'",
                request_id="",
                status="busy",
                paths=paths,
            )
        owner_path = state_directory(profile) / "managed.json"
        owner_path.unlink(missing_ok=True)
        paths.heartbeat.unlink(missing_ok=True)
        (paths.root / "drain.json").unlink(missing_ok=True)
        token = uuid.uuid4().hex
        # Retain a pending launch if the CLI dies between spawn and PID recording.
        _atomic_json(
            owner_path,
            {"token": token, "launcher": None, "pending_until": time.time() + 1800},
        )
        try:
            pid = _launch(command, profile, paths, token)
        except OSError:
            owner_path.unlink(missing_ok=True)
            raise
        try:
            identity = process_identity(pid)
        except FileNotFoundError:
            identity = {"pid": pid, "starttime": None, "boot_id": None}
        _atomic_json(owner_path, {"token": token, "launcher": identity})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _owned(profile, paths) and profile_active(profile):
                return {
                    "status": "started",
                    "managed": True,
                    "message": "Managed headless Thunderbird started; "
                    "fixed 30-minute safety deadline",
                }
            time.sleep(0.1)
        raise BridgeError(
            "Thunderbird startup timed out; run 'tbmail stop' to clean up",
            request_id="",
            status="timeout",
            paths=paths,
        )


def stop(profile: Path, timeout: float = 300) -> dict[str, object]:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("--timeout must be positive")
    paths = ipc_paths(profile)
    owner = _owner(profile)
    # Publish before waiting for the operation lock so active sync can drain.
    if owner.get("token") and paths.root.is_dir():
        _atomic_json(paths.root / "drain.json", {"startupToken": owner["token"]})
    deadline = time.monotonic() + timeout
    with profile_lock(profile, timeout):
        while not profile_active(profile) and _launcher_alive(_owner(profile)):
            if time.monotonic() >= deadline:
                raise BridgeError(
                    "Managed launcher has not released; retry 'tbmail stop'. "
                    "No process was killed",
                    request_id="",
                    status="timeout",
                    paths=paths,
                )
            time.sleep(0.1)
        if not profile_active(profile):
            (state_directory(profile) / "managed.json").unlink(missing_ok=True)
            return {"status": "stopped", "managed": False}
        while not _heartbeat_is_fresh(paths) and _launcher_alive(_owner(profile)):
            if time.monotonic() >= deadline:
                raise BridgeError(
                    "Managed bridge has not responded; drain signal retained. "
                    "Retry 'tbmail stop'",
                    request_id="",
                    status="timeout",
                    paths=paths,
                )
            time.sleep(0.1)
        if not _owned(profile, paths):
            return {
                "status": "not_owned",
                "managed": False,
                "message": "Unowned Thunderbird left unchanged",
            }
        # Recheck after the lock: a concurrent start may have changed the token.
        _atomic_json(
            paths.root / "drain.json", {"startupToken": _owner(profile)["token"]}
        )
        while time.monotonic() < deadline:
            if not profile_active(profile):
                (state_directory(profile) / "managed.json").unlink(missing_ok=True)
                return {"status": "stopped", "managed": False}
            time.sleep(0.1)
        raise BridgeError(
            "Thunderbird is still draining; no process was killed. Retry 'tbmail stop'",
            request_id="",
            status="timeout",
            paths=paths,
        )


def execute(
    profile: Path,
    command: tuple[str, ...],
    operation: str,
    payload: dict[str, object],
    timeout: float,
) -> tuple[str, dict[str, Any]]:
    with profile_lock(profile, timeout):
        return execute_locked(profile, command, operation, payload, timeout)


def execute_locked(
    profile: Path,
    command: tuple[str, ...],
    operation: str,
    payload: dict[str, object],
    timeout: float,
) -> tuple[str, dict[str, Any]]:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("--timeout must be positive")
    deadline = time.monotonic() + timeout
    absolute_deadline = int((time.time() + timeout) * 1000)
    request_id = uuid.uuid4().hex
    paths = ipc_paths(profile)
    _prepare_paths(paths)
    if not _heartbeat_is_fresh(paths) or not profile_active(profile):
        raise BridgeError(
            "No Thunderbird bridge instance is available. "
            "Run 'tbmail start' and retry.",
            request_id=request_id,
            status="absent_instance",
            paths=paths,
        )
    while operation != "identify" and _read_json(paths.heartbeat).get("active"):
        if time.monotonic() >= deadline:
            raise BridgeError(
                "Previous Thunderbird operation is still draining",
                request_id=request_id,
                status="busy",
                paths=paths,
            )
        time.sleep(0.1)
        if not _heartbeat_is_fresh(paths):
            raise BridgeError(
                "No Thunderbird bridge instance is available. "
                "Run 'tbmail start' and retry.",
                request_id=request_id,
                status="absent_instance",
                paths=paths,
            )
    if operation != "identify" and _read_json(paths.heartbeat).get("draining"):
        raise BridgeError(
            "Thunderbird is stopping; wait for 'tbmail stop', "
            "then run 'tbmail start' and retry",
            request_id=request_id,
            status="busy",
            paths=paths,
        )
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

    try:
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
        if timestamp is None or timestamp > now:
            warnings.append(
                f"Local mail for {account.name} has never been synchronized"
            )
        elif now - timestamp >= max_age:
            minutes = max(1, int((now - timestamp) // 60))
            warnings.append(
                f"Local mail for {account.name} was last synchronized "
                f"{minutes} minutes ago"
            )
    return warnings


@dataclass
class AccountFreshness:
    account: str
    server_key: str
    needs_sync: bool
    last_sync: str | None


@dataclass
class FreshnessResult:
    needs_sync: bool
    accounts: list[AccountFreshness]
    message: str


def sync_freshness(profile: Path, accounts: list[MailAccount]) -> FreshnessResult:
    try:
        state = _read_json(ipc_paths(profile).last_sync)
    except (FileNotFoundError, ValueError):
        state = {}
    values = state.get("accounts", {})
    if state.get("protocolVersion") != PROTOCOL_VERSION or not isinstance(values, dict):
        values = {}
    now = time.time()
    results = []
    for account in accounts:
        raw = values.get(account.server_id)
        timestamp = _timestamp(raw)
        fresh = timestamp is not None and 0 <= now - timestamp < 300
        results.append(
            AccountFreshness(
                account.name,
                account.server_id,
                not fresh,
                raw if isinstance(raw, str) else None,
            )
        )
    needs_sync = any(account.needs_sync for account in results)
    return FreshnessResult(
        needs_sync,
        results,
        "Synchronization needed"
        if needs_sync
        else "All selected accounts synchronized successfully "
        "less than five minutes ago; sync skipped",
    )
