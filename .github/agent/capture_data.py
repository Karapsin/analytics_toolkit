"""Inert bounded capture metadata and descriptor-anchored private originals.

No admission is issued here. Paths are trusted ledger components, never model
paths. Existing bytes are immutable; interrupted publication fails closed.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import math
import os
import re
import stat
import sys
import time
import uuid
from pathlib import Path

MAX_ID = 9007199254740991


class Pending(RuntimeError):
    """Missing genuine capability/evidence; never a request for owner upload."""


class BudgetPending(Pending):
    """Joint read/retention budget exhausted before an additional operation."""


class Budget:
    """Conservative single operation accounting, including future retained copies.

    HTTP prepays bounded original/index copies; Store consumes those credits.
    The final64KiB is only for a small fixed capacity-pending observation.
    """

    def __init__(self, limit=16 * 1024 * 1024, used=0):
        if (
            type(limit) is not int
            or not 65536 <= limit <= 16 * 1024 * 1024
            or type(used) is not int
            or not 0 <= used <= limit
        ):
            raise ValueError("Invalid joint capture budget")
        self.limit, self.used, self.credit = limit, used, 0

    @property
    def remaining(self):
        return max(0, self.limit - 65536 - self.used)

    def charge(self, count, *, retained=False, emergency=False):
        if type(count) is not int or count < 0:
            raise ValueError("Invalid capture budget charge")
        take = min(count, self.credit) if retained else 0
        extra = count - take
        maximum = self.limit if emergency else self.limit - 65536
        if self.used + extra > maximum:
            raise BudgetPending("Joint capture evidence budget exhausted")
        self.credit -= take
        self.used += extra

    def response_limit(self, maximum):
        result = min(maximum, (self.remaining - 131072) // 8 - 1)
        if result < 1:
            raise BudgetPending("Joint capture response capacity unavailable")
        return result

    def reserve_response(self, maximum):
        limit = self.response_limit(maximum)
        held = 8 * limit + 131080
        self.charge(held)
        return ResponseReservation(self, limit, held)

    def response(self, captured, observed):
        # Count exact observed input plus seven complete retained/index copies
        # and fixed header/receipt allowance before exposing the observation.
        copies = captured * 7 + 65536
        self.charge(observed + copies)
        self.credit += copies


class ResponseReservation:
    """Already charged capacity; only unused unobserved bytes are released."""

    def __init__(self, budget, limit, held):
        self.budget, self.limit, self.held = budget, limit, held
        self.settled = False

    def settle(self, captured, observed, header_bytes):
        if (
            self.settled
            or any(type(n) is not int for n in (captured, observed, header_bytes))
            or not 0 <= captured <= self.limit
            or not captured <= observed <= self.limit + 1
            or not 0 <= header_bytes <= 65536
        ):
            raise BudgetPending("Invalid bounded response reservation settlement")
        copies = captured * 7 + 65536
        actual = observed + header_bytes + copies
        if actual > self.held or self.budget.used < self.held:
            raise BudgetPending("Response exceeds reserved joint capacity")
        self.budget.used -= self.held - actual
        self.budget.credit += copies
        self.settled = True


def joint(http, store=None):
    budget = getattr(http, "budget", None)
    if budget is None:
        budget = Budget()
        http.budget = budget
    if store is not None:
        if store.budget is not None and store.budget is not budget:
            if budget.used or budget.credit:
                raise Pending("Split capture evidence budget")
            budget = store.budget
            http.budget = budget
        store.budget = budget
    return budget


def sibling(name):
    if name not in {
        "capture_data",
        "capture_transport",
        "capture_receiver",
        "capture_sandbox",
        "capture_isolation",
    }:
        raise ValueError("Unsupported trusted module")
    path = Path(__file__).with_name(name + ".py")
    key = "atk_capture_source:" + str(path.resolve())
    if key in sys.modules:
        module = sys.modules[key]
        if Path(module.__file__).resolve() != path.resolve():
            raise Pending("Trusted capture module identity changed")
        return module
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:
        raise Pending("Trusted capture source unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def integer(value, minimum=1):
    return type(value) is int and minimum <= value <= MAX_ID


def parse(raw, limit=2097152):
    """Reject finite-number, duplicate, depth and allocation surprises as data."""
    if not isinstance(raw, bytes) or len(raw) > limit:
        raise ValueError("Capture metadata byte limit")
    text = raw.decode("utf-8", errors="strict")
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > 64:
                raise ValueError("Capture metadata depth limit")
        elif char in "]}":
            depth -= 1

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate capture metadata key")
            result[key] = value
        return result

    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("Nonfinite capture metadata")
        return result

    def constant(_value):
        raise ValueError("Nonfinite capture metadata")

    value = json.loads(text, object_pairs_hook=pairs, parse_float=number, parse_constant=constant)
    todo = [value]
    count = 0
    while todo:
        item = todo.pop()
        count += 1
        if count > 100000:
            raise ValueError("Capture metadata node limit")
        if isinstance(item, str):
            if len(item.encode("utf-8")) > 524288:
                raise ValueError("Capture metadata string limit")
        elif isinstance(item, dict):
            todo.extend(item.keys())
            todo.extend(item.values())
        elif isinstance(item, list):
            todo.extend(item)
        elif type(item) is int and abs(item) > MAX_ID:
            raise ValueError("Capture integer overflow")
    return value


def matches(value, rule, definitions):
    """Only the fixed schema's data keywords; no evaluation or external refs."""
    if "$ref" in rule:
        prefix = "#/$defs/"
        reference = rule["$ref"]
        if not reference.startswith(prefix):
            raise ValueError("External capture schema reference")
        return matches(value, definitions[reference[len(prefix) :]], definitions)
    if "const" in rule and (type(value) is not type(rule["const"]) or value != rule["const"]):
        return False
    if "enum" in rule and not any(type(value) is type(x) and value == x for x in rule["enum"]):
        return False
    if "oneOf" in rule and sum(matches(value, r, definitions) for r in rule["oneOf"]) != 1:
        return False
    if "anyOf" in rule and not any(matches(value, r, definitions) for r in rule["anyOf"]):
        return False
    if "allOf" in rule and not all(matches(value, r, definitions) for r in rule["allOf"]):
        return False
    if "if" in rule:
        branch = "then" if matches(value, rule["if"], definitions) else "else"
        if branch in rule and not matches(value, rule[branch], definitions):
            return False
    types = {
        "object": dict,
        "array": list,
        "integer": int,
        "string": str,
        "boolean": bool,
        "null": type(None),
    }
    if "type" in rule and type(value) is not types[rule["type"]]:
        return False
    if isinstance(value, dict):
        if not set(rule.get("required", ())) <= set(value):
            return False
        props = rule.get("properties", {})
        if rule.get("additionalProperties") is False and not set(value) <= set(props):
            return False
        if any(
            key in value and not matches(value[key], r, definitions) for key, r in props.items()
        ):
            return False
    elif isinstance(value, list):
        if not rule.get("minItems", 0) <= len(value) <= rule.get("maxItems", 100000):
            return False
        if rule.get("uniqueItems") and len({canonical(x) for x in value}) != len(value):
            return False
        if "items" in rule and not all(matches(x, rule["items"], definitions) for x in value):
            return False
    elif isinstance(value, str):
        if len(value) > rule.get("maxLength", 524288):
            return False
        if "pattern" in rule and re.fullmatch(rule["pattern"], value) is None:
            return False
    elif type(value) is int:
        if not rule.get("minimum", -MAX_ID) <= value <= rule.get("maximum", MAX_ID):
            return False
    return True


def identity(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_nlink)


class Store:
    """Private immutable ledger rooted at an existing owned0700 directory."""

    def __init__(self, root, *, budget=None):
        self.budget = budget
        path = Path(root).absolute()
        self.fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:]:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
                os.close(self.fd)
                self.fd = nxt
            st = os.fstat(self.fd)
            if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
                raise Pending("Private capture ledger ownership/mode unavailable")
        except BaseException:
            os.close(self.fd)
            raise
        self.root_identity = (st.st_dev, st.st_ino)

    def close(self):
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    @contextlib.contextmanager
    def parent(self, parts, create=False):
        if not parts or any(
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", p) or p in {".", ".."} for p in parts
        ):
            raise ValueError("Invalid fixed ledger component")
        fd = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    os.fsync(fd)
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
                st = os.fstat(fd)
                if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
                    raise Pending("Unsafe capture ledger directory")
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def read(self, parts, limit=2097152, *, emergency=False):
        with self.parent(parts) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                before = os.fstat(fd)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_size > limit
                ):
                    raise Pending("Unsafe capture ledger member")
                if self.budget is not None:
                    self.budget.charge(before.st_size, retained=True, emergency=emergency)
                chunks = []
                remaining = limit + 1
                while remaining:
                    block = os.read(fd, min(65536, remaining))
                    if not block:
                        break
                    chunks.append(block)
                    remaining -= len(block)
                raw = b"".join(chunks)
                after = os.fstat(fd)
                path_st = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (
                    len(raw) != before.st_size
                    or identity(before) != identity(after)
                    or identity(after) != identity(path_st)
                ):
                    raise Pending("Capture ledger member changed")
                return raw
            finally:
                os.close(fd)

    def put(self, parts, raw, *, emergency=False):
        if not isinstance(raw, bytes):
            raise TypeError("Ledger originals must be bytes")
        with self.parent(parts, create=True) as (parent, name):
            try:
                existing = self.read(parts, len(raw), emergency=emergency)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if existing != raw:
                    raise Pending("Immutable capture ledger collision")
                os.fsync(parent)
                return
            if self.budget is not None:
                self.budget.charge(len(raw), retained=True, emergency=emergency)
            temporary = ".pending-" + uuid.uuid4().hex
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            try:
                view = memoryview(raw)
                while view:
                    count = os.write(fd, view)
                    if count < 1:
                        raise OSError("Incomplete capture ledger write")
                    view = view[count:]
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(
                    temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False
                )
                os.fsync(parent)
            finally:
                os.unlink(temporary, dir_fd=parent)
                os.fsync(parent)
            if self.read(parts, len(raw), emergency=emergency) != raw:
                raise Pending("Capture ledger readback mismatch")

    @contextlib.contextmanager
    def lease(self, parts, seconds=2):
        # Private worker/source publication is POSIX only; no fake cross-host lock.
        import fcntl  # noqa: PLC0415

        with self.parent(parts, create=True) as (parent, name):
            fd = os.open(
                name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent
            )
            try:
                st = os.fstat(fd)
                if (
                    not stat.S_ISREG(st.st_mode)
                    or st.st_nlink != 1
                    or st.st_uid != os.getuid()
                    or stat.S_IMODE(st.st_mode) != 0o600
                ):
                    raise Pending("Unsafe capture publication lease")
                end = time.monotonic() + seconds
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= end:
                            raise Pending("Capture publication lease busy") from None
                        # Bounded acquisition only, never a model/CI/evidence wait.
                        time.sleep(min(0.01, max(0, end - time.monotonic())))
                if identity(st) != identity(os.stat(name, dir_fd=parent, follow_symlinks=False)):
                    raise Pending("Capture lease identity changed")
                yield
            finally:
                os.close(fd)
