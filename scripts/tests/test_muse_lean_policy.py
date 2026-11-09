"""The Lean-mode shell approval policy (``creme/muse_lean_policy.py``) and its use by ``decide_approval``."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from creme import muse_client as C
from creme import muse_lean_policy as P

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "scripts/tests/fixtures/muse/lean_policy_corpus.json"


def stages_for(command: str) -> list:
    """Stages as Muse reports them: one per simple command; none when a word uses a variable."""
    simples = [simple for _, pipeline in P._parse(command) for simple in pipeline]
    if any(part[0] == "var" for simple in simples for word in simple.words for part in word.parts):
        return []
    return [{"argv": [word.compare() for word in simple.words],
             "argvComplete": not any(word.has_vars() for word in simple.words) and not simple.redirects}
            for simple in simples]


class PolicyCase(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="leanpol")).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.home = self.base / "home"
        self.ws = self.home / "jaune/.worktrees/g"
        (self.ws / "JauneConsensus/Facts").mkdir(parents=True)
        (self.ws / "JauneConsensus/Facts/A.lean").write_text("theorem a : True := trivial\n")
        (self.ws / "scripts").mkdir()
        (self.home / ".elan/toolchains/lean/src").mkdir(parents=True)
        (self.home / "secret.txt").write_text("no\n")
        self.gate_failure = None
        self.ctx = P.context(self.ws, "g", (), home=self.home, scripts_check=lambda workspace: self.gate_failure)

    def evaluate(self, command: str, stages=...):
        return P.evaluate(command, stages_for_safe(command) if stages is ... else stages, self.ctx)

    def assertApproved(self, command: str, stages=...):
        ok, why = self.evaluate(command, stages)
        self.assertTrue(ok, f"{command!r} went to the master: {why}")
        return why

    def assertMaster(self, command: str, stages=...):
        ok, why = self.evaluate(command, stages)
        self.assertFalse(ok, f"{command!r} was approved: {why}")
        return why


class SafetyBoundaryTest(PolicyCase):
    """Every fail-closed boundary, one rejected command per way across it."""

    def check_all_master(self, commands):
        for command in commands:
            with self.subTest(command=command):
                self.assertMaster(command)

    def test_no_writes_outside_the_workspace(self):
        self.check_all_master([
            "cat JauneConsensus/Facts/A.lean > /tmp/out.txt",
            "cat JauneConsensus/Facts/A.lean > ../out",
            "cat JauneConsensus/Facts/A.lean >> ~/out",
            "echo x 2> /etc/x",
            "echo x &> /tmp/x.log",                       # /tmp/NAME.log is only for the owned build
            "grep -n a JauneConsensus/Facts/A.lean > /tmp/a.log",
            "cp JauneConsensus/Facts/A.lean /tmp/x",
            "mv JauneConsensus/Facts/A.lean /tmp/x",
            "rm -rf JauneConsensus",
            "rm JauneConsensus/Facts/A.lean",
            "echo x | tee /tmp/x",
            "echo x | tee out",
            "sed -i 's/a/b/' /etc/hosts",
            "sed -i 's/a/b/' ../other/A.lean",
            f"sed -i 's/a/b/' {self.home}/secret.txt",
            "sed -i 's/a/b/' .git/config",
            "mkdir -p /tmp/newdir",
            "mkdir -p .git/hooks",
            "sort -o /tmp/x JauneConsensus/Facts/A.lean",
            "uniq JauneConsensus/Facts/A.lean /tmp/x",
            "python3 -c \"open('/tmp/x', 'w').write('1')\"",
            "python3 -c \"open('../x', 'w').write('1')\"",
            "python3 -c \"open('~/x', 'a').write('1')\"",
            "python3 -c \"p = '/etc/x'\nopen(p, 'w').write('1')\"",
            "python3 -c \"p = 'ok'\np = '/etc/x'\nopen(p, 'w').write('1')\"",
            "python3 -c \"f = open\nf('/etc/x', 'w')\"",
            "python3 -c \"open('x', 'w', opener=print)\"",
            "cd /tmp && python3 -c \"open('x', 'w').write('1')\"",
        ])

    def test_symlinks_cannot_carry_a_write_or_read_out_of_the_roots(self):
        (self.ws / "link").symlink_to(self.home / "secret.txt")
        self.assertMaster("sed -i 's/a/b/' link")
        self.assertMaster("cat link")
        self.assertMaster("echo x > link")

    def test_no_os_subprocess_or_system_escapes(self):
        self.check_all_master([
            "python3 -c \"import os; os.system('id')\"",
            "python3 -c \"import subprocess; subprocess.run(['id'])\"",
            "python3 -c \"import sys\"",
            "python3 -c \"from os import system\"",
            "python3 -c \"__import__('os').system('id')\"",
            "python3 -c \"eval('1')\"",
            "python3 -c \"exec('1')\"",
            "python3 -c \"getattr(str, 'x')\"",
            "python3 -c \"print(().__class__.__bases__)\"",
            "python3 -c \"import re as os; globals()\"",
            "python3 - <<'EOF'\nimport shutil\nEOF",
            "python3 - <<EOF\nprint('$HOME')\nEOF",
            "python3 scripts/other.py",
            "python3 -m creme status",
            "awk 'BEGIN { system(\"id\") }'",
            "awk 'BEGIN { \"id\" | getline x }'",
            "awk '{ print | \"sh\" }' JauneConsensus/Facts/A.lean",
            "awk '{ print > \"/tmp/x\" }' JauneConsensus/Facts/A.lean",
            "awk -f prog.awk JauneConsensus/Facts/A.lean",
            "awk '@load \"x\"'",
            "find . -exec id ;",
            "find . -execdir id +",
            "find . -ok id ;",
            "find . -delete",
            "find . -fprint /tmp/x",
            "sed -n '1e id' JauneConsensus/Facts/A.lean",
            "sed -n 's/a/b/e' JauneConsensus/Facts/A.lean",
            "sed -n 's/a/b/w /tmp/x' JauneConsensus/Facts/A.lean",
            "sed -n 'w /tmp/x' JauneConsensus/Facts/A.lean",
            "sed -n 'r /etc/passwd' JauneConsensus/Facts/A.lean",
            "sed -f script.sed JauneConsensus/Facts/A.lean",
            "sort --compress-program=sh JauneConsensus/Facts/A.lean",
            "bash -c id",
            "sh -c id",
            "echo id | sh",
            "xargs cat",
            "git --exec-path=/tmp status",
            "git -c core.pager=sh log",
            "git diff --ext-diff",
            "git grep -O x",
            "PATH=/tmp; ls",
            "LD_PRELOAD=/tmp/x.so; ls",
            "PYTHONPATH=. python3 -c 'print(1)'",
            "BASH_ENV=/tmp/x; ls",
        ])

    def test_python_cannot_import_a_module_planted_in_its_directory(self):
        (self.ws / "re.py").write_text("print('planted')\n")
        self.assertMaster("python3 -c \"import re\"")
        (self.ws / "re.py").unlink()
        self.assertApproved("python3 -c \"import re\"")

    def test_no_backticks_or_command_substitution_but_the_build_log_idiom(self):
        self.check_all_master([
            "echo `id`",
            "echo \"`id`\"",
            "echo $(id)",
            "echo \"$(id)\"",
            "cat $(ls JauneConsensus)",
            "echo $((1+1))",
            "echo ${HOME}",
            "echo $HOME",
            "echo $1",
            "cat <(id)",
            "diff <(ls) <(ls)",
            "cat <<< x",
            "(cat JauneConsensus/Facts/A.lean)",
            "{ cat JauneConsensus/Facts/A.lean; }",
            "cat JauneConsensus/Facts/A.lean &",
            "echo a{b,c}",
            f"L=$(ls -t {self.home}/creme/.creme/lean-build-ownership/logs/other-*.log | head -1); cat \"$L\"",
            f"L=$(ls -t /tmp/*.log | head -1); cat \"$L\"",
            f"L=$(ls -t {self.home}/creme/.creme/lean-build-ownership/logs/g-*.log | head -1); rm \"$L\"",
        ])
        logs = self.home / "creme/.creme/lean-build-ownership/logs"
        why = self.assertApproved(f"L=$(ls -t {logs}/g-*.log | head -1); echo \"$L\"; grep -n \"error\" \"$L\" | head")
        self.assertIn("newest own build log", why)

    def test_no_network_tools(self):
        self.check_all_master([
            "curl https://example.com", "wget https://example.com", "ssh host", "scp a host:b", "nc host 80",
            "git fetch", "git pull", "git ls-remote origin", "git clone https://example.com/x",
            "python3 -c \"import urllib.request\"", "python3 -c \"import socket\"",
        ])

    def test_semaphore_and_reclaim_stay_aborted(self):
        workspace = str(self.ws)
        for command in ("~/creme/.semaphore/semaphore status", "python3 -m creme reclaim --wind-down g",
                        "ls; ~/creme/scripts/creme semaphore status"):
            with self.subTest(command=command):
                params = approval(command, workspace, stages_for_safe(command))
                decision = C.decide_approval(params, "lean", "g", self.ws, lean_policy_context=self.ctx)
                self.assertEqual(decision.action, "abort", command)

    def test_no_git_mutation(self):
        self.check_all_master([
            "git commit -m x", "git push", "git reset --hard", "git checkout main", "git stash",
            "git switch main", "git branch -D x", "git tag x", "git restore .", "git clean -fdx",
            "git add .", "git rm x", "git merge x", "git rebase main", "git apply x.patch",
            "git config core.pager sh", "git worktree add x", "git log --output=/tmp/x",
            "git diff -o /tmp/x", "git show --output=x", "git diff --no-index /etc/passwd x",
            "git -C / status", f"git -C {self.home} log",
        ])

    def test_git_refuses_a_git_directory_the_session_could_write(self):
        (self.ws / "evil").mkdir()
        (self.ws / ".git").write_text("gitdir: evil\n")
        self.assertMaster("git status")
        (self.ws / ".git").write_text(f"gitdir: {self.base}/real-gitdir\n")
        self.assertApproved("git status")
        (self.ws / ".git").unlink()
        (self.ws / ".git").mkdir()
        self.assertMaster("git status")

    def test_reads_stay_inside_the_read_roots(self):
        self.check_all_master([
            "cat /etc/passwd", "cat ~/.ssh/id_rsa", f"cat {self.home}/secret.txt", "cat ../../secret.txt",
            "ls /", "ls ..", "ls .*", "grep -rn x /etc", "grep -f /etc/passwd x", "grep --file=/etc/passwd x",
            "grep -R x .", "head -c 10 -/etc/passwd", "find / -name x", "find ~ -name x", "find . -L",
            "cd /etc && cat passwd", "cd .. ; ls", "X=/etc; cat $X/passwd", "cat < /etc/passwd",
            "python3 -c \"print(open('/etc/passwd').read())\"", "awk '1' /etc/passwd",
            "diff /etc/passwd JauneConsensus/Facts/A.lean", "tail -f JauneConsensus/Facts/A.lean",
        ])

    def test_muse_stages_must_agree_with_the_policy_parse(self):
        command = "cat JauneConsensus/Facts/A.lean | head"
        self.assertApproved(command)
        self.assertMaster(command, stages=[{"argv": ["cat", "JauneConsensus/Facts/A.lean"], "argvComplete": True}])
        self.assertMaster(command, stages=[{"argv": ["cat", "/etc/passwd"], "argvComplete": True},
                                           {"argv": ["head"], "argvComplete": True}])
        self.assertMaster(command, stages=[])           # no variable in the command, so stages are required
        self.assertMaster(command, stages=None)
        self.assertMaster("ls\n", stages="nonsense")


class PositiveRulesTest(PolicyCase):
    def test_rules_from_the_brief(self):
        logs = self.home / "creme/.creme/lean-build-ownership/logs"
        for command in (
            "ls JauneConsensus/ && ls JauneConsensus/Facts/",
            "grep -rn \"theorem\" JauneConsensus/ | head -20; echo ---; wc -l JauneConsensus/Facts/A.lean",
            "sed -n '1,60p' JauneConsensus/Facts/A.lean",
            "sed -i 's/trivial/by trivial/' JauneConsensus/Facts/A.lean && grep -c trivial JauneConsensus/Facts/A.lean",
            "find JauneConsensus -name '*.lean' -newer JauneConsensus/Facts/A.lean",
            "git status --short | head -20 && git log --oneline -3",
            f"git -C {self.ws} diff --stat",
            f"cd {self.ws} && git diff HEAD -- JauneConsensus",
            "mkdir -p JauneConsensus/New",
            "python3 -c \"import re, json; print(json.dumps(re.findall('a', 'aa')))\"",
            f"cd {self.ws} && python3 - <<'EOF'\np = 'JauneConsensus/Facts/A.lean'\nsrc = open(p).read()\n"
            "open(p, 'w').write(src.replace('a', 'a'))\nEOF\ngrep -c a JauneConsensus/Facts/A.lean",
            "awk 'NR>=1 && NR<=5 {print NR\": \"$0}' JauneConsensus/Facts/A.lean",
            "~/creme/scripts/creme lake-build g --wait 900 -- JauneConsensus.Facts.A > /tmp/g-build1.log 2>&1; "
            "tail -c 1500 /tmp/g-build1.log",
            "~/creme/scripts/creme lake-build g --wait 900 -- JauneConsensus.Facts.A 2>&1 | grep -E \"^error\" | head; "
            "echo \"EXIT:$?\"",
            f"L={logs}/g-20261109T102258.797893Z-2749276.log; grep -n \"error\" $L | head -2",
            "grep -rn foldl ~/.elan/toolchains/lean/src/ | head",
            "sleep 30; date",
            "echo \"column.*$\"",
        ):
            with self.subTest(command=command):
                self.assertApproved(command)

    def test_gate_scripts_need_a_clean_scripts_directory(self):
        for command in ("scripts/check-hygiene.sh", "bash scripts/check-integrity.sh",
                        "./scripts/check-hygiene.sh", "python3 scripts/check-assurance-manifest.py",
                        "python3 -B scripts/check-assurance-manifest.py"):
            with self.subTest(command=command):
                self.assertApproved(command)
                self.gate_failure = "scripts/ differs from HEAD"
                self.assertMaster(command)
                self.gate_failure = None
        self.assertMaster("bash scripts/check-proof-duplication.sh")
        self.assertMaster("bash scripts/check-hygiene.sh --fix")
        self.assertMaster(f"cd {self.home}/.elan/toolchains && bash scripts/check-hygiene.sh")

    def test_gate_scripts_failure_reads_git(self):
        main = self.base / "main"
        (main / "scripts").mkdir(parents=True)
        subprocess.run(["git", "-C", str(main), "init", "-q"], check=True)
        (main / ".gitignore").write_text("__pycache__/\n")
        (main / "scripts/check-hygiene.sh").write_text("echo ok\n")
        subprocess.run(["git", "-C", str(main), "add", "."], check=True)
        subprocess.run(["git", "-C", str(main), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
                       check=True)
        self.assertIn("inside the workspace", P.gate_scripts_failure(main))   # a main clone's .git is writable
        repo = self.base / "worktree"
        subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", "--detach", str(repo)], check=True)
        git = ["git", "-C", str(repo)]
        (repo / "scripts/__pycache__").mkdir()
        (repo / "scripts/__pycache__/m.cpython-314.pyc").write_bytes(b"x")
        self.assertIsNone(P.gate_scripts_failure(repo))
        (repo / "scripts/check-hygiene.sh").write_text("id\n")
        self.assertIn("differs", P.gate_scripts_failure(repo))
        subprocess.run(git + ["checkout", "-q", "--", "scripts"], check=True)
        (repo / "scripts/new.py").write_text("")
        self.assertIn("differs", P.gate_scripts_failure(repo))

    def test_read_roots(self):
        extra = self.base / "spec"
        extra.mkdir()
        (extra / "a.txt").write_text("x\n")
        self.assertMaster(f"cat {extra}/a.txt")
        self.ctx = P.context(self.ws, "g", [extra], home=self.home, scripts_check=lambda w: None)
        self.assertApproved(f"cat {extra}/a.txt")
        self.assertMaster(f"sed -i 's/x/y/' {extra}/a.txt")          # read roots are never writable
        self.assertIsNone(P.read_root_failure(str(extra)))
        self.assertIsNotNone(P.read_root_failure("/"))
        self.assertIsNotNone(P.read_root_failure(str(Path.home())))
        self.assertIsNotNone(P.read_root_failure("relative/dir"))
        self.assertIsNotNone(P.read_root_failure(str(self.base / "missing")))
        defaults = P.context(self.ws, "g", home=self.home).read_roots
        self.assertEqual(defaults, (self.ws, self.home / ".elan/toolchains", self.home / ".local/share/muse/sessions"))


def approval(command: str, workspace: str, stages) -> dict:
    return {"approvalId": "a", "currentRequirementId": {"approvalId": "a", "sourceIndex": 0},
            "availableChoices": [{"choiceId": "once", "decision": "approved", "scope": "once"},
                                 {"choiceId": "abort", "decision": "abort", "scope": "once"}],
            "subject": {"kind": "shell", "command": command, "workspaceRoot": workspace, "stages": stages},
            "toolName": "bash"}


def stages_for_safe(command: str):
    try:
        return stages_for(command)
    except P.Refusal:     # unparseable (or the build-log idiom): Muse then sends no stages either
        return []


class DecideApprovalTest(PolicyCase):
    def decide(self, command: str, workspace=None, stages=None):
        params = approval(command, str(workspace or self.ws), stages_for_safe(command) if stages is None else stages)
        return C.decide_approval(params, "lean", "g", self.ws, lean_policy_context=self.ctx)

    def test_policy_approval_carries_its_reason(self):
        decision = self.decide("grep -n a JauneConsensus/Facts/A.lean | head")
        self.assertEqual((decision.action, decision.choice), ("approve", "once"))
        self.assertTrue(decision.reason.startswith(P.POLICY_NAME), decision.reason)
        self.assertIn("read grep", decision.reason)

    def test_unmatched_goes_to_the_master_with_the_policy_reason(self):
        decision = self.decide("rm -rf JauneConsensus")
        self.assertEqual((decision.action, decision.choice), ("master", None))
        self.assertIn("outside the Lean shell policy", decision.reason)
        self.assertIn("rm", decision.reason)

    def test_another_workspace_goes_to_the_master(self):
        self.assertEqual(self.decide("ls", workspace=self.base).action, "master")

    def test_sandboxed_modes_are_unchanged(self):
        params = approval("rm -rf /", str(self.ws), [])
        self.assertEqual(C.decide_approval(params, "write", None, self.ws).action, "approve")
        self.assertEqual(C.decide_approval({**params, "protectedWrite": True}, "lean", "g", self.ws,
                                           lean_policy_context=self.ctx).action, "abort")
        self.assertEqual(C.decide_approval({**approval("ls", str(self.ws), [{"argv": ["ls"], "argvComplete": True}]),
                                            "subagentOrigin": {"id": "x"}}, "lean", "g", self.ws,
                                           lean_policy_context=self.ctx).action, "abort")

    def test_read_roots_reach_the_policy(self):
        extra = self.base / "spec"
        extra.mkdir()
        command = f"cat {extra}/a.txt"
        params = approval(command, str(self.ws), stages_for(command))
        self.assertEqual(C.decide_approval(params, "lean", "g", self.ws).action, "master")
        self.assertEqual(C.decide_approval(params, "lean", "g", self.ws, (str(extra),)).action, "approve")


class RealCorpusTest(unittest.TestCase):
    """Real commands from the 2026-11-09 Muse Lean units: the safe sample approves, the rest stays with the master."""

    @classmethod
    def setUpClass(cls):
        cls.corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
        cls.base = Path(tempfile.mkdtemp(prefix="leancorpus")).resolve()
        cls.home = cls.base / "home"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, True)

    def instantiate(self, item: dict):
        text = json.dumps(item).replace("{home}", str(self.home))
        item = json.loads(text)
        workspace = Path(item["workspace"])
        workspace.mkdir(parents=True, exist_ok=True)
        roots = [root.replace("{home}", str(self.home)) for root in self.corpus["extra_read_roots"]]
        ctx = P.context(workspace, item["goal"], roots, home=self.home, scripts_check=lambda w: None)
        return item, ctx

    def test_the_safe_sample_approves(self):
        self.assertGreaterEqual(len(self.corpus["approve"]), 40)
        for raw in self.corpus["approve"]:
            item, ctx = self.instantiate(raw)
            with self.subTest(command=item["command"][:120]):
                ok, why = P.evaluate(item["command"], item["stages"], ctx)
                self.assertTrue(ok, why)

    def test_the_rest_stays_with_the_master(self):
        for raw in self.corpus["master"]:
            item, ctx = self.instantiate(raw)
            with self.subTest(command=item["command"][:120]):
                ok, why = P.evaluate(item["command"], item["stages"], ctx)
                self.assertFalse(ok, why)


if __name__ == "__main__":
    unittest.main()
