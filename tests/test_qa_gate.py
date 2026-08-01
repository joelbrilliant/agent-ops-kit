"""Production-path tests for the isolated repository QA release gate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_ops.config import RepoPolicy
from agent_ops.qa.gate import run_gate

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
    return path


def test_qa_gate_pass_maps_every_criterion_once(tmp_path: Path):
    repo = _repo(tmp_path)
    spec = _spec(repo)
    outcome = run_gate(_gate(tmp_path, spec), repo, spec)
    assert outcome.exit_code == 0
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
    outcome = run_gate(_gate(tmp_path, spec), repo, spec)
    assert outcome.exit_code == 2
    assert outcome.bundle.verdict == "HOLD"


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
    before = (repo / "demo.txt").read_text(encoding="utf-8")
    command = [sys.executable, "-c", "from pathlib import Path; Path('demo.txt').write_text('bad')"]
    outcome = run_gate(_gate(tmp_path, _spec(repo), command), repo, _spec(repo))
    assert outcome.exit_code == 1
    assert (repo / "demo.txt").read_text(encoding="utf-8") == before


@pytest.mark.parametrize("command", [["sh", "-c", "true"], ["python\n", "-c", "pass"], ["env", "python", "-c", "pass"]])
def test_qa_gate_rejects_shell_wrappers_and_control_characters(tmp_path: Path, command: list[str]):
    repo = _repo(tmp_path)
    assert run_gate(_gate(tmp_path, _spec(repo), command), repo, _spec(repo)).exit_code == 2


def test_qa_gate_strips_credentials_and_github_capability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("GH_TOKEN", "private-marker")
    command = [sys.executable, "-c", "import os; raise SystemExit(bool(os.getenv('GH_TOKEN') or os.getenv('SSH_AUTH_SOCK')))" ]
    assert run_gate(_gate(tmp_path, _spec(repo), command), repo, _spec(repo)).exit_code == 0


@pytest.mark.parametrize("body", ["Path('demo.txt').write_text('x')", "subprocess.run(['git','config','x.y','z'], check=True)", "Path('.git/hooks/x').write_text('x')", "subprocess.run(['git','-c','user.email=t@example.invalid','-c','user.name=t','commit','--allow-empty','-m','x'], check=True)"])
def test_qa_gate_holds_on_clone_file_config_hook_and_history_mutation(tmp_path: Path, body: str):
    repo = _repo(tmp_path)
    code = "from pathlib import Path; import subprocess; " + body
    command = [sys.executable, "-c", code]
    assert run_gate(_gate(tmp_path, _spec(repo), command), repo, _spec(repo)).exit_code == 1


def test_qa_gate_bundle_is_deterministic_and_private(tmp_path: Path):
    repo = _repo(tmp_path)
    spec = _spec(repo)
    cfg = _gate(tmp_path, spec)
    one = run_gate(cfg, repo, spec).render()
    two = run_gate(cfg, repo, spec).render()
    assert one == two
    assert str(repo) not in one and "private-marker" not in one and "-c" not in one


def test_qa_gate_rejects_malformed_duplicate_and_oversized_spec(tmp_path: Path):
    repo = _repo(tmp_path)
    cfg = _gate(tmp_path, _spec(repo))
    assert run_gate(cfg, repo, b'{"schema":"QaGateSpecV1","schema":"x"}').exit_code == 2
    assert run_gate(cfg, repo, b"{" + b"x" * (1024 * 1024 + 1) + b"}").exit_code == 2


def test_qa_gate_cli_needs_no_gh_model_or_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _repo(tmp_path)
    spec = _spec(repo)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    cfg = _cli_config(tmp_path, [sys.executable, "-c", "import os; raise SystemExit(bool(os.getenv('GH_TOKEN')))" ])
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(Path(__file__).parents[1] / "src"), "GH_TOKEN": "private-marker"}
    proc = subprocess.run([sys.executable, "-m", "agent_ops", "qa", "verify", "--config", str(cfg), "--spec", str(spec_path), "--repository", str(repo)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["verdict"] == "PASS"


def test_qa_gate_real_local_canary(tmp_path: Path):
    repo = _repo(tmp_path)
    spec = _spec(repo)
    assert run_gate(_gate(tmp_path, spec), repo, spec).bundle.base_sha == _git(repo, "rev-parse", "HEAD")
