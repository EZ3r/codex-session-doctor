"""Private local state and atomic, durable recovery records."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from session_doctor import HEX_SHA, Unavailable, parse, thread_id, uint

REGISTRY_LIMIT = 8 * 1024 * 1024
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
REGISTRATION = {None, "pending", "registered"}
TEST = {None, "pending", "passed", "failed", "already_continued"}
FIELDS = {
    "source_thread_id", "candidate_path", "candidate_sha256", "registration", "test",
    "thread_id", "window_id", "codex_version", "codex_build_sha256", "verifier_commit",
    "verifier_sha256", "test_nonce", "test_start_line", "test_turn_id", "updated_at",
    "title_verified",
}


def state_directory(args):
    if getattr(args, "state_dir", None):
        return absolute_path(args.state_dir)
    state = os.environ.get("XDG_STATE_HOME")
    return absolute_path((Path(state) if state else Path.home() / ".local" / "state") / "session-doctor")


def absolute_path(path):
    path = Path(path).expanduser().absolute()
    if ".." in path.parts:
        raise Unavailable("STATE_PATH_MUST_NOT_CONTAIN_PARENT_COMPONENTS")
    return path


def private_stat(st, *, directory=False):
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077
            or (not directory and st.st_nlink != 1)):
        raise Unavailable("PRIVATE_PATH_HAS_UNSAFE_OWNER_OR_PERMISSIONS")


@contextmanager
def directory_fd(path, *, create=False, private=True):
    """Walk with openat/O_NOFOLLOW, including the parent components."""
    path = absolute_path(path)
    fd = os.open(path.anchor, DIR_FLAGS)
    try:
        for name in path.parts[1:]:
            try:
                child = os.open(name, DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(name, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
            ancestor = os.fstat(fd)
            if ancestor.st_uid not in (0, os.geteuid()):
                raise Unavailable("PRIVATE_PATH_HAS_UNTRUSTED_ANCESTOR_OWNER")
            if ancestor.st_mode & 0o022 and not ancestor.st_mode & stat.S_ISVTX:
                raise Unavailable("PRIVATE_PATH_HAS_WRITABLE_ANCESTOR")
        if private:
            private_stat(os.fstat(fd), directory=True)
        yield fd
    finally:
        os.close(fd)


def verify_private_files(path, *names):
    with directory_fd(path) as fd:
        for name in names:
            private_stat(os.stat(name, dir_fd=fd, follow_symlinks=False))


def check_state_root(path, codex_home, claude_home):
    path = absolute_path(path)
    roots = (codex_home, claude_home, Path.home() / ".codex", Path.home() / ".claude")
    if any(path.is_relative_to(Path(root).resolve()) for root in roots):
        raise Unavailable("STATE_DIR_MUST_BE_OUTSIDE_SOURCE_HOMES")
    return path


def recovery_key(codex_home, tid):
    return hashlib.sha256(os.fsencode(codex_home) + b"\0" + tid.encode()).hexdigest()


def check_entry(entry):
    if not isinstance(entry, dict) or not set(entry) <= FIELDS:
        raise ValueError
    if not thread_id(entry.get("source_thread_id")):
        raise ValueError
    if entry.get("registration") not in REGISTRATION or entry.get("test") not in TEST:
        raise ValueError
    for field in ("candidate_sha256", "codex_build_sha256", "verifier_sha256"):
        value = entry.get(field)
        if value is not None and (not isinstance(value, str) or not HEX_SHA.fullmatch(value)):
            raise ValueError
    for field in ("thread_id", "window_id", "test_nonce"):
        if entry.get(field) is not None and not thread_id(entry[field]):
            raise ValueError
    for field in ("candidate_path", "codex_version", "verifier_commit", "test_turn_id", "updated_at"):
        if entry.get(field) is not None and (not isinstance(entry[field], str) or len(entry[field]) > 4096
                                             or "\0" in entry[field]):
            raise ValueError
    if entry.get("candidate_path") and not Path(entry["candidate_path"]).is_absolute():
        raise ValueError
    if entry.get("test_start_line") is not None and not uint(entry["test_start_line"]):
        raise ValueError
    if entry.get("title_verified") is not None and type(entry["title_verified"]) is not bool:
        raise ValueError
    if entry.get("registration") and not all(entry.get(k) for k in
            ("candidate_path", "candidate_sha256", "window_id", "codex_version",
             "codex_build_sha256", "verifier_commit", "verifier_sha256")):
        raise ValueError
    if entry.get("registration") == "registered" and (
            not entry.get("thread_id") or entry["thread_id"] == entry["source_thread_id"]):
        raise ValueError
    if entry.get("test") and entry.get("registration") != "registered":
        raise ValueError
    if entry.get("test") in ("pending", "passed", "failed") and (
            not entry.get("test_nonce") or entry.get("test_start_line") is None):
        raise ValueError


class Registry:
    def __init__(self, fd, records, writable):
        self.fd = fd
        self.records = records
        self.writable = writable

    def put(self, key, entry):
        if not self.writable:
            raise RuntimeError("read-only registry")
        entry = {**entry, "updated_at": datetime.now(timezone.utc).isoformat()}
        check_entry(entry)
        records = {**self.records, key: entry}
        raw = json.dumps({"schema": 1, "recoveries": records}, sort_keys=True,
                         separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
        if len(raw) > REGISTRY_LIMIT:
            raise Unavailable("RECOVERY_REGISTRY_TOO_LARGE")
        temp = ".registry." + secrets.token_hex(16)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(raw)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, "registry.json", src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(temp, dir_fd=self.fd)
            except FileNotFoundError:
                pass
        self.records = records
        return entry


def read_records(fd, *, missing_ok=True):
    try:
        handle = os.open("registry.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        if not missing_ok:
            raise Unavailable("RECOVERY_REGISTRY_MISSING")
        return {}
    with os.fdopen(handle, "rb") as file:
        private_stat(os.fstat(file.fileno()))
        try:
            raw = file.read(REGISTRY_LIMIT + 1)
            if len(raw) > REGISTRY_LIMIT:
                raise ValueError
            value = parse(raw)
            if set(value) != {"schema", "recoveries"} or type(value["schema"]) is not int or value["schema"] != 1:
                raise ValueError
            records = value["recoveries"]
            if not isinstance(records, dict) or not all(HEX_SHA.fullmatch(key) for key in records):
                raise ValueError
            for entry in records.values():
                check_entry(entry)
        except (ValueError, TypeError, RecursionError) as exc:
            raise Unavailable("RECOVERY_REGISTRY_CORRUPT") from exc
    return records


@contextmanager
def registry(path, *, write=False):
    if not write:
        try:
            with directory_fd(path) as fd:
                records = read_records(fd)
                if not records:
                    try:
                        lock = os.stat("registry.lock", dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        private_stat(lock)
                        records = read_records(fd, missing_ok=False)
        except FileNotFoundError:
            records = {}
        yield Registry(None, records, False)
        return
    with directory_fd(path, create=True) as fd:
        # Once initialized, a missing registry is also suspicious. A lock is
        # deliberately never deleted as part of normal operations.
        try:
            lock = os.open("registry.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                           0o600, dir_fd=fd)
            fresh_lock = True
        except FileExistsError:
            lock = os.open("registry.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            fresh_lock = False
        with os.fdopen(lock, "r+b") as lock_file:
            private_stat(os.fstat(lock))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Unavailable("RECOVERY_OPERATION_ALREADY_RUNNING") from exc
            records = read_records(fd, missing_ok=fresh_lock)
            if fresh_lock and not records:
                bootstrap = Registry(fd, {}, True)
                # Write a valid empty registry before exposing the initialized lock.
                temp = ".registry.init." + secrets.token_hex(16)
                new_fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                try:
                    with os.fdopen(new_fd, "wb") as file:
                        file.write(b'{"schema":1,"recoveries":{}}\n')
                        file.flush()
                        os.fsync(file.fileno())
                    os.replace(temp, "registry.json", src_dir_fd=fd, dst_dir_fd=fd)
                    os.fsync(fd)
                finally:
                    try:
                        os.unlink(temp, dir_fd=fd)
                    except FileNotFoundError:
                        pass
                yield bootstrap
            else:
                yield Registry(fd, records, True)
