
# Appended to the generated broker; names above come from its pinned prelude.
import uuid
import tempfile
import json

SYSTEMCTL = Path("/usr/bin/systemctl")
UNIT = "creme-contained-workflow.service"
WORKFLOW_OWNER_RE = re.compile(r"workflow-[0-9a-f]{32}")
RECOVERABLE_WORKFLOW_STATUSES = {"ADMITTING", "RUNNING", "RELEASE_FAILED"}
CERTIFICATE_CHECK = '''import runpy, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root / "scripts"))
module = runpy.run_path(str(root / "scripts/gate-cache.py"), run_name="creme_certificate_check")
current, reason, _ = module["build_certificate_status"](root)
print(("OK" if current else "REFUSED") + " — build certificate: " + reason)
raise SystemExit(0 if current else 2)
'''


def workflow_parse(arguments):
    if arguments == ["status"]:
        return None
    if len(arguments) not in {4, 6}:
        refuse("usage: codex-creme-contained-workflow status | PROFILE GOAL OPERATION MODE [--purpose PURPOSE]")
    profile, goal, operation, mode = arguments[:4]
    purpose = "goal"
    if len(arguments) == 6:
        if arguments[4] != "--purpose":
            refuse("only --purpose is accepted after MODE")
        purpose = arguments[5]
    if profile not in {"jaune", "blanc"} or GOAL_RE.fullmatch(goal) is None or purpose not in PURPOSE_SUFFIX:
        refuse("invalid profile, goal, or purpose")
    recipe = RECIPES["operations"].get(operation)
    if recipe is None or recipe["profile"] != profile or mode not in recipe["modes"]:
        refuse("unregistered operation, mode, or profile")
    return profile, goal, operation, mode, purpose


def workflow_control_plane():
    require_control_plane()
    regular_path(RECIPES_PATH, "workflow recipes")
    if hashlib.sha256(RECIPES_PATH.read_bytes()).hexdigest() != RECIPES_SHA256:
        refuse("workflow recipes changed; review and reinstall complete capability bundle")


def workflow_worktree(profile, goal, purpose):
    # Reuse the exact worktree identity check with reviewed host layout rather
    # than assuming all repositories share a parent directory.
    repository = Path(RECIPES["repositories"][profile])
    safe_component_path(repository, Path("/"))
    return require_worktree(profile, goal, purpose, repository)


def workflow_command(repo, operation, mode):
    recipe = RECIPES["operations"][operation]["modes"][mode]
    def expand(value):
        return value.replace("{repo}", str(repo)).replace("{creme}", str(CREME_ROOT))
    command = [expand(argument) for argument in recipe["argv"]]
    # Target venv interpreters may be links; their reviewed absolute path is
    # fixed in the pinned recipe. Repository script components may not be links.
    script = next(argument for argument in command[1:] if argument.startswith(str(repo / "scripts") + "/"))
    safe_component_path(Path(script), repo)
    regular_path(Path(script), "recipe script")
    environment = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"}
    environment.update({key: expand(value) for key, value in recipe["env"].items()})
    environment["LAKE_CACHE_DIR"] = str(CREME_ROOT / ".creme/lake-cache")
    return command, environment


def workflow_status():
    regular_path(SYSTEMCTL, "systemctl", executable=True)
    result = subprocess.run(
        [str(SYSTEMCTL), "--user", "show", UNIT, "--property=LoadState,ActiveState,SubState,Result,ExecMainStatus"],
        capture_output=True, text=True, check=False, timeout=15,
    )
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode and properties.get("LoadState") != "not-found":
        refuse("cannot inspect workflow service; execution state is unknown")
    record = BROKER_STATE / "workflow-last.json"
    last = None
    if record.exists():
        if record.is_symlink():
            refuse("workflow record is linked; execution state is unknown")
        try:
            last = json.loads(record.read_text())
        except (OSError, ValueError):
            refuse("workflow record is unreadable; execution state is unknown")
    telemetry = subprocess.run([str(CREME), "telemetry"], capture_output=True, text=True, check=False, timeout=30)
    semaphore = subprocess.run([str(CREME), "semaphore", "status"], capture_output=True, text=True, check=False, timeout=30)
    transient = subprocess.run([
        str(SYSTEMCTL), "--user", "list-units", "--type=service", "--state=running,activating",
        "--no-pager", "--no-legend", "run-*", "creme-contained-build*",
    ], capture_output=True, text=True, check=False, timeout=15)
    status = "OK" if telemetry.returncode == semaphore.returncode == transient.returncode == 0 else "UNAVAILABLE"
    if last is not None and not isinstance(last, dict):
        refuse("workflow record has invalid shape; execution state is unknown")
    last_status = last.get("status") if last else None
    if last and not isinstance(last_status, str):
        refuse("workflow record has invalid status; execution state is unknown")
    if last_status in {"ADMITTING", "RUNNING"} and properties.get("ActiveState") not in {"active", "activating"}:
        status = "UNKNOWN"
    if last_status == "RECOVERED_UNKNOWN":
        status = "UNKNOWN"
    if last and last.get("release_exit_code", 0) != 0:
        status = "REFUSED"
    print(json.dumps({"status": status,
                      "unit": UNIT, "service": result.stdout.strip(),
                      "telemetry": telemetry.stdout, "semaphore": semaphore.stdout,
                      "other_transient_services": transient.stdout,
                      "last_record": last,
                      "note": "A missing service with a nonterminal record is unknown, not completed."}))
    return 0 if status == "OK" else 2


def workflow_atomic_record(path, payload):
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        refuse(f"cannot inspect workflow record destination: {exc}")
    else:
        if path.is_symlink() or not path.is_file():
            refuse("workflow record destination is not a regular file")
    descriptor, temporary = tempfile.mkstemp(prefix=".workflow-", dir=BROKER_STATE)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(BROKER_STATE, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def workflow_record(payload):
    workflow_atomic_record(BROKER_STATE / "workflow-last.json", payload)
    print(json.dumps(payload), flush=True)


def require_workflow_unit():
    try:
        relative = next(
            line.split(":", 2)[2] for line in Path("/proc/self/cgroup").read_text().splitlines()
            if line.startswith("0:")
        )
    except (OSError, StopIteration):
        refuse("cgroup v2 membership is unavailable")
    if not relative.startswith("/") or relative.endswith("/") or relative.rsplit("/", 1)[-1] != UNIT:
        refuse(f"contained recovery is outside the exact workflow unit: {relative}")


def workflow_recovery_path(owner):
    if not isinstance(owner, str) or WORKFLOW_OWNER_RE.fullmatch(owner) is None:
        refuse("previous workflow owner is not an exact unique workflow label")
    return BROKER_STATE / f"workflow-recovery-{owner}.json"


def expected_previous_commands(previous):
    profile = previous.get("profile")
    goal = previous.get("goal")
    operation = previous.get("operation")
    mode = previous.get("mode")
    if (
        not isinstance(profile, str)
        or profile not in {"jaune", "blanc"}
        or not isinstance(goal, str)
        or GOAL_RE.fullmatch(goal) is None
        or goal in {".", ".."}
        or not isinstance(operation, str)
        or not isinstance(mode, str)
    ):
        refuse("previous workflow metadata has an invalid profile, goal, operation, or mode")
    recipe = RECIPES["operations"].get(operation)
    if recipe is None or recipe["profile"] != profile or mode not in recipe["modes"]:
        refuse("previous workflow metadata does not name a registered recipe")
    repository = Path(RECIPES["repositories"][profile])
    execution = recipe["modes"][mode]
    commands = []
    for suffix in PURPOSE_SUFFIX.values():
        repo = repository / ".worktrees" / f"{goal}{suffix}"
        commands.append([
            value.replace("{repo}", str(repo)).replace("{creme}", str(CREME_ROOT))
            for value in execution["argv"]
        ])
    return commands


def validate_interrupted_workflow(previous):
    if not isinstance(previous, dict):
        refuse("previous workflow is not a supported interrupted record")
    status = previous.get("status")
    if not isinstance(status, str) or status not in RECOVERABLE_WORKFLOW_STATUSES:
        refuse("previous workflow is not a supported interrupted record")
    if previous.get("unit") != UNIT:
        refuse("previous workflow record does not name the exact workflow unit")
    owner = previous.get("owner")
    workflow_recovery_path(owner)
    if previous.get("recipes_sha256") != RECIPES_SHA256:
        refuse("previous workflow record does not match the installed recipe set")
    argv = previous.get("argv")
    if not isinstance(argv, list) or argv not in expected_previous_commands(previous):
        refuse("previous workflow command does not match its recorded recipe metadata")
    if status == "RELEASE_FAILED":
        command_exit = previous.get("exit_code")
        release_exit = previous.get("release_exit_code")
        if (
            isinstance(command_exit, bool)
            or not isinstance(command_exit, int)
            or isinstance(release_exit, bool)
            or not isinstance(release_exit, int)
            or release_exit == 0
        ):
            refuse("previous release-failed workflow record has invalid exit metadata")
    return owner


def validate_recovered_workflow(previous):
    if not isinstance(previous, dict) or previous.get("status") != "RECOVERED_UNKNOWN":
        refuse("previous workflow recovery record has invalid shape")
    owner = previous.get("owner")
    path = workflow_recovery_path(owner)
    preserved = previous.get("previous_record")
    recovery = previous.get("recovery")
    if previous.get("unit") != UNIT or not isinstance(preserved, dict) or not isinstance(recovery, dict):
        refuse("previous workflow recovery record has invalid metadata")
    if preserved.get("owner") != owner or validate_interrupted_workflow(preserved) != owner:
        refuse("previous workflow recovery owner does not match its preserved record")
    outcome = recovery.get("outcome")
    exit_code = recovery.get("exit_code")
    stdout = recovery.get("stdout")
    if (
        set(recovery) != {"action", "outcome", "exit_code", "stdout"}
        or recovery.get("action") != "hard-release"
        or not isinstance(outcome, str)
        or isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
        or not isinstance(stdout, str)
        or (outcome, exit_code, stdout) not in {
            ("released", 0, "OK — hard hold released"),
            ("matching-hard-hold-not-found", 1, "REFUSED — matching hard hold not found"),
        }
    ):
        refuse("previous workflow recovery evidence is invalid")
    if path.is_symlink() or not path.is_file():
        refuse("durable workflow recovery record is missing or linked")
    try:
        durable = json.loads(path.read_text())
    except (OSError, ValueError):
        refuse("durable workflow recovery record is unreadable")
    if durable != previous:
        refuse("workflow recovery record does not match its durable evidence")


def recover_interrupted_workflow(previous):
    owner = validate_interrupted_workflow(previous)
    recovery_path = workflow_recovery_path(owner)
    if recovery_path.is_symlink():
        refuse("durable workflow recovery record is linked")
    if recovery_path.exists():
        if not recovery_path.is_file():
            refuse("durable workflow recovery record is not a regular file")
        try:
            recovered = json.loads(recovery_path.read_text())
        except (OSError, ValueError):
            refuse("durable workflow recovery record is unreadable")
        validate_recovered_workflow(recovered)
        if recovered.get("previous_record") != previous:
            refuse("durable workflow recovery record belongs to different workflow metadata")
        workflow_record(recovered)
        return
    try:
        release = subprocess.run(
            [str(CREME), "semaphore", "hard-release", owner],
            capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        refuse(f"cannot recover previous workflow hold: {exc}")
    stdout = (release.stdout or "").strip()
    stderr = (release.stderr or "").strip()
    if release.returncode == 0 and stdout == "OK — hard hold released" and not stderr:
        outcome = "released"
    elif release.returncode == 1 and stdout == "REFUSED — matching hard hold not found" and not stderr:
        outcome = "matching-hard-hold-not-found"
    else:
        refuse("previous workflow hard-release returned an unrecognized result; preserve it for recovery")
    recovered = {
        "status": "RECOVERED_UNKNOWN",
        "unit": UNIT,
        "owner": owner,
        "previous_record": previous,
        "recovery": {
            "action": "hard-release",
            "outcome": outcome,
            "exit_code": release.returncode,
            "stdout": stdout,
        },
    }
    workflow_atomic_record(recovery_path, recovered)
    workflow_record(recovered)


def reconcile_previous_workflow():
    record = BROKER_STATE / "workflow-last.json"
    if record.is_symlink():
        refuse("previous workflow record is linked; preserve it for recovery")
    if not record.exists():
        return
    try:
        previous = json.loads(record.read_text())
    except (OSError, ValueError):
        refuse("previous workflow record is unreadable; preserve it for recovery")
    if not isinstance(previous, dict):
        refuse("previous workflow record has invalid shape; preserve it for recovery")
    status = previous.get("status")
    if not isinstance(status, str):
        refuse("previous workflow record has invalid status; preserve it for recovery")
    if status in {"TERMINAL", "ADMISSION_REFUSED"} and previous.get("release_exit_code", 0) == 0:
        return
    if status == "RECOVERED_UNKNOWN":
        validate_recovered_workflow(previous)
        return
    if status in RECOVERABLE_WORKFLOW_STATUSES:
        recover_interrupted_workflow(previous)
        return
    refuse("previous workflow is unresolved; inspect status and preserve its recorded owner before retrying")


def workflow_service(arguments, parsed):
    profile, goal, operation, mode, purpose = parsed
    require_containment(profile)
    require_workflow_unit()
    # The service, not the disposable launcher, owns the global build/workflow
    # lock. Children inherit it so a lost supervisor cannot unlock live work.
    descriptor = broker_lock()
    os.set_inheritable(descriptor, True)
    label = "workflow-" + uuid.uuid4().hex
    try:
        workflow_control_plane()
        reconcile_previous_workflow()
        if (CREME_ROOT / ".creme/lean-heavy-suspended").exists():
            refuse("host circuit breaker is active")
        repo = workflow_worktree(profile, goal, purpose)
        command, environment = workflow_command(repo, operation, mode)
        check = subprocess.run([str(PREFLIGHT)], check=False)
        if check.returncode:
            return check.returncode
        if RECIPES["operations"][operation].get("guard") == "blanc-build-certificate":
            script = repo / "scripts/gate-cache.py"
            safe_component_path(script, repo)
            regular_path(script, "certificate guard")
            certificate = subprocess.run([
                "/usr/bin/python3", "-I", "-c", CERTIFICATE_CHECK, str(repo),
            ], cwd=repo, env=environment, check=False, pass_fds=(descriptor,))
            if certificate.returncode:
                return certificate.returncode
        metadata = {"unit": UNIT, "owner": label, "profile": profile, "goal": goal,
                    "operation": operation, "mode": mode, "argv": command,
                    "recipes_sha256": RECIPES_SHA256}
        # Persist the prospective owner before acquisition. If admission succeeds
        # but this supervisor dies before observing it, recovery still has its ID.
        workflow_record({**metadata, "status": "ADMITTING"})
        admission = subprocess.run([
            str(CREME), "semaphore", "adaptive-acquire", label,
            "--note", f"workflow {profile}/{goal} {operation}/{mode}",
            "--memory-gib", str(RECIPES["operations"][operation]["memory_gib"]),
            "--contention", "exclusive", "--lease", "7200",
        ], check=False)
        if admission.returncode:
            workflow_record({**metadata, "status": "ADMISSION_REFUSED", "exit_code": admission.returncode})
            return admission.returncode
        workflow_record({**metadata, "status": "RUNNING"})
        # SIGTERM's default exit leaves the hold intact; systemd kills the whole
        # control group. No finally-release may certify interrupted work idle.
        result = subprocess.run(command, cwd=repo, env=environment, check=False, pass_fds=(descriptor,))
        release = subprocess.run([str(CREME), "semaphore", "hard-release", label], check=False)
        workflow_record({"status": "TERMINAL" if release.returncode == 0 else "RELEASE_FAILED", "unit": UNIT, "owner": label,
                         "profile": profile, "goal": goal, "operation": operation, "mode": mode,
                         "argv": command, "recipes_sha256": RECIPES_SHA256,
                         "exit_code": result.returncode, "release_exit_code": release.returncode})
        return result.returncode or release.returncode
    finally:
        # In particular, exceptions after admission preserve the hold for
        # ownership-aware recovery. Closing our fd cannot unlock a live child.
        os.close(descriptor)


def main(arguments):
    contained = bool(arguments and arguments[0] == "--contained")
    if contained:
        arguments = arguments[1:]
    parsed = workflow_parse(arguments)
    workflow_control_plane()
    if parsed is None:
        if contained:
            refuse("status is not a contained execution operation")
        return workflow_status()
    if (CREME_ROOT / ".creme/lean-heavy-suspended").exists():
        refuse("host circuit breaker is active")
    profile, goal, operation, mode, purpose = parsed
    repo = workflow_worktree(profile, goal, purpose)
    workflow_command(repo, operation, mode)
    if contained:
        return workflow_service(arguments, parsed)
    print(json.dumps({"status": "LAUNCHING", "unit": UNIT, "profile": profile,
                      "goal": goal, "operation": operation, "mode": mode}), flush=True)
    return subprocess.run([
        str(SYSTEMD_RUN), "--user", "--wait", "--collect", "--pipe", "--quiet",
        "--unit=" + UNIT, "--slice=creme-lean.slice",
        "--property=MemoryAccounting=yes", "--property=MemoryHigh=infinity",
        "--property=MemoryMax=8G", "--property=MemorySwapMax=" + ("0" if profile == "blanc" else "1G"),
        "--property=OOMScoreAdjust=500", "--property=OOMPolicy=kill",
        "--property=KillMode=control-group",
        str(Path(__file__).resolve()), "--contained", *arguments,
    ], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
