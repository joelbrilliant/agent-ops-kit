from __future__ import annotations

import os
import plistlib
import stat
import subprocess
from importlib import resources
from pathlib import Path

from github_watch.config import load_config


ROOT = Path(__file__).parents[1]


def test_paused_launch_agent_template_is_three_hourly_and_versioned():
    with (ROOT / "templates" / "com.joelbrilliant.github-watch.plist").open("rb") as handle:
        template = plistlib.load(handle)

    assert template["Label"] == "com.joelbrilliant.github-watch"
    assert template["Disabled"] is True
    assert template["StartInterval"] == 10800
    assert template["ProgramArguments"][-1] == "run"


def test_config_template_validates_and_packaged_sandbox_denies_gui_execution():
    config = load_config(ROOT / "templates" / "config.json")
    sandbox = resources.files("github_watch").joinpath("templates/oscar-headless.sb").read_text(encoding="utf-8")

    assert config.batch_limit == 20
    assert "(deny process-exec (literal \"/usr/bin/open\"))" in sandbox
    assert ".app/Contents/MacOS" in sandbox


def test_oscar_wrapper_uses_the_supported_hermes_oneshot_contract():
    path = ROOT / "templates" / "oscar-oneshot"
    wrapper = path.read_text(encoding="utf-8")

    assert path.stat().st_mode & stat.S_IXUSR
    assert "HERMES_HOME=/Users/openclaw/.hermes/profiles/oscar" in wrapper
    assert 'exec hermes --oneshot "$1"' in wrapper


def test_buzz_wrapper_uses_real_argv_and_rejects_unaccepted_results(tmp_path):
    wrapper = ROOT / "templates" / "github-watch-buzz-notify"
    fake_buzz = tmp_path / "buzz"
    arguments = tmp_path / "arguments"
    credentials = tmp_path / "buzz.env"
    fake_buzz.write_text(
        "#!/bin/sh\nprintf '%s|%s' \"$*\" \"${NOT_BUZZ-unset}\" > \"$BUZZ_ARGUMENTS\"\nprintf '%s' \"$BUZZ_RESPONSE\"\n",
        encoding="utf-8",
    )
    fake_buzz.chmod(0o755)
    credentials.write_text("BUZZ_TOKEN=only-this-is-loaded\nNOT_BUZZ=must-not-leak\n", encoding="utf-8")
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "BUZZ_ENV_FILE": str(credentials),
        "BUZZ_ARGUMENTS": str(arguments),
    }
    command = [str(wrapper), "safe message", "00000000-0000-0000-0000-000000000000"]

    accepted = subprocess.run(command, env={**environment, "BUZZ_RESPONSE": '{"accepted":true}'}, capture_output=True, text=True)
    rejected = subprocess.run(command, env={**environment, "BUZZ_RESPONSE": '{"accepted":false}'}, capture_output=True, text=True)

    assert accepted.returncode == 0
    assert rejected.returncode != 0
    assert arguments.read_text(encoding="utf-8") == "messages send --channel 00000000-0000-0000-0000-000000000000 --content safe message|unset"
