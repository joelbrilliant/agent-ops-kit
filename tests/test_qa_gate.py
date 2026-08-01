"""Production-path tests for the isolated repository QA release gate."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from agent_ops.config import RepoPolicy, load_config
from agent_ops.qa import gate as gate_module
from agent_ops.qa.gate import run_gate
from agent_ops.qa.spec import MAX_CHECKS_PER_CRITERION, MAX_CRITERIA, parse_spec

from conftest import make_config, write_executable


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True,
                          capture_output=True).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", "https://github.com/operator/demo.git")
    (repo / "demo.txt").write_text("safe\n", encoding="utf-8")
    _git(repo, "add", "demo.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def _spec(repo: Path, *, checks: list[str] | None = None) -> dict[str, object]:
    return {"schema": "QaGateSpecV1", "schema_version": 1,
            "repository": "operator/demo", "base_sha": _git(repo, "rev-parse", "HEAD"),
            "criteria": {"AC-1": checks or ["unit"]}}


def _gate(tmp_path: Path, spec: dict[str, object], command: list[str] | None = None):
    cfg = make_config(tmp_path)
    commands = {"unit": command or [sys.executable, "-c", "import os; assert not os.getenv('GH_TOKEN')"]}
    object.__setattr__(cfg, "repository_policies", {
        "operator/demo": RepoPolicy(name="operator/demo", verification_commands=commands)
    })
    return cfg


def _cli_config(tmp_path: Path, command: list[str]) -> Path:
    """A complete owner-only config for exercising the installed CLI boundary."""
    scripts = tmp_path / "scripts"
    noop = [sys.executable, "-c", "raise SystemExit(0)"]
    data = {
        "operator_logins": ["operator"], "owned_namespaces": ["operator"],
        "excluded_repositories": [], "trusted_reviewer_logins": ["reviewer"],
        "workspace_root": str(tmp_path / "workspace"), "state_dir": str(tmp_path / "state"),
        "protected_path_patterns": [], "classifier_command": noop + ["{request_path}", "{response_path}"],
        "builder_command": noop + ["{request_path}", "{response_path}", "{worktree_path}"],
        "reviewer_command": noop + ["{request_path}", "{response_path}", "{worktree_path}"],
        "required_runner_identity": {"profile": "p", "provider": "p", "model": "m", "reasoning_effort": "low", "service_tier": "standard"},
        "capability_isolation": {"enabled": True, "environment_allowlist": ["PATH"]},
        "notification_mode": "quiet", "default_verification_commands": {}, "gh_command": str(scripts / "absent-gh"),
        "repository_policies": {"operator/demo": {"permitted_paths": [], "verification_commands": {"unit": command}}},
    }
    path = tmp_path / "qa-config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_qa_gate_pass_maps_every_criterion_once(tmp_path: Path):
    repo = _repo(tmp_path)
    spec = _spec(repo)
    outcome = run_gate(_gate(tmp_path, spec), repo, spec)
    assert outcome.exit_code == 0, outcome.render()
    assert outcome.bundle.verdict == "PASS"
    assert [item.check_id for item in outcome.bundle.checks] == ["repository-state", "unit"]
    assert "criteria=AC-1" in outcome.bundle.checks[1].evidence_refs


def test_qa_gate_one_check_satisfies_multiple_criteria_once(tmp_path: Path):
    repo = _repo(tmp_path)
    spec = _spec(repo)
    spec["criteria"] = {"AC-1": ["unit"], "AC-2": ["unit"]}
    outcome = run_gate(_gate(tmp_path, spec), repo, spec)
    assert outcome.exit_code == 0
    assert [row.check_id for row in outcome.bundle.checks] == ["repository-state", "unit"]
    assert "criteria=AC-1,AC-2" in outcome.bundle.checks[1].evidence_refs


def test_qa_gate_rejects_unknown_check_before_execution(tmp_path: Path):
    repo = _repo(tmp_path)
    spec = _spec(repo, checks=["unknown"])
    cfg = _gate(tmp_path, spec)
    wildcard_marker = tmp_path / "wildcard-executed"
    object.__setattr__(cfg, "repository_policies", {
        "operator/demo": RepoPolicy(name="operator/demo", verification_commands={"unit": [sys.executable, "-c", "pass"]}),
        "*": RepoPolicy(
            name="*",
            verification_commands={
                "unknown": [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(wildcard_marker)!r}).write_text('x')",
                ]
            },
        ),
    })
    outcome = run_gate(cfg, repo, spec)
    assert outcome.exit_code == 2
    assert outcome.bundle.verdict == "HOLD"
    assert not wildcard_marker.exists()
    command_bearing = dict(_spec(repo))
    command_bearing["commands"] = {"unit": ["python", "-c", "pass"]}
    assert run_gate(cfg, repo, command_bearing).exit_code == 2


def test_qa_gate_rejects_stale_dirty_and_wrong_repository(tmp_path: Path):
    repo = _repo(tmp_path)
    spec = _spec(repo)
    spec["base_sha"] = "0" * 40
    assert run_gate(_gate(tmp_path, spec), repo, spec).exit_code == 1
    spec["base_sha"] = _git(repo, "rev-parse", "HEAD")
    (repo / "untracked").write_text("x", encoding="utf-8")
    assert run_gate(_gate(tmp_path, spec), repo, spec).exit_code == 1
    (repo / "untracked").unlink()
    _git(repo, "remote", "set-url", "origin", "https://github.com/operator/wrong.git")
    assert run_gate(_gate(tmp_path, spec), repo, spec).exit_code == 1
    _git(repo, "remote", "set-url", "origin", "https://github.com/operator/demo.git")
    marker = repo / _git(repo, "rev-parse", "--git-path", "MERGE_HEAD")
    marker.write_text("x", encoding="utf-8")
    assert run_gate(_gate(tmp_path, spec), repo, spec).exit_code == 1
    marker.unlink()


def test_qa_gate_rejects_stale_and_dirty_before_durable_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = _repo(tmp_path)
    spec = _spec(repo)

    def snapshot_must_not_run(*args, **kwargs):
        raise AssertionError("durable snapshot ran before stale or dirty rejection")

    monkeypatch.setattr(gate_module, "repository_state", snapshot_must_not_run)
    stale = dict(spec)
    stale["base_sha"] = "0" * 40
    assert run_gate(_gate(tmp_path, stale), repo, stale).exit_code == 1
    (repo / "untracked").write_text("x", encoding="utf-8")
    assert run_gate(_gate(tmp_path, spec), repo, spec).exit_code == 1


def test_qa_gate_rejects_parent_symlinks_and_path_swap_races(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    repo = _repo(real_parent)
    spec = _spec(repo)
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)
    assert run_gate(_gate(tmp_path, spec), alias / "repo", spec).exit_code == 1

    moved = tmp_path / "moved-repo"
    real_run = subprocess.run
    swapped = False

    def swap_before_clone(argv, *args, **kwargs):
        nonlocal swapped
        if not swapped and isinstance(argv, list) and len(argv) > 1 and argv[1] == "clone":
            repo.rename(moved)
            repo.symlink_to(moved, target_is_directory=True)
            swapped = True
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(gate_module.subprocess, "run", swap_before_clone)
    outcome = run_gate(_gate(tmp_path, spec), repo, spec)
    assert swapped and outcome.exit_code == 1
    assert (moved / "demo.txt").read_text(encoding="utf-8") == "safe\n"
    assert sum(row.check_id == "repository-state" for row in outcome.bundle.checks) == 1


def test_qa_gate_rejects_escaping_tracked_symlink_before_execution(tmp_path: Path):
    repo = _repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("safe\n", encoding="utf-8")
    (repo / "escape").symlink_to(outside)
    _git(repo, "add", "escape")
    _git(repo, "commit", "-m", "add symlink")
    executed = tmp_path / "executed"
    command = [sys.executable, "-c", f"from pathlib import Path; Path({str(executed)!r}).write_text('x')"]
    outcome = run_gate(_gate(tmp_path, _spec(repo), command), repo, _spec(repo))
    assert outcome.exit_code == 1
    assert not executed.exists()
    assert outside.read_text(encoding="utf-8") == "safe\n"


def test_qa_gate_handles_submodules_and_rejects_nonregular_tracked_entries(tmp_path: Path):
    child = tmp_path / "child"
    child.mkdir()
    _git(child, "init")
    _git(child, "config", "user.email", "test@example.invalid")
    _git(child, "config", "user.name", "Test")
    (child / "child.txt").write_text("child\n", encoding="utf-8")
    _git(child, "add", "child.txt")
    _git(child, "commit", "-m", "child")
    repo = _repo(tmp_path)
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", str(child), "vendor/child")
    _git(repo, "commit", "-m", "add submodule")
    assert run_gate(_gate(tmp_path, _spec(repo)), repo, _spec(repo)).exit_code == 0

    tracked = repo / "demo.txt"
    tracked.unlink()
    os.mkfifo(tracked)
    try:
        assert run_gate(_gate(tmp_path, _spec(repo)), repo, _spec(repo)).exit_code == 1
    finally:
        tracked.unlink()


def test_qa_gate_runs_only_in_disposable_clone(tmp_path: Path):
    repo = _repo(tmp_path)
    marker = tmp_path / "clone-cwd"
    command = [sys.executable, "-c", "from pathlib import Path; Path('made-in-clone').write_text('x')"]
    outcome = run_gate(_gate(tmp_path, _spec(repo), command), repo, _spec(repo))
    assert outcome.exit_code == 0
    assert not (repo / "made-in-clone").exists()
    assert not marker.exists()


def test_qa_gate_source_is_unchanged_after_pass_failure_and_attack(tmp_path: Path):
    repo = _repo(tmp_path)
    before = _source_proof(repo)
    cases = [
        ([sys.executable, "-c", "raise SystemExit(0)"], 0),
        ([sys.executable, "-c", "raise SystemExit(7)"], 1),
        ([sys.executable, "-c", "from pathlib import Path; Path('demo.txt').write_text('bad')"], 1),
    ]
    for command, expected_exit in cases:
        outcome = run_gate(_gate(tmp_path, _spec(repo), command), repo, _spec(repo))
        assert outcome.exit_code == expected_exit
        assert _source_proof(repo) == before


@pytest.mark.parametrize(
    "command",
    [
        ["sh", "-c", "true"],
        ["python\n", "-c", "pass"],
        ["env", "python", "-c", "pass"],
        ["BASH.EXE", "-c", "true"],
        [r"C:\Windows\System32\cmd.exe", "/c", "exit 0"],
        ["tool.cmd", "/c", "exit 0"],
        ["gh.exe", "api", "repos/operator/demo"],
        ["claude", "-p", "test"],
        ["busybox", "sh", "-c", "true"],
        ["uv", "run", "pytest"],
        ["npm", "exec", "tool"],
    ],
)
def test_qa_gate_rejects_shell_wrappers_and_control_characters(tmp_path: Path, command: list[str]):
    repo = _repo(tmp_path)
    assert run_gate(_gate(tmp_path, _spec(repo), command), repo, _spec(repo)).exit_code == 2


def test_qa_gate_strips_credentials_and_github_capability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("GH_TOKEN", "private-marker")
    command = [sys.executable, "-c", "import os; raise SystemExit(bool(os.getenv('GH_TOKEN') or os.getenv('SSH_AUTH_SOCK')))" ]
    assert run_gate(_gate(tmp_path, _spec(repo), command), repo, _spec(repo)).exit_code == 0


def test_qa_gate_isolates_all_git_calls_credentials_network_hints_and_tmpdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = _repo(tmp_path)
    operator_tmp = tmp_path / "operator-tmp"
    operator_tmp.mkdir()
    malicious_global = tmp_path / "global.gitconfig"
    malicious_global.write_text(
        "[url \"https://github.com/wrong/\"]\n\tinsteadOf = https://github.com/operator/\n",
        encoding="utf-8",
    )
    inherited = {
        "TMPDIR": str(operator_tmp),
        "GIT_CONFIG_GLOBAL": str(malicious_global),
        "GH_TOKEN": "private-marker",
        "GITHUB_TOKEN": "private-marker",
        "SLACK_BOT_TOKEN": "private-marker",
        "DISCORD_TOKEN": "private-marker",
        "TELEGRAM_BOT_TOKEN": "private-marker",
        "SSH_AUTH_SOCK": str(tmp_path / "agent.sock"),
        "OPENAI_API_KEY": "private-marker",
        "ANTHROPIC_API_KEY": "private-marker",
    }
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)
    code = """
import os, stat, subprocess
from pathlib import Path
denied = ['GH_TOKEN','GITHUB_TOKEN','SLACK_BOT_TOKEN','DISCORD_TOKEN','TELEGRAM_BOT_TOKEN','SSH_AUTH_SOCK','OPENAI_API_KEY','ANTHROPIC_API_KEY']
assert not any(os.getenv(name) for name in denied)
tmp = Path(os.environ['TMPDIR'])
assert tmp != Path(%r) and tmp.parent == Path(os.environ['HOME']).parent
assert stat.S_IMODE(tmp.stat().st_mode) == 0o700
(tmp / 'check-artifact').write_text('temporary')
assert os.environ['GIT_CONFIG_GLOBAL'] == os.devnull
assert os.environ['GIT_ALLOW_PROTOCOL'] == 'file'
attempt = subprocess.run(['git', 'ls-remote', 'https://github.com/operator/demo.git'], capture_output=True)
assert attempt.returncode != 0
""" % str(operator_tmp)
    command = [sys.executable, "-c", "exec(" + repr(code) + ")"]
    outcome = run_gate(_gate(tmp_path, _spec(repo), command), repo, _spec(repo))
    assert outcome.exit_code == 0
    assert not (operator_tmp / "check-artifact").exists()


def test_qa_gate_rejects_private_identifiers_without_rendering_them(tmp_path: Path):
    repo = _repo(tmp_path)
    spec = _spec(repo)
    spec["criteria"] = {"private-marker": ["unit"]}
    outcome = run_gate(_gate(tmp_path, spec), repo, spec)
    assert outcome.exit_code == 2
    assert "private-marker" not in outcome.render()


@pytest.mark.parametrize("body", ["Path('demo.txt').write_text('x')", "subprocess.run(['git','config','x.y','z'], check=True)", "Path('.git/hooks/x').write_text('x')", "subprocess.run(['git','-c','user.email=t@example.invalid','-c','user.name=t','commit','--allow-empty','-m','x'], check=True)"])
def test_qa_gate_holds_on_clone_file_config_hook_and_history_mutation(tmp_path: Path, body: str):
    repo = _repo(tmp_path)
    code = "from pathlib import Path; import subprocess; " + body
    command = [sys.executable, "-c", code]
    assert run_gate(_gate(tmp_path, _spec(repo), command), repo, _spec(repo)).exit_code == 1


@pytest.mark.parametrize(
    "body",
    [
        "p=Path('demo.txt'); q=Path('replacement'); q.write_bytes(p.read_bytes()); q.replace(p)",
        "subprocess.run(['git','branch','hidden'], check=True)",
        "subprocess.run(['git','-c','user.email=t@example.invalid','-c','user.name=t','commit','--allow-empty','-m','x'], check=True); subprocess.run(['git','reset','--hard','HEAD^'], check=True)",
    ],
)
def test_qa_gate_detects_inode_ref_and_reset_history_mutation(tmp_path: Path, body: str):
    repo = _repo(tmp_path)
    code = "from pathlib import Path; import subprocess; " + body
    outcome = run_gate(_gate(tmp_path, _spec(repo), [sys.executable, "-c", code]), repo, _spec(repo))
    assert outcome.exit_code == 1


def test_qa_gate_hashes_hook_symlinks_without_following_external_targets(tmp_path: Path):
    repo = _repo(tmp_path)
    external = tmp_path / "external-hook-target"
    external.write_text("private-marker", encoding="utf-8")
    hook = repo / ".git" / "hooks" / "external"
    hook.symlink_to(external)
    outcome = run_gate(_gate(tmp_path, _spec(repo)), repo, _spec(repo))
    assert outcome.exit_code == 0
    assert "private-marker" not in outcome.render()


def test_qa_gate_detects_absolute_source_mutation_without_claiming_os_sandbox(tmp_path: Path):
    repo = _repo(tmp_path)
    source_file = repo / "demo.txt"
    original_mode = stat.S_IMODE(source_file.stat().st_mode)
    changed_mode = original_mode ^ stat.S_IXUSR
    code = "import os,sys; os.chmod(sys.argv[1], int(sys.argv[2]))"
    command = [sys.executable, "-c", code, str(source_file), str(changed_mode)]
    try:
        outcome = run_gate(_gate(tmp_path, _spec(repo), command), repo, _spec(repo))
        assert outcome.exit_code == 1
        assert outcome.bundle.checks[0].status == "HOLD"
        assert "hold_code=source_not_clean" in outcome.bundle.checks[0].evidence_refs
    finally:
        source_file.chmod(original_mode)


def test_qa_gate_rejects_in_progress_operation_in_linked_worktree(tmp_path: Path):
    repo = _repo(tmp_path)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "--detach", str(linked), "HEAD")
    marker_text = _git(linked, "rev-parse", "--git-path", "rebase-merge")
    marker = Path(marker_text) if os.path.isabs(marker_text) else linked / marker_text
    marker.mkdir(parents=True)
    outcome = run_gate(_gate(tmp_path, _spec(linked)), linked, _spec(linked))
    assert outcome.exit_code == 1


def test_qa_gate_bundle_is_deterministic_and_private(tmp_path: Path):
    repo = _repo(tmp_path)
    spec = _spec(repo)
    cfg = _gate(tmp_path, spec)
    one = run_gate(cfg, repo, spec).render()
    two = run_gate(cfg, repo, spec).render()
    assert one == two
    assert str(repo) not in one and "private-marker" not in one and "-c" not in one
    boundary = json.loads(one)["redaction_record"]
    assert boundary["relative_path_source_isolation"] is True
    assert boundary["operating_system_sandbox"] is False
    assert boundary["absolute_path_confinement"] is False
    assert boundary["raw_socket_confinement"] is False


def test_qa_gate_rejects_malformed_duplicate_and_oversized_spec(tmp_path: Path):
    repo = _repo(tmp_path)
    cfg = _gate(tmp_path, _spec(repo))
    assert run_gate(cfg, repo, b'{"schema":"QaGateSpecV1","schema":"x"}').exit_code == 2
    assert run_gate(cfg, repo, b"{").exit_code == 2
    assert run_gate(cfg, repo, b"\xff").exit_code == 2
    assert run_gate(cfg, repo, b'{"schema":NaN}').exit_code == 2
    assert run_gate(cfg, repo, b"{" + b"x" * (1024 * 1024 + 1) + b"}").exit_code == 2


def test_qa_gate_rejects_symlinked_boolean_and_structurally_oversized_specs(tmp_path: Path):
    repo = _repo(tmp_path)
    spec = _spec(repo)
    cfg = _gate(tmp_path, spec)
    target = tmp_path / "spec-target.json"
    target.write_text(json.dumps(spec), encoding="utf-8")
    link = tmp_path / "spec-link.json"
    link.symlink_to(target)
    assert run_gate(cfg, repo, link).exit_code == 2

    invalid_bool = dict(spec)
    invalid_bool["schema_version"] = True
    assert run_gate(cfg, repo, invalid_bool).exit_code == 2

    too_many = dict(spec)
    too_many["criteria"] = {f"AC-{index}": ["unit"] for index in range(MAX_CRITERIA + 1)}
    assert run_gate(cfg, repo, too_many).exit_code == 2

    too_wide = dict(spec)
    too_wide["criteria"] = {"AC-1": [f"check{index}" for index in range(MAX_CHECKS_PER_CRITERION + 1)]}
    assert run_gate(cfg, repo, too_wide).exit_code == 2

    duplicate = dict(spec)
    duplicate["criteria"] = {"AC-1": ["unit", "unit"]}
    assert run_gate(cfg, repo, duplicate).exit_code == 2
    empty = dict(spec)
    empty["criteria"] = {"AC-1": []}
    assert run_gate(cfg, repo, empty).exit_code == 2


def test_qa_gate_cleans_partial_workspace_and_reports_cleanup_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _repo(tmp_path)
    partial_root = tmp_path / "partial-gate"
    real_mkdtemp = tempfile.mkdtemp

    def make_partial_root(*args, **kwargs):
        partial_root.mkdir(mode=0o700)
        return str(partial_root)

    original_mkdir = Path.mkdir

    def fail_tmp_directory(path: Path, *args, **kwargs):
        if path == partial_root / "tmp":
            raise OSError("injected mkdir failure")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(gate_module.tempfile, "mkdtemp", make_partial_root)
    monkeypatch.setattr(Path, "mkdir", fail_tmp_directory)
    outcome = run_gate(_gate(tmp_path, _spec(repo)), repo, _spec(repo))
    assert outcome.exit_code == 1
    assert not partial_root.exists()

    monkeypatch.setattr(Path, "mkdir", original_mkdir)
    monkeypatch.setattr(gate_module.tempfile, "mkdtemp", real_mkdtemp)
    leaked_roots: list[Path] = []
    real_rmtree = shutil.rmtree

    def leave_workspace(path: Path, *args, **kwargs):
        leaked_roots.append(Path(path))

    monkeypatch.setattr(gate_module.shutil, "rmtree", leave_workspace)
    outcome = run_gate(_gate(tmp_path, _spec(repo)), repo, _spec(repo))
    monkeypatch.setattr(gate_module.shutil, "rmtree", real_rmtree)
    for root in leaked_roots:
        real_rmtree(root, ignore_errors=True)
    assert outcome.exit_code == 1
    assert sum(row.check_id == "repository-state" for row in outcome.bundle.checks) == 1
    assert "hold_code=cleanup_failed" in outcome.bundle.checks[0].evidence_refs


def test_qa_gate_cli_needs_no_gh_model_or_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _repo(tmp_path)
    spec = _spec(repo)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    spec_path.chmod(0o600)
    cfg = _cli_config(tmp_path, [sys.executable, "-c", "import os; raise SystemExit(bool(os.getenv('GH_TOKEN')))" ])
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(Path(__file__).parents[1] / "src"), "GH_TOKEN": "private-marker"}
    proc = subprocess.run([sys.executable, "-m", "agent_ops", "qa", "verify", "--config", str(cfg), "--spec", str(spec_path), "--repository", str(repo)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["verdict"] == "PASS"


def _source_proof(repo: Path) -> tuple[str, str, str]:
    rows = []
    for raw_name in subprocess.run(
        ["git", "ls-files", "-z"], cwd=repo, check=True, capture_output=True
    ).stdout.split(b"\0"):
        if not raw_name:
            continue
        path = repo / os.fsdecode(raw_name)
        item = os.lstat(path)
        payload = os.fsencode(os.readlink(path)) if stat.S_ISLNK(item.st_mode) else path.read_bytes() if stat.S_ISREG(item.st_mode) else b""
        rows.append(
            raw_name
            + b"\0"
            + str(stat.S_IMODE(item.st_mode)).encode()
            + b"\0"
            + str(item.st_ino).encode()
            + b"\0"
            + hashlib.sha256(payload).digest()
        )
    root = os.lstat(repo)
    topology = f"{root.st_dev}:{root.st_ino}:{stat.S_IMODE(root.st_mode)}"
    return _git(repo, "rev-parse", "HEAD"), _git(repo, "status", "--porcelain=v1", "--untracked-files=all"), topology + ":" + hashlib.sha256(b"\n".join(rows)).hexdigest()


def test_qa_gate_real_local_canary(tmp_path: Path):
    expected_sha = "e0f13f95e7a5af4194aed67ac72175fd977b5507"
    source_text = os.environ.get("AGENT_OPS_REAL_CANARY_SOURCE")
    expected_repository = os.environ.get("AGENT_OPS_REAL_CANARY_REPOSITORY")
    if source_text is None or expected_repository is None:
        pytest.skip("set the real-canary source and repository for the exact external AC-8 run")
    privacy_markers = json.loads(os.environ["AGENT_OPS_REAL_CANARY_PRIVACY_MARKERS"])
    assert isinstance(privacy_markers, list) and privacy_markers
    assert all(isinstance(marker, str) and marker for marker in privacy_markers)
    source = Path(source_text)
    build_python = os.environ.get("AGENT_OPS_REAL_CANARY_BUILD_PYTHON", sys.executable)
    files_dir = Path(os.environ.get("AGENT_OPS_REAL_CANARY_FILES_DIR", str(tmp_path / "canary-files")))
    files_dir.mkdir(mode=0o700, exist_ok=True)
    files_dir.chmod(0o700)
    assert _git(source, "rev-parse", "HEAD") == expected_sha
    assert _git(source, "status", "--porcelain=v1", "--untracked-files=all") == ""

    unit_code = """
import subprocess, sys
result = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-o', 'addopts='], capture_output=True)
if result.returncode or b'170 passed' not in result.stdout + result.stderr:
    raise SystemExit(1)
print('verified_test_count=170')
"""
    package_code = """
import subprocess, sys
from pathlib import Path
result = subprocess.run([sys.executable, '-m', 'build', '--no-isolation', '--outdir', '.qa-dist'], capture_output=True)
artifacts = list(Path('.qa-dist').glob('*.whl')) + list(Path('.qa-dist').glob('*.tar.gz'))
raise SystemExit(0 if result.returncode == 0 and len(artifacts) == 2 else 1)
"""
    privacy_code = """
import subprocess, sys
diff = subprocess.run(['git', 'diff', '--no-ext-diff', '--binary', 'HEAD^', 'HEAD'], check=True, capture_output=True).stdout
markers = [marker.encode() for marker in sys.argv[1:]]
raise SystemExit(1 if any(marker in diff for marker in markers) else 0)
"""
    commands = {
        "unit": [sys.executable, "-c", "exec(" + repr(unit_code) + ")"],
        "package": [build_python, "-c", "exec(" + repr(package_code) + ")"],
        "privacy": [sys.executable, "-c", "exec(" + repr(privacy_code) + ")", *privacy_markers],
        "diff": ["git", "diff", "--check", "HEAD^", "HEAD"],
    }
    spec = {
        "schema": "QaGateSpecV1",
        "schema_version": 1,
        "repository": expected_repository,
        "base_sha": expected_sha,
        "criteria": {
            "AC-diff": ["diff"],
            "AC-package": ["package"],
            "AC-privacy": ["privacy"],
            "AC-unit": ["unit"],
        },
    }
    noop = [sys.executable, "-c", "raise SystemExit(0)"]
    config_data = {
        "operator_logins": ["operator"],
        "owned_namespaces": [expected_repository.split("/", 1)[0]],
        "excluded_repositories": [],
        "trusted_reviewer_logins": ["reviewer"],
        "workspace_root": str(files_dir / "workspace"),
        "state_dir": str(files_dir / "state"),
        "protected_path_patterns": [],
        "classifier_command": noop + ["{request_path}", "{response_path}"],
        "builder_command": noop + ["{request_path}", "{response_path}", "{worktree_path}"],
        "reviewer_command": noop + ["{request_path}", "{response_path}", "{worktree_path}"],
        "required_runner_identity": {
            "profile": "p", "provider": "p", "model": "m", "reasoning_effort": "low", "service_tier": "standard"
        },
        "capability_isolation": {
            "enabled": True, "environment_allowlist": ["PATH"], "private_markers": ["private-marker"]
        },
        "notification_mode": "quiet",
        "default_verification_commands": {},
        "gh_command": str(files_dir / "absent-gh"),
        "git_command": "git",
        "repository_policies": {
            expected_repository: {"permitted_paths": [], "verification_commands": commands}
        },
    }
    config_path = files_dir / "config.json"
    spec_path = files_dir / "spec.json"
    config_path.write_text(json.dumps(config_data), encoding="utf-8")
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    config_path.chmod(0o600)
    spec_path.chmod(0o600)
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(spec_path.stat().st_mode) == 0o600
    cfg = load_config(config_path)
    before = _source_proof(source)
    outcome = run_gate(cfg, source, spec_path)
    after = _source_proof(source)
    assert outcome.exit_code == 0, outcome.render()
    assert outcome.bundle.verdict == "PASS"
    assert [row.check_id for row in outcome.bundle.checks] == [
        "repository-state", "diff", "package", "privacy", "unit"
    ]
    assert before == after
    if os.environ.get("AGENT_OPS_REAL_CANARY_PRINT_RECEIPT") == "1":
        print(outcome.render(), end="")
