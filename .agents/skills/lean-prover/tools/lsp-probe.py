#!/usr/bin/env python3
"""Direct JSON-RPC client for Lean's language server, for timing edit->verdict.

Why this exists: MCP tool invocations are not independently timestamped, so the
language-server quadrants of the edit-loop cost model cannot be measured through
them.  This client speaks LSP over stdio to ``lake serve`` and measures elapsed
time *inside the client*, around the protocol exchange only.

================================================================
FINALITY: how this client knows a version's diagnostics are done
================================================================

Lean publishes diagnostics PROGRESSIVELY.  Timing a partial result would make
every downstream number wrong, so the finality rule is not guessed -- it is
taken from the server's own source (``Lean/Server/FileWorker.lean``,
``FileWorker/RequestHandling.lean``, ``Data/Lsp/Ipc.lean``, toolchain
``v4.32.1``).

ADOPTED RULE -- ``textDocument/waitForDiagnostics``
    Immediately after ``didOpen``/``didChange`` at version ``V`` we send the
    request ``textDocument/waitForDiagnostics {uri, version: V}``.  Its handler
    (``handleWaitForDiagnostics``) first waits until ``V <= doc.meta.version``,
    then returns a task that waits on BOTH ``doc.reporter`` (the task that
    publishes every diagnostic for the version) AND ``doc.cmdSnaps.waitAll``
    (every command snapshot elaborated).  The worker has a single output
    channel, so the response is serialized after everything the reporter
    enqueued.  Elapsed time is measured to the arrival of that response.
    This is the same primitive Lean's own LSP test runner uses
    (``Lean.Lsp.Ipc.collectDiagnostics``).

REJECTED RULE -- ``$/lean/fileProgress`` with ``processing == []``
    ``reportSnapshots`` sends the progress-done notification BEFORE the line

        unless st.hasBlocked do  -- "Debouncing 4."
          publishDiagnostics ctx doc

    so whenever the reporter never blocked -- which is the normal case for a
    file that finishes inside ``server.reportDelayMs`` -- the FINAL diagnostics
    publication happens strictly AFTER progress-done.  A client that stops at
    progress-done reads a stale or entirely absent diagnostic set.  This client
    records the progress-done timestamp anyway, as corroboration and so the
    ordering can be exhibited.

REJECTED RULE -- "the first ``publishDiagnostics`` for version V"
    ``reportSnapshots`` publishes at least: once when it first blocks
    ("Debouncing 2."), then again on every snapshot that adds diagnostics.  The
    first publication for a slow file is a prefix of the answer.  Every step
    record therefore carries ``first_publish_*`` beside the final values so the
    naive rule can be scored rather than argued about.

DISCLOSED CONSTANTS
    * ``server.reportDelayMs`` defaults to **200 ms** and is a command-line-only
      option.  The reporter sleeps for it before reporting anything, so every
      per-iteration LSP measurement carries a ~0.2 s floor.  It is left at the
      default because that is what a real editor client pays; ``--report-delay-ms``
      can override it for a decomposition experiment.
    * This client deliberately does NOT declare
      ``capabilities.lean.incrementalDiagnosticSupport``.  With it declared the
      server sends diagnostic DELTAS and a client reading one notification sees
      only the increment.  Undeclared, every ``publishDiagnostics`` is a full
      set, so "last publication wins" is correct.
    * ``silentDiagnosticSupport`` is likewise not declared, so silent messages
      are filtered server-side -- matching a plain client, not VS Code.

Usage (CLI):
  lsp-probe.py --workdir DIR --job JOB.json [--out RESULT.json] [--trace T.jsonl]
  lsp-probe.py --workdir DIR --open Blanc/X.lean [--change FILE]... [--out ...]

Exit codes: 0 ok, 2 usage error, 5 server/protocol failure or step timeout.
"""

import argparse
import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time


def _uri(path):
    return pathlib.Path(os.path.abspath(path)).as_uri()


class LspError(Exception):
    pass


class LeanLspClient:
    """One persistent ``lake serve`` process, driven synchronously."""

    def __init__(self, workdir, server_cmd=None, trace_path=None,
                 report_delay_ms=None, timeout=600.0):
        self.workdir = os.path.abspath(workdir)
        self.timeout = timeout
        self.report_delay_ms = report_delay_ms
        cmd = list(server_cmd) if server_cmd else ["lake", "serve"]
        if report_delay_ms is not None:
            # `lake serve` forwards everything after `--` to `lean --server`.
            cmd = cmd + ["--", "-Dserver.reportDelayMs=%d" % report_delay_ms]
        self.cmd = cmd
        self._trace = open(trace_path, "w", encoding="utf-8") if trace_path else None
        self._q = queue.Queue()
        self._stderr = []
        self._next_id = 1
        self._versions = {}
        self._open = set()

        self.t_spawn = time.perf_counter()
        self.proc = subprocess.Popen(
            cmd, cwd=self.workdir,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._errthread = threading.Thread(target=self._err_loop, daemon=True)
        self._errthread.start()

        self.warmup = {"cmd": cmd, "pid": self.proc.pid,
                       "report_delay_ms": report_delay_ms}
        self._handshake()

    # ---------------------------------------------------------------- io ---
    def _err_loop(self):
        for raw in iter(self.proc.stderr.readline, b""):
            self._stderr.append(raw.decode("utf-8", "replace").rstrip("\n"))

    def _read_loop(self):
        f = self.proc.stdout
        try:
            while True:
                length = None
                while True:
                    line = f.readline()
                    if not line:
                        self._q.put((time.perf_counter(), None))
                        return
                    line = line.decode("ascii", "replace").strip()
                    if line == "":
                        break
                    if line.lower().startswith("content-length:"):
                        length = int(line.split(":", 1)[1].strip())
                if length is None:
                    continue
                body = b""
                while len(body) < length:
                    chunk = f.read(length - len(body))
                    if not chunk:
                        self._q.put((time.perf_counter(), None))
                        return
                    body += chunk
                ts = time.perf_counter()
                try:
                    msg = json.loads(body.decode("utf-8"))
                except Exception as e:  # pragma: no cover
                    self._q.put((ts, {"__parse_error__": str(e)}))
                    continue
                self._q.put((ts, msg))
        except Exception as e:  # pragma: no cover
            self._q.put((time.perf_counter(), {"__reader_error__": str(e)}))

    def _send(self, obj):
        body = json.dumps(obj).encode("utf-8")
        head = ("Content-Length: %d\r\n\r\n" % len(body)).encode("ascii")
        if self._trace:
            self._trace.write(json.dumps(
                {"t": time.perf_counter() - self.t_spawn, "dir": "out",
                 "msg": obj}) + "\n")
        self.proc.stdin.write(head + body)
        self.proc.stdin.flush()

    def _request(self, method, params):
        rid = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                    "params": params})
        return rid

    def _notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _answer_server_request(self, msg):
        """Never leave a server->client request unanswered: the worker can
        block on it, which would look like an arbitrarily slow elaboration."""
        method = msg.get("method")
        if method == "workspace/configuration":
            items = (msg.get("params") or {}).get("items") or []
            result = [{} for _ in items]
        else:
            result = None
        self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})

    # --------------------------------------------------------- handshake ---
    def _handshake(self):
        rid = self._request("initialize", {
            "processId": os.getpid(),
            "rootUri": _uri(self.workdir),
            "rootPath": self.workdir,
            "clientInfo": {"name": "lsp-probe", "version": "1"},
            "capabilities": {
                "textDocument": {
                    "synchronization": {"dynamicRegistration": False},
                    "publishDiagnostics": {"relatedInformation": True},
                },
                "workspace": {"configuration": False,
                              "workspaceFolders": False},
                # deliberately NOT declaring lean.incrementalDiagnosticSupport
                # or lean.silentDiagnosticSupport -- see module docstring.
            },
            "initializationOptions": {"editDelay": 0},
            "trace": "off",
        })
        res = self._await_response(rid, self.timeout)
        self.server_capabilities = (res or {}).get("capabilities", {})
        self._notify("initialized", {})
        self.warmup["spawn_to_initialized_s"] = (
            time.perf_counter() - self.t_spawn)

    # ------------------------------------------------------------- pump ----
    def _pump(self, deadline, on_message):
        """Drain messages until ``on_message`` returns a non-None value."""
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return None
            try:
                ts, msg = self._q.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise LspError(
                        "server exited rc=%s; stderr tail: %s"
                        % (self.proc.returncode, " | ".join(self._stderr[-5:])))
                continue
            if msg is None:
                raise LspError(
                    "server closed stdout; stderr tail: %s"
                    % " | ".join(self._stderr[-5:]))
            if self._trace:
                self._trace.write(json.dumps(
                    {"t": ts - self.t_spawn, "dir": "in", "msg": msg}) + "\n")
            if "id" in msg and "method" in msg:
                self._answer_server_request(msg)
                continue
            out = on_message(ts, msg)
            if out is not None:
                return out

    def _await_response(self, rid, timeout):
        deadline = time.perf_counter() + timeout

        def h(ts, msg):
            if msg.get("id") == rid and "method" not in msg:
                if "error" in msg:
                    raise LspError("request %d failed: %r" % (rid, msg["error"]))
                return (ts, msg.get("result"))
            return None

        got = self._pump(deadline, h)
        if got is None:
            raise LspError("timeout waiting for response to request %d" % rid)
        return got[1]

    # ------------------------------------------------------- doc actions ---
    def _sync(self, uri, version, t0, grace=0.0):
        """Wait for version ``version`` of ``uri`` to be final.  Returns a
        record; see module docstring for the rule."""
        rid = self._request("textDocument/waitForDiagnostics",
                            {"uri": uri, "version": version})
        deadline = time.perf_counter() + self.timeout
        st = {
            "publishes": [],          # (ts, n_diags, n_errors) for this version
            "other_version_publishes": 0,
            "progress_done_ts": None,
            "fatal": False,
            "done_ts": None,
        }

        def h(ts, msg):
            m = msg.get("method")
            if m == "textDocument/publishDiagnostics":
                p = msg.get("params") or {}
                if p.get("uri") != uri:
                    return None
                v = p.get("version")
                if v is not None and v != version:
                    st["other_version_publishes"] += 1
                    return None
                diags = p.get("diagnostics") or []
                errs = [d for d in diags if d.get("severity") == 1]
                st["publishes"].append({
                    "t": ts, "n": len(diags), "n_err": len(errs),
                    "is_incremental": p.get("isIncremental"),
                    "first_error": (errs[0].get("message", "")[:200]
                                    if errs else None),
                    "diags": diags,
                })
                return None
            if m == "$/lean/fileProgress":
                p = msg.get("params") or {}
                td = p.get("textDocument") or {}
                if td.get("uri") != uri:
                    return None
                v = td.get("version")
                if v is not None and v != version:
                    return None
                proc = p.get("processing") or []
                # LeanFileProgressKind ToJson: processing => 1, fatalError => 2.
                if any(x.get("kind") == 2 for x in proc):
                    st["fatal"] = True
                if not proc:
                    if st["progress_done_ts"] is None:
                        st["progress_done_ts"] = ts
                return None
            if msg.get("id") == rid and "method" not in msg:
                if "error" in msg:
                    raise LspError("waitForDiagnostics failed: %r" % msg["error"])
                st["done_ts"] = ts
                return ts
            return None

        got = self._pump(deadline, h)
        timed_out = got is None
        t_final = st["done_ts"] if st["done_ts"] is not None else time.perf_counter()

        # Optional grace drain: validates the rule by checking whether any
        # further publication for this version arrives AFTER we declared final.
        grace_extra = 0
        grace_changed = False
        if grace > 0 and not timed_out:
            before = st["publishes"][-1]["diags"] if st["publishes"] else []
            gdead = time.perf_counter() + grace
            n0 = len(st["publishes"])

            def hg(ts, msg):
                h(ts, msg)
                return None

            self._pump(gdead, hg)
            grace_extra = len(st["publishes"]) - n0
            if grace_extra:
                after = st["publishes"][-1]["diags"]
                grace_changed = (json.dumps(after, sort_keys=True)
                                 != json.dumps(before, sort_keys=True))

        pubs = st["publishes"]
        last = pubs[-1] if pubs else None
        first = pubs[0] if pubs else None
        rec = {
            "uri": uri,
            "version": version,
            "elapsed_s": t_final - t0,
            "final_rule": "textDocument/waitForDiagnostics",
            "timed_out": timed_out,
            "fatal_progress": st["fatal"],
            "n_publishes": len(pubs),
            "n_diagnostics": last["n"] if last else 0,
            "n_errors": last["n_err"] if last else 0,
            "green": (last is None) or last["n_err"] == 0,
            "first_error": last["first_error"] if last else None,
            "diagnostics": last["diags"] if last else [],
            # --- naive-rule comparison, recorded on every single step ---
            "first_publish_elapsed_s": (first["t"] - t0) if first else None,
            "first_publish_n_diagnostics": first["n"] if first else None,
            "first_publish_n_errors": first["n_err"] if first else None,
            "first_publish_green": (first["n_err"] == 0) if first else None,
            "naive_first_publish_differs": bool(
                first is not None and last is not None
                and (first["n"] != last["n"] or first["n_err"] != last["n_err"])),
            # --- fileProgress-done comparison ---
            "progress_done_elapsed_s": (
                (st["progress_done_ts"] - t0)
                if st["progress_done_ts"] is not None else None),
            "publishes_after_progress_done": (
                sum(1 for p in pubs
                    if st["progress_done_ts"] is not None
                    and p["t"] > st["progress_done_ts"])
                if st["progress_done_ts"] is not None else None),
            "other_version_publishes": st["other_version_publishes"],
            "grace_extra_publishes": grace_extra,
            "grace_changed_answer": grace_changed,
            "publish_offsets_s": [round(p["t"] - t0, 6) for p in pubs],
            "publish_counts": [[p["n"], p["n_err"]] for p in pubs],
        }
        return rec

    def did_open(self, rel_path, text=None, grace=0.0):
        path = os.path.join(self.workdir, rel_path)
        uri = _uri(path)
        if text is None:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        version = 1
        self._versions[uri] = version
        self._open.add(uri)
        t0 = time.perf_counter()
        self._notify("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": "lean4",
                             "version": version, "text": text}})
        rec = self._sync(uri, version, t0, grace=grace)
        rec["op"] = "didOpen"
        rec["path"] = rel_path
        return rec

    def did_change(self, rel_path, text, grace=0.0):
        uri = _uri(os.path.join(self.workdir, rel_path))
        if uri not in self._open:
            raise LspError("didChange on a document that is not open: %s" % rel_path)
        version = self._versions[uri] + 1
        self._versions[uri] = version
        t0 = time.perf_counter()
        self._notify("textDocument/didChange", {
            "textDocument": {"uri": uri, "version": version},
            "contentChanges": [{"text": text}]})
        rec = self._sync(uri, version, t0, grace=grace)
        rec["op"] = "didChange"
        rec["path"] = rel_path
        return rec

    def did_close(self, rel_path):
        uri = _uri(os.path.join(self.workdir, rel_path))
        self._notify("textDocument/didClose", {"textDocument": {"uri": uri}})
        self._open.discard(uri)
        self._versions.pop(uri, None)
        return {"op": "didClose", "path": rel_path}

    def shutdown(self):
        try:
            rid = self._request("shutdown", None)
            self._await_response(rid, 30.0)
            self._notify("exit", None)
        except Exception:
            pass
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=20)
        except Exception:
            self.proc.kill()
            self.proc.wait(timeout=20)
        if self._trace:
            self._trace.close()
        return {"returncode": self.proc.returncode,
                "stderr_tail": self._stderr[-20:]}


# ------------------------------------------------------------------ CLI ---

def run_job(workdir, job, trace_path=None, grace=0.0):
    cli = LeanLspClient(workdir,
                        server_cmd=job.get("server_cmd"),
                        trace_path=trace_path,
                        report_delay_ms=job.get("report_delay_ms"),
                        timeout=job.get("timeout", 600.0))
    out = {"workdir": workdir, "steps": []}
    try:
        for step in job["steps"]:
            op = step["op"]
            text = None
            if step.get("text_file"):
                with open(step["text_file"], "r", encoding="utf-8") as f:
                    text = f.read()
            g = step.get("grace", grace)
            if op == "open":
                rec = cli.did_open(step["path"], text=text, grace=g)
                if "spawn_to_initialized_s" in cli.warmup and \
                        "spawn_to_first_final_s" not in cli.warmup:
                    cli.warmup["spawn_to_first_final_s"] = (
                        time.perf_counter() - cli.t_spawn)
                    cli.warmup["first_open_elapsed_s"] = rec["elapsed_s"]
            elif op == "change":
                rec = cli.did_change(step["path"], text, grace=g)
            elif op == "close":
                rec = cli.did_close(step["path"])
            else:
                raise LspError("unknown op %r" % op)
            rec["label"] = step.get("label")
            out["steps"].append(rec)
    finally:
        out["warmup"] = cli.warmup
        out["shutdown"] = cli.shutdown()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--job")
    ap.add_argument("--open", dest="open_path")
    ap.add_argument("--change", action="append", default=[],
                    help="file whose contents become the next didChange text")
    ap.add_argument("--out")
    ap.add_argument("--trace")
    ap.add_argument("--grace", type=float, default=0.0,
                    help="seconds to keep draining AFTER the finality rule "
                         "fires, to validate the rule (0 = off)")
    ap.add_argument("--report-delay-ms", type=int, default=None)
    args = ap.parse_args()

    if args.job:
        with open(args.job, "r", encoding="utf-8") as f:
            job = json.load(f)
    elif args.open_path:
        job = {"steps": [{"op": "open", "path": args.open_path}]}
        for i, c in enumerate(args.change):
            job["steps"].append({"op": "change", "path": args.open_path,
                                 "text_file": c, "label": "change%d" % (i + 1)})
    else:
        print("lsp-probe: need --job or --open", file=sys.stderr)
        return 2
    if args.report_delay_ms is not None:
        job["report_delay_ms"] = args.report_delay_ms

    try:
        out = run_job(args.workdir, job, trace_path=args.trace, grace=args.grace)
    except LspError as e:
        print("lsp-probe: %s" % e, file=sys.stderr)
        return 5

    text = json.dumps(out, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
