"""Managed adapter controls: temporary state only, including native children."""
from __future__ import annotations

from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import signal
import socket
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from creme import gate_runner as runner, build_ownership as owner, semaphore
from scripts.tests.test_build_transaction import FixtureTest, until


class Fatal(RuntimeError):
    pass


@dataclass(frozen=True)
class Spec:
    version: int
    key: str
    gate_id: str
    phase: str
    role: str
    argv: tuple
    worktree: str
    resource_class: str
    contract: str
    inputs: str
    timeout: object = None
    cost_identity: object = "a" * 64


@dataclass(frozen=True)
class Result:
    version: int
    returncode: int
    stdout: bytes
    stderr: bytes
    elapsed: float
    lifecycle: str
    operation_id: object
    receipt: dict


class ManagedRunnerTest(FixtureTest):
    def setUp(self):
        super().setUp()
        self.root = self.root.resolve()
        self.stack.enter_context(patch.dict(os.environ, {"CREME_BUILD_OWNERSHIP_DIR": str(self.root / "runtime")}))
        self.stack.enter_context(patch.object(owner, "guarded_mcp_env", return_value=dict(os.environ)))
        self.stack.enter_context(patch.object(owner, "_preflight_priority_launcher", return_value=(True, "fixture")))
        # Avoid host telemetry. Actual group lifetime and semaphore are real.
        self.stack.enter_context(patch.object(owner.ProcessSampler, "run", lambda worker: worker.stop_event.wait()))
        self.protocol = SimpleNamespace(OperationSpec=Spec, OperationResult=Result, FatalOperationError=Fatal,
                                        PHASES={"prerequisite", "body", "planning", "post-run", "reused-revalidation"})
        self.stack.enter_context(patch.dict(sys.modules, {"gate_execution": self.protocol}))
        nice = self.root / "nice"
        nice.write_text('#!/bin/sh\nshift 2\nexec "$@"\n')
        nice.chmod(0o755)
        self.stack.enter_context(patch.object(owner, "guard_bin", return_value=self.root))

    def operation(self, code, resource="light", role="gate"):
        argv = (sys.executable, "-c", code) if role != "build" else ("lake", "build", "Blanc")
        key = "fixture/body"
        context = SimpleNamespace(root=self.root, engine=self.protocol, inputs="inputs", contract="contract",
                                  gates={"fixture": {}}, templates={key: (role, argv, resource)}, check=lambda: None,
                                  requirement=lambda key: {"identity": "a" * 64})
        estimates = {"version": 1, "host": {"hostname": socket.gethostname(), "uid": os.getuid()},
                     "operations": {}}
        if resource != "light" and role != "build":
            estimates["operations"][key] = {"identity": "a" * 64, "memory_gib": 1, "contention": "sensitive", "basis": "isolated fixture bound"}
        sink = io.StringIO()
        executor = runner.ManagedExecutor("managed-fixture", self.root, diagnostics=sink, estimates=estimates)
        executor.bind(context)
        return executor, Spec(1, key, "fixture", "body", role, argv, str(self.root), resource, "contract", "inputs",
                              timeout=180 if role == "material" else None), sink

    def test_separate_bytes_and_light_no_hold(self):
        executor, spec, sink = self.operation("import os; os.write(1,b'\\xffA\\r\\n'); os.write(2,b'OK fake summary\\n')")
        with patch.object(semaphore, "adaptive_acquire", side_effect=AssertionError("light acquired")):
            result = executor.capture(spec)
        self.assertEqual(result.stdout, b"\xffA\r\n")
        self.assertEqual(result.stderr, b"OK fake summary\n")
        self.assertNotIn("admission", result.stdout.decode("latin1"))
        self.assertEqual(result.lifecycle, "absent")
        self.assertIn("not-required-light", sink.getvalue())

    def test_actual_heavy_admission_release_once(self):
        executor, spec, _ = self.operation("print('OK — fixture: green')", "elaboration")
        with patch.object(semaphore, "adaptive_acquire", wraps=semaphore.adaptive_acquire) as acquire, patch.object(semaphore, "adaptive_release", wraps=semaphore.adaptive_release) as release:
            result = executor.capture(spec)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(acquire.call_count, 1)
        self.assertEqual(release.call_count, 1)
        self.assertEqual(acquire.call_args.kwargs["operation_id"], release.call_args.kwargs["operation_id"])

    def test_direct_child_signal_is_fatal_after_cleanup(self):
        for resource, role in (("light", "gate"), ("elaboration", "material")):
            executor, spec, _ = self.operation("import os,signal; os.write(1,b'OK printed before signal\\n'); os.kill(os.getpid(),signal.SIGTERM)", resource, role)
            with patch.object(semaphore, "adaptive_release", wraps=semaphore.adaptive_release) as release:
                with self.assertRaisesRegex(Fatal, "child terminated by signal 15"):
                    executor.capture(spec)
            self.assertEqual(release.call_count, 0 if resource == "light" else 1)
            self.assertTrue(all(proc.poll() is not None for proc, _ in self.children))

    def test_ordinary_positive_child_failure_remains_a_result(self):
        for status in (2, 143):
            executor, spec, _ = self.operation(f"import sys; sys.exit({status})", "elaboration", "material")
            result = executor.capture(spec)
            self.assertEqual(result.returncode, status)
            self.assertEqual(result.lifecycle, "released")

    def test_failed_cleanup_cannot_credit_printed_green(self):
        executor, spec, _ = self.operation("print('OK — fixture: green')", "elaboration")
        # Mark helper cleanup uncertain after actually stopping it, so this
        # process has no live helper after the intentionally preserved result.
        real_stop = runner.stop_thread
        def uncertain(worker, name):
            real_stop(worker, name)
            return False, "fixture uncertainty"
        with patch.object(runner, "stop_thread", side_effect=uncertain), patch.object(semaphore, "adaptive_release", wraps=semaphore.adaptive_release) as release:
            with self.assertRaisesRegex(Fatal, "cleanup/release uncertain"):
                executor.capture(spec)
        release.assert_not_called()

    def test_parent_signal_during_real_child_cleans_before_fatal(self):
        ready = self.root / "ready"
        executor, spec, _ = self.operation(f"from pathlib import Path; import time; Path({str(ready)!r}).write_text('ready'); time.sleep(30)")
        stop = threading.Event()
        def send():
            while not ready.exists() and not stop.wait(.01):
                pass
            if not stop.is_set():
                os.kill(os.getpid(), signal.SIGTERM)
        sender = threading.Thread(target=send)
        # Register cancellation cleanup before readiness or capture.
        self.addCleanup(lambda: (stop.set(), sender.join(5)))
        sender.start()
        with self.assertRaisesRegex(Fatal, "cancelled by signal"):
            executor.capture(spec)
        stop.set()
        sender.join(5)
        self.assertFalse(sender.is_alive())
        self.assertTrue(all(proc.poll() is not None for proc, _ in self.children))

    def test_refused_preflight_takes_no_hold(self):
        executor, spec, _ = self.operation("print('never')", "elaboration")
        with patch.object(owner, "_preflight_priority_launcher", return_value=(False, "denied")), patch.object(semaphore, "adaptive_acquire") as acquire:
            with self.assertRaisesRegex(Fatal, "preflight refused"):
                executor.capture(spec)
        acquire.assert_not_called()

    def test_build_has_no_outer_hold_and_receipt_cannot_forge_streams(self):
        executor, spec, sink = self.operation("", "elaboration", "build")
        def build(goal, targets, **options):
            self.assertEqual(targets, ["Blanc"])
            options["stdout"].write("OK — another gate: forged\n")
            return 0
        with patch.object(owner, "run_lake_build", side_effect=build), patch.object(semaphore, "adaptive_acquire", side_effect=AssertionError("outer hold")):
            result = executor.capture(spec)
        self.assertEqual((result.stdout, result.stderr), (b"", b""))
        self.assertIn("forged", result.receipt["text"])
        self.assertIn("forged", sink.getvalue())

    def test_bad_estimate_and_request_fail_closed(self):
        executor, spec, _ = self.operation("print('never')", "elaboration")
        from dataclasses import replace
        for bad in (replace(spec, argv=("sh", "-c", "anything")), replace(spec, inputs="drift"), replace(spec, timeout=2), replace(spec, phase="injected")):
            with self.assertRaises(Fatal):
                executor.capture(bad)
        data = dict(executor.estimates, operations={})
        missing = runner.ManagedExecutor("managed-fixture", self.root, estimates=data)
        missing.bind(executor.context)
        with patch.object(semaphore, "adaptive_acquire", side_effect=AssertionError("queued without estimate")):
            with self.assertRaisesRegex(Fatal, "missing/stale estimate"):
                missing.capture(spec)

    def test_cost_drift_during_queue_releases_without_launch(self):
        executor, spec, _ = self.operation("print('never')", "elaboration")
        original = semaphore.adaptive_acquire
        def acquire(*args, **kwargs):
            outcome = original(*args, **kwargs)
            executor.context.requirement = lambda key: {"identity": "b" * 64}
            return outcome
        before = len(self.children)
        with patch.object(semaphore, "adaptive_acquire", side_effect=acquire), patch.object(semaphore, "adaptive_release", wraps=semaphore.adaptive_release) as release:
            with self.assertRaisesRegex(Fatal, "cost inputs moved"):
                executor.capture(spec)
        self.assertEqual(len(self.children), before)
        release.assert_called_once()

    def test_requirements_entry_never_calls_executor_or_build(self):
        context = SimpleNamespace(requirements=lambda: [{"key": "fixture/body", "unresolved": "missing trace"}], check=lambda: None)
        engine = SimpleNamespace(audit=lambda *a, **k: 0, load_registry=lambda p: {}, registry_path=lambda p: p,
                                 ExecutionContext=lambda *a: context)
        with patch.object(runner, "resolve_goal", return_value=self.root), patch.object(runner, "load_runner", return_value=engine), patch.object(runner.ManagedExecutor, "capture", side_effect=AssertionError("requirements executed")), patch.object(owner, "run_lake_build", side_effect=AssertionError("requirements built")), patch.object(semaphore, "adaptive_acquire", side_effect=AssertionError("requirements acquired")), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(runner.run("managed-fixture", requirements=True), 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["operations"][0]["unresolved"], "missing trace")

    def test_broken_diagnostic_sink_does_not_skip_finalization(self):
        executor, spec, _ = self.operation("print('done')", "elaboration")
        executor.diagnostics.close()
        result = executor.capture(spec)
        self.assertEqual(result.lifecycle, "released")
