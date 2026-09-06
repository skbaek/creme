"""Optional Blanc catalogue adapter. One accepted owned lifetime per operation.

No arbitrary command/PID/module CLI surface and no direct fallback. Repository
requests are compared with the frozen v1 recipe templates. Build ownership stays
with run_lake_build; its legacy combined text is a receipt, never child stdout.
"""
from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Optional, TextIO

from . import build_ownership as owner
from . import semaphore
from .adapters import get_adapter
from .build_lifecycle import BuildTransaction, OwnedThread, TerminationSignals, communicate, stop_thread
from .profile import DEFAULT_RELATIVE_PROFILE, load as load_profile

API_VERSION = 1
ESTIMATES_RELATIVE = Path(".creme/managed-gate-estimates.json")


class GateRunnerError(RuntimeError):
    pass


def resolve_goal(goal: str) -> Path:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", goal) is None or goal in {".", ".."}:
        raise GateRunnerError("invalid gate goal")
    checked = load_profile(semaphore.canonical_creme_root() / DEFAULT_RELATIVE_PROFILE, get_adapter())
    if checked.profile is None or checked.status not in {"VALID", "LIMITED"}:
        raise GateRunnerError("managed gates require a current validated host profile")
    workspace = checked.profile["workspace"]
    base = Path(workspace["root"]).expanduser().resolve()
    repository = base / workspace["blanc"]
    root = repository / ".worktrees" / goal
    if repository.resolve().parent != base or root.is_symlink() or not (root / ".git").is_file():
        raise GateRunnerError("configured Blanc goal worktree is absent or aliased")
    root = root.resolve()
    resolved, identity = owner._worktree_identity(root, goal)
    if resolved != root or identity != goal:
        raise GateRunnerError("managed gate root does not match build ownership")
    return root


@contextmanager
def in_directory(root: Path):
    prior = Path.cwd()
    os.chdir(root)
    try:
        yield
    finally:
        os.chdir(prior)


def load_runner(root: Path) -> Any:
    scripts = root / "scripts"
    for name in ("gate-cache.py", "gate_execution.py"):
        path = scripts / name
        if path.is_symlink() or not path.is_file():
            raise GateRunnerError(f"managed support missing or aliased: {name}")
    # This fixed repository import is trusted application code, not an injected
    # environment-module name. Refuse a cached protocol from another worktree.
    cached = sys.modules.get("gate_execution")
    if cached is not None and Path(cached.__file__).resolve() != scripts / "gate_execution.py":
        raise GateRunnerError("another worktree's managed protocol is already loaded")
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location("blanc_managed_gate_cache", scripts / "gate-cache.py")
        if spec is None or spec.loader is None:
            raise GateRunnerError("managed runner cannot be loaded")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        if getattr(module, "MANAGED_API_VERSION", None) != API_VERSION:
            raise GateRunnerError("unsupported managed runner API")
        return module
    except (ImportError, OSError, SyntaxError) as exc:
        raise GateRunnerError(f"managed support unavailable: {exc}") from exc
    finally:
        sys.path.pop(0)


class Renewal(OwnedThread):
    """Only record refusal; the transaction finalizer alone signals the group."""
    def __init__(self, goal: str, operation_id: str):
        super().__init__(daemon=True)
        self.goal, self.operation_id = goal, operation_id
        self.stop_event = threading.Event()
        self.refused: Optional[str] = None

    def run(self) -> None:
        while not self.stop_event.wait(owner.RENEW_INTERVAL_SECONDS):
            try:
                ok, detail = semaphore.renew(self.goal, semaphore.ADAPTIVE_LEASE_SECONDS,
                                              operation_id=self.operation_id)
                if ok:
                    continue
                self.refused = detail
            except Exception as exc:
                self.refused = f"renewal raised {type(exc).__name__}"
            return

    def stop(self) -> None:
        self.stop_event.set()
        self.join(timeout=2)


class ManagedExecutor:
    version = API_VERSION
    managed = True

    def __init__(self, goal: str, root: Path, *, wait_seconds: Optional[int] = None,
                 diagnostics: Optional[TextIO] = None, estimates: Optional[dict[str, Any]] = None):
        self.goal, self.root, self.wait_seconds = goal, root.resolve(), wait_seconds
        self.diagnostics = diagnostics if diagnostics is not None else sys.stderr
        self.estimates = estimates
        self.context = None

    def diagnostic(self, text: str) -> None:
        try:
            self.diagnostics.write(text + ("" if text.endswith("\n") else "\n"))
            self.diagnostics.flush()
        except (OSError, ValueError):
            # The private lifecycle journal remains authoritative when the
            # display sink is broken; this must not unwind resource teardown.
            pass

    def bind(self, context: Any) -> None:
        if self.context is not None or context.root != self.root:
            raise GateRunnerError("managed context cannot be rebound or moved")
        if self.estimates is None:
            path = semaphore.canonical_creme_root() / ESTIMATES_RELATIVE
            try:
                self.estimates = json.loads(path.read_text())
            except FileNotFoundError:
                self.estimates = {"version": API_VERSION,
                                  "host": {"hostname": socket.gethostname(), "uid": os.getuid()}, "operations": {}}
            except (OSError, ValueError) as exc:
                raise context.engine.FatalOperationError(f"managed gate estimates unreadable: {exc}") from exc
        data = self.estimates
        if (not isinstance(data, dict) or set(data) != {"version", "host", "operations"} or
                data["version"] != API_VERSION or data["host"] != {"hostname": socket.gethostname(), "uid": os.getuid()} or
                not isinstance(data["operations"], dict)):
            raise context.engine.FatalOperationError("managed estimate host/schema mismatch")
        for key, value in data["operations"].items():
            if (not isinstance(key, str) or not isinstance(value, dict) or
                    set(value) != {"identity", "memory_gib", "contention", "basis"} or
                    not isinstance(value["identity"], str) or re.fullmatch(r"[0-9a-f]{64}", value["identity"]) is None or
                    type(value["memory_gib"]) is not int or value["memory_gib"] < 1 or
                    value["contention"] not in {"sensitive", "exclusive"} or
                    not isinstance(value["basis"], str) or not value["basis"].strip()):
                raise context.engine.FatalOperationError(f"invalid or unjustified managed estimate: {key}")
        self.context = context

    def capture(self, spec: Any) -> Any:
        context = self.context
        if context is None:
            raise GateRunnerError("managed executor is not bound")
        protocol = sys.modules["gate_execution"]
        fatal = protocol.FatalOperationError
        if (not isinstance(spec, protocol.OperationSpec) or spec.version != API_VERSION or
                spec.worktree != str(self.root) or spec.contract != context.contract or spec.inputs != context.inputs or
                spec.gate_id not in context.gates or not spec.key.startswith(spec.gate_id + "/") or
                context.templates.get(spec.key) != (spec.role, spec.argv, spec.resource_class) or
                spec.phase not in protocol.PHASES or spec.timeout != (180 if spec.role == "material" else None)):
            raise fatal("request differs from its registered operation")
        context.check()
        started = time.monotonic()
        if spec.role == "build":
            receipt = io.StringIO()
            try:
                with in_directory(self.root):
                    status = owner.run_lake_build(self.goal, list(spec.argv[2:]),
                                                  wait_seconds=self.wait_seconds, stdout=receipt)
            finally:
                self.diagnostic(receipt.getvalue())
                receipt_id = uuid.uuid4().hex
                record = {"kind": "owned-build-receipt", "text": receipt.getvalue(), "key": spec.key}
                receipt_path = self.root / ".lake/managed-gate-receipts" / (receipt_id + ".json")
                receipt_path.parent.mkdir(parents=True, exist_ok=True)
                # Receipt writes happen after the owned API has finalized. A
                # failed write stops the run rather than losing diagnostics.
                receipt_path.write_text(json.dumps(record, sort_keys=True) + "\n")
                self.diagnostic(f"build receipt: {receipt_path}")
            if status:
                raise fatal(f"owned build prerequisite failed ({status}); see build receipt")
            return protocol.OperationResult(API_VERSION, status, b"", b"", time.monotonic() - started,
                                            "released", None, {**record, "path": str(receipt_path)})

        heavy = spec.resource_class != "light"
        if heavy:
            requirement = context.requirement(spec.key)
            estimate = self.estimates["operations"].get(spec.key)
            required_class = "exclusive" if spec.resource_class == "exclusive" else "sensitive"
            if (not requirement.get("identity") or requirement["identity"] != spec.cost_identity or
                    estimate is None or estimate["identity"] != spec.cost_identity or estimate["contention"] != required_class):
                raise fatal(f"missing/stale estimate for {spec.key}; run gate-run {self.goal} --requirements before this operation can queue")
        env = owner.guarded_mcp_env()
        env["LEAN_NUM_THREADS"] = str(owner.DEFAULT_THREADS)
        executable = str((self.root / spec.argv[0]).resolve()) if "/" in spec.argv[0] else shutil.which(spec.argv[0], path=env["PATH"])
        if not executable or not os.access(executable, os.X_OK):
            raise fatal("registered operation executable is unavailable")
        argv = [executable, *spec.argv[1:]]
        if heavy:
            launcher = owner.guard_bin() / "nice"
            ok, detail = owner._preflight_priority_launcher(launcher, cwd=self.root, env=env)
            if not ok:
                raise fatal(f"priority launch preflight refused before admission: {detail}")
            argv = [str(launcher), "-n", "10", *argv]
        transaction = None
        renewer = None
        sampler = None
        failure: Optional[BaseException] = None
        streams = (b"", b"")
        status = 1
        termination = TerminationSignals()
        with termination:
            transaction = BuildTransaction(self.goal)
            try:
                termination.check()
                if heavy:
                    estimate = self.estimates["operations"][spec.key]
                    ok, detail = semaphore.adaptive_acquire(
                        self.goal, f"gate {spec.key} ({spec.phase})", semaphore.ADAPTIVE_LEASE_SECONDS,
                        memory_gib=estimate["memory_gib"], contention=estimate["contention"],
                        wait_seconds=self.wait_seconds, estimate_source=estimate["basis"],
                        operation_id=transaction.id, cancel_check=termination.check,
                        announce=self.diagnostic,
                    )
                    if not ok:
                        raise fatal(f"managed admission refused: {detail}")
                termination.check()
                context.check()
                if heavy and context.requirement(spec.key).get("identity") != spec.cost_identity:
                    raise fatal("operation cost inputs moved while waiting for admission")
                proc = transaction.launch(argv, cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if heavy:
                    renewer = Renewal(self.goal, transaction.id)
                    transaction.helpers.append((renewer, "gate renewal"))
                    renewer.start()
                    sampler = owner.ProcessSampler(proc.pid, worktree=self.root)
                    transaction.helpers.append((sampler, "gate sampler"))
                    sampler.start()

                class Cancellation:
                    def check(self):
                        termination.check()
                        if renewer is not None and renewer.refused:
                            raise fatal(f"managed renewal refused: {renewer.refused}")

                streams = communicate(proc, Cancellation(), spec.timeout)
                status = proc.returncode
                termination.check()
            except BaseException as exc:
                failure = exc
            finally:
                transaction.finalize(terminate=owner._terminate_process_group,
                                     close=owner._close_process_pipes, stop=stop_thread, fresh=not heavy)
        if (not transaction.cleanup_proved or not transaction.release_result or not transaction.release_result[0]):
            raise fatal(f"managed cleanup/release uncertain: {transaction.errors}; {transaction.recovery}")
        if termination.signum is not None:
            raise fatal(f"managed operation cancelled by signal {termination.signum}")
        if renewer is not None and renewer.refused:
            raise fatal(f"managed renewal refused: {renewer.refused}")
        if failure is not None:
            raise fatal(f"managed operation failed: {type(failure).__name__}: {failure}") from failure
        receipt = {"kind": "owned-gate", "key": spec.key, "phase": spec.phase,
                   "admission": "released" if heavy else "not-required-light",
                   "cost_identity": spec.cost_identity,
                   "estimate": estimate if heavy else None,
                   "cost_envelope": requirement.get("envelope", {}) if heavy else {},
                   "peak_rss_mib": sampler.peak_rss_mib if sampler and sampler.samples else None,
                   "samples": sampler.samples if sampler else 0,
                   "unavailable_samples": sampler.unavailable_samples if sampler else 0}
        self.diagnostic(json.dumps(receipt, sort_keys=True))
        return protocol.OperationResult(API_VERSION, status, streams[0], streams[1], time.monotonic() - started,
                                        "released" if heavy else "absent", transaction.id, receipt)


def run(goal: str, *, plan: bool = False, explain: bool = False, fresh: bool = False,
        echo: bool = False, wait_seconds: Optional[int] = None, requirements: bool = False) -> int:
    try:
        if requirements and (plan or explain or fresh or echo or wait_seconds is not None):
            raise GateRunnerError("--requirements must be used without execution options")
        root = resolve_goal(goal)
        runner = load_runner(root)
        argv = ["plan" if plan or explain else "run"]
        if fresh:
            argv.append("--fresh")
        if explain:
            argv.append("--explain")
        if echo and argv[0] == "run":
            argv.append("--echo")
        executor = ManagedExecutor(goal, root, wait_seconds=wait_seconds)
        with in_directory(root):
            # Keep static identity helpers and the actual child environment on
            # the same canonical cache; no helper starts a build here.
            prior_cache = os.environ.get("LAKE_CACHE_DIR")
            os.environ["LAKE_CACHE_DIR"] = owner.lake_env()["LAKE_CACHE_DIR"]
            try:
                if requirements:
                    if runner.audit(root, quiet=True) != 0:
                        raise GateRunnerError("managed catalogue audit failed")
                    class Inspector:
                        version = API_VERSION
                        managed = True
                        def bind(self, context):
                            pass
                        def capture(self, spec):
                            raise AssertionError("requirements must never execute an operation")
                    context = runner.ExecutionContext(root, runner.load_registry(runner.registry_path(root)), Inspector(), runner)
                    rows = context.requirements()
                    context.check()
                    print(json.dumps({"version": API_VERSION, "host": {"hostname": socket.gethostname(), "uid": os.getuid()},
                                      "estimate_file": str(semaphore.canonical_creme_root() / ESTIMATES_RELATIVE),
                                      "operations": rows}, indent=2, sort_keys=True))
                    return 0
                return runner.main(argv, executor=executor)
            finally:
                if prior_cache is None:
                    os.environ.pop("LAKE_CACHE_DIR", None)
                else:
                    os.environ["LAKE_CACHE_DIR"] = prior_cache
    except (GateRunnerError, OSError, ValueError) as exc:
        print(f"gate-run: REFUSED: {exc}", file=sys.stderr)
        return 2
