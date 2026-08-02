from __future__ import annotations

import os
import plistlib
import shutil
import stat
import subprocess
import sys
from importlib import resources
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest

from github_watch.config import load_config


ROOT = Path(__file__).parents[1]


def test_launch_agent_template_is_three_hourly_and_requires_runtime_pause():
    with (ROOT / "templates" / "com.joelbrilliant.github-watch.plist").open("rb") as handle:
        template = plistlib.load(handle)

    assert template["Label"] == "com.joelbrilliant.github-watch"
    assert "Disabled" not in template
    assert template["StartInterval"] == 10800
    assert template["RunAtLoad"] is False
    assert template["ProgramArguments"][0] == "/Users/openclaw/.local/bin/github-watch"
    assert template["ProgramArguments"][-1] == "run"


def test_config_template_validates_and_packaged_sandbox_denies_gui_execution():
    config = load_config(ROOT / "templates" / "config.json")
    sandbox = resources.files("github_watch").joinpath("templates/oscar-headless.sb").read_text(encoding="utf-8")

    assert config.batch_limit == 20
    assert config.max_notification_age_hours == 168
    assert "(deny process-exec (literal \"/usr/bin/open\"))" in sandbox
    assert "[.]app/Contents/MacOS" in sandbox
    readonly = resources.files("github_watch").joinpath("templates/oscar-readonly.sb").read_text(encoding="utf-8")
    assert "/Users/openclaw/.config/gh" in readonly
    assert "/usr/bin/security" in readonly


def test_oscar_wrapper_uses_the_supported_hermes_oneshot_contract():
    path = ROOT / "templates" / "oscar-oneshot"
    wrapper = path.read_text(encoding="utf-8")

    assert path.stat().st_mode & stat.S_IXUSR
    assert "HERMES_HOME=/Users/openclaw/.hermes/profiles/oscar" in wrapper
    assert 'exec /Users/openclaw/.local/bin/hermes --oneshot "$1"' in wrapper


def test_buzz_wrapper_uses_fixed_argv_minimal_env_and_rejects_unaccepted_results(tmp_path, monkeypatch):
    wrapper = ROOT / "templates" / "github-watch-buzz-notify"
    fake_buzz = tmp_path / "buzz"
    arguments = tmp_path / "arguments"
    credentials = tmp_path / "buzz.env"
    fake_buzz.write_text(
        "#!/bin/sh\nprintf '%s|%s|%s' \"$*\" \"${PARENT_SECRET-unset}\" \"$BUZZ_AUTH_TAG\" > \"$BUZZ_ARGUMENTS\"\nprintf '%s' \"$BUZZ_RESPONSE\"\n",
        encoding="utf-8",
    )
    fake_buzz.chmod(0o755)
    loader = SourceFileLoader("github_watch_buzz_template", str(wrapper))
    spec = spec_from_loader(loader.name, loader)
    assert spec is not None
    module = module_from_spec(spec)
    loader.exec_module(module)
    module.ENV_PATH = credentials
    module.DEFAULT_BUZZ = str(fake_buzz)
    monkeypatch.setenv("PARENT_SECRET", "must-not-leak")
    monkeypatch.setattr(sys, "argv", [str(wrapper), "safe message", "00000000-0000-0000-0000-000000000000"])
    base = (
        f"BUZZ_ARGUMENTS={arguments}\n"
        "BUZZ_AUTH_TAG='[\"auth\",\"tag\"]'\n"
        "BUZZ_CLI_PATH=/usr/bin/open\n"
    )
    credentials.write_text(base + 'BUZZ_RESPONSE={"accepted":true}\n', encoding="utf-8")
    accepted = module.main()
    credentials.write_text(base + 'BUZZ_RESPONSE={"accepted":false}\n', encoding="utf-8")
    rejected = module.main()

    assert accepted == 0
    assert rejected != 0
    assert arguments.read_text(encoding="utf-8") == 'messages send --channel 00000000-0000-0000-0000-000000000000 --content safe message|unset|["auth","tag"]'


@pytest.mark.parametrize("profile", ["oscar-headless.sb", "oscar-readonly.sb"])
def test_real_sandbox_denies_harmless_app_bundle_executable(tmp_path, profile):
    executable = tmp_path / "Harmless.app" / "Contents" / "MacOS" / "fake-gui"
    executable.parent.mkdir(parents=True)
    shutil.copyfile("/usr/bin/true", executable)
    executable.chmod(0o755)
    sandbox = resources.files("github_watch").joinpath(f"templates/{profile}")

    result = subprocess.run(
        ["/usr/bin/sandbox-exec", "-f", str(sandbox), "/bin/sh", "-c", 'exec "$1"', "sandbox-probe", str(executable)],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0


def test_reviewed_oscar_profile_contracts_replace_agent_ops_pipeline():
    oscar = ROOT / "templates" / "oscar"
    agents = (oscar / "AGENTS.md").read_text(encoding="utf-8")

    assert {path.name for path in oscar.iterdir()} == {"SOUL.md", "AGENTS.md", "profile.yaml"}
    assert "one routine end-to-end repair" in agents
    assert "Mutation-allowed jobs" in agents
    assert "separate review" not in agents
