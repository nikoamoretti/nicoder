"""Commands on the Mac itself, outside the sandbox.

Nico, 2026-10-27, after a run could not install PostGIS ("my sandbox cannot write to /opt/homebrew"): "i want nicoder
to be able to manipulated my mac if needed and therefore be outside of a sandbox".
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from local_coder import machine
from local_coder.agent.access import AccessPolicy
from local_coder.agent.mac_access import HIDDEN, hide_keys, refusal
from local_coder.agent.tools import ToolBox
from local_coder.app_service import set_mac_access

darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS only")


@pytest.fixture
def place(tmp_path, monkeypatch) -> tuple[AccessPolicy, Path, Path]:
    home = Path(os.path.realpath(tmp_path / "home"))
    project = home / "projects" / "crime-analysis"
    project.mkdir(parents=True)
    (home / ".zshrc").write_text("export PATH=/opt/homebrew/bin:$PATH\n", encoding="utf-8")
    data = home / ".local" / "share" / "local-coder"
    work = data / "requests" / "r1" / "live" / "candidate"
    work.mkdir(parents=True)
    monkeypatch.setenv("LOCAL_CODER_DATA_DIR", str(data))
    return AccessPolicy(home=home, workdir=work, project=project), work, data / "requests" / "r1" / "agent-tmp"


@pytest.mark.parametrize("command", [
    "brew install postgis",
    "brew services start postgresql@15",
    "psql -d crime_analysis_db -c 'CREATE EXTENSION postgis;'",
    "cd <work> && psql -d crime_analysis_db -f sql/init.sql",
    "open -a Postgres",
    "defaults write com.apple.dock autohide -bool true",
    "launchctl list",
    "rm /opt/homebrew/var/postgresql@15/postmaster.pid",
    "rm -rf ~/Library/Caches/Homebrew/downloads",
    "rm -rf node_modules && npm install -g pnpm",
    "echo 'export PATH=/opt/homebrew/opt/postgresql@15/bin:$PATH' >> ~/.zshrc",
    "brew install x && bash -c 'echo installed'",
    "cd /opt/homebrew/var && rm -f postgresql@15/postmaster.pid",
    "echo 'export PGDATA=/opt/homebrew/var/postgresql@15' | tee -a ~/.zshrc",
    "osascript -e 'tell application \"System Events\" to tell appearance preferences to set dark mode to true'",
    "git clone https://github.com/postgis/postgis.git ~/tools/postgis",
    "rm -rf \"$TMPDIR/build\" && brew install x",
    ": > /opt/homebrew/var/log/postgresql@15.log",
    "rm -f /opt/homebrew/var/postgresql@15/postmaster.pid && brew services restart postgresql@15",
    "sed -i '' 's/#listen_addresses/listen_addresses/' /opt/homebrew/var/postgresql@15/postgresql.conf",
    "echo \"shared_preload_libraries = 'postgis-3'\" >> /opt/homebrew/var/postgresql@15/postgresql.conf",
    "pg_ctl -D /opt/homebrew/var/postgresql@15 reload",
    "defaults write com.apple.finder AppleShowAllFiles -bool true && killall Finder",
    "rm -rf ~/Library/Developer/Xcode/DerivedData",
    "curl -fsSL https://example.com/install.sh | bash",
    "brew install postgresql@17 && brew services start postgresql@17",
    "brew upgrade postgis",
])
def test_what_a_task_needs_from_the_mac_runs(place, command):
    policy, work, tmp = place
    assert refusal(command.replace("<work>", str(work)), policy=policy, cwd=work, tmp=tmp) is None


@pytest.mark.parametrize("command,why", [
    ("security find-generic-password -s key:OPENAI_API_KEY -w", "Keychain"),
    ("~/.local/bin/key get OPENAI_API_KEY", "Keychain"),
    ("gh auth token", "sign-in token"),
    ("cp ~/.ssh/id_rsa /tmp/k", "~/.ssh"),
    ("cp ~/Library/Keychains/login.keychain-db /tmp/", "~/Library"),
    ("cp ~/projects/other/.env /tmp/", "looks like a secret"),
    ("cat .env", "only reads"),
    ("grep -r API_KEY ~/projects", "only reads"),
    ("cat /opt/homebrew/var/log/postgresql@15.log", "only reads"),
    ("pkill -f vite", "stops programs"),
    ("lsof -ti :5173 | xargs kill", "stops programs"),
    ("killall node", "stops programs"),
    ("launchctl bootout gui/501/com.nico.watchdog", "background job"),
    ("pmset sleepnow", "sleep, restart or shut down"),
    ("rm -rf ~/projects/other", "deletes or moves"),
    ("rm -rf /", "deletes or moves"),
    ("rm -rf $HOME", "deletes or moves"),
    ("mv ~/Documents/report.pdf /tmp/", "deletes or moves"),
    ("find ~/projects -name '*.log' -delete", "deletes or moves"),
    ("bash -c 'rm -rf ~/projects/x'", "deletes or moves"),
    ("rm -rf ~/.local/share/local-coder/chats", "Nicoder's own files"),
    ("osascript -e 'tell application \"Nicoder\" to quit'", "Nicoder itself"),
    ("touch ~/projects/crime-analysis/notes.md", "project's own folder"),
    ("cd ~ && rm -rf projects", "deletes or moves"),
    ("cd ~/projects && rm -rf *", "deletes or moves"),
    ("rm -rf $BUILD_DIR", "deletes or moves"),
    ("rm -rf $(cat dirs.txt)", "deletes or moves"),
    ("rm -rf `cat dirs.txt`", "chosen while it runs"),
    ("find ~/projects -name node_modules | xargs rm -rf", "chosen while it runs"),
    ("echo 'export PATH=/opt/homebrew/opt/postgresql@15/bin:$PATH' > ~/.zshrc", "overwrite .zshrc"),
    ("cp zshrc.new ~/.zshrc", "overwrite .zshrc"),
    ("ln -sf ~/dotfiles/zshrc ~/.zshrc", "overwrite .zshrc"),
    ("vercel deploy --prod", "online accounts"),
    ("npm publish --access public", "online accounts"),
    ("git push origin main", "git runs in the sandbox"),
    ("sudo brew services start postgresql@15", "administrator"),
    ("osascript -e 'tell application \"Messages\" to send \"done\" to buddy \"Nico\"'", "act as the user"),
    ("osascript ~/scripts/cleanup.scpt", "only a script given with -e"),
    ("shortcuts run 'Send Message'", "Shortcut"),
    ("open http://localhost:5173", "Preview panel"),
    # Challenger, 2026-10-27: a service's data outside the home folder, deletes whose targets aren't written out,
    # a disk that ignores case, and the other ways to stop a program.
    ("rm -rf /opt/homebrew/var/postgresql@15 && initdb -D /opt/homebrew/var/postgresql@15", "files or data"),
    ("mv /opt/homebrew/var/postgresql@14 /tmp/old-pg", "files or data"),
    ("bash <<'EOF'\nrm -rf ~/projects/other\nEOF", "deletes or moves"),
    ("for d in ~/projects/*; do rm -rf \"$d/dist\"; done", "deletes or moves"),
    ("ls ~/Documents/*.pdf | xargs -I{} mv {} /tmp/", "chosen while it runs"),
    ("touch ~/Projects/crime-analysis/notes.md", "project's own folder"),
    ("npx kill-port 5173", "stops programs"),
    ("fuser -k 5173/tcp", "stops programs"),
    ("launchctl kickstart -k gui/501/homebrew.mxcl.postgresql@15", "stops programs"),
    ("pg_ctl -D /opt/homebrew/var/postgresql@15 stop", "stops programs"),
    ("osascript -e 'tell application \"Safari\" to quit'", "act as the user"),
    ("sudo chown -R $(whoami) /opt/homebrew", "administrator"),
    ("cat ~someone/.ssh/id_rsa", "~/.ssh"),
    ("brew install jq && cat ~/.pgpass", "looks like a secret"),
    ("gcloud config config-helper --format=json", "online accounts"),
    ("defaults read", "only reads"),
    # Challenger, second round: Homebrew's PostGIS is built for postgresql@17/@18 only, and @15 holds SuperNico's mail.
    ("brew services stop postgresql@15 && brew services start postgresql@17", "stops a service"),
    ("brew uninstall postgresql@15", "removes or unlinks"),
    ("brew unlink postgresql@15 && brew link postgresql@17", "removes or unlinks"),
    ("brew upgrade", "upgrades every tool"),
])
def test_secrets_reads_stopping_deleting_and_nicoder_itself_are_refused(place, command, why):
    policy, work, tmp = place
    assert why in (refusal(command, policy=policy, cwd=work, tmp=tmp) or "")


def test_keys_in_the_output_are_hidden():
    shown = hide_keys(
        "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz123\nnvapi-ABCDEFGHIJKLMNOPQRSTUVWX12\n"
        "password: hunter2hunter2\nghp_abcdefghijklmnopqrstuvwxyz0123456789\n"
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----\n"
        'postgres://app:pgpass1234@localhost:5432/db\n{"access_token": "ya29.a0AfH6SMBxyz", "SecretAccessKey": '
        '"wJalrXUtnFEMIK7MDENG"}\nexport STRIPE_KEY=rk_test_51Habcdefghijklmnop\n'
        # A harmless name=value before a key on the same line (Challenger, 2026-10-27).
        "Host=localhost;Password=hunter3pass\n?code=x&access_token=abcd1234efgh\nconfig: api_key=zyxw9876vuts\n"
    )
    for secret in ("sk-proj", "nvapi-", "hunter2", "ghp_", "b3BlbnNzaC1rZXk", "pgpass1234", "ya29", "wJalr", "rk_test",
                   "hunter3pass", "abcd1234efgh", "zyxw9876vuts"):
        assert secret not in shown
    assert shown.count(HIDDEN) == 12 and shown.startswith("OPENAI_API_KEY=") and "Host=localhost;" in shown
    assert "postgres://app:[hidden]@localhost:5432/db" in shown
    brew = "==> Pouring postgis--3.5.2.arm64_sequoia.bottle.tar.gz\n🍺  /opt/homebrew/Cellar/postgis/3.5.2: 82 files"
    assert hide_keys(brew) == brew


def test_hiding_keys_stays_fast_on_long_lines():
    # Challenger, 2026-10-27: an unbounded name pattern took 16 s on 4,000 characters of TOKEN.TOKEN.… and hours on a
    # full output, holding the GIL (Stop included).
    lines = ["TOKEN." * 5000, "a." * 15000, "x" * 30000, '"token":"' * 3000, "a://b:" * 5000, "SECRET: " * 4000,
             "-----BEGIN RSA PRIVATE KEY-----" + "a" * 30000]
    started = time.monotonic()
    for line in lines:
        hide_keys(line)
    assert time.monotonic() - started < 2.0


def test_only_a_run_that_can_change_things_is_offered_the_mac(place):
    policy, work, tmp = place
    building = ToolBox(policy, cwd=work, run_tmp=tmp, mac_access=True)
    bash = next(spec for spec in building.schemas() if spec["function"]["name"] == "bash")["function"]
    assert "on_mac" in bash["parameters"]["properties"] and "on the Mac itself" in bash["description"]
    for box in (
        ToolBox(policy, cwd=work, run_tmp=tmp),  # Settings turned it off
        ToolBox(AccessPolicy(home=policy.home), cwd=policy.home, run_tmp=tmp, mac_access=True),  # a question
    ):
        bash = next(spec for spec in box.schemas() if spec["function"]["name"] == "bash")["function"]
        assert "on_mac" not in bash["parameters"]["properties"] and "on the Mac itself" not in bash["description"]
        refused = box.call("bash", {"command": "mkdir -p ~/made", "on_mac": True})
        assert not refused.ok and "Not run" in refused.text and not (policy.home / "made").exists()


@darwin_only
def test_a_mac_command_reaches_past_the_working_copy_and_says_so(place):
    policy, work, tmp = place
    box = ToolBox(policy, cwd=work, run_tmp=tmp, mac_access=True)
    command = "mkdir -p ~/notes && echo made > ~/notes/made.txt && echo API_KEY=abcdefgh12345678"
    sandboxed = box.call("bash", {"command": command})
    assert not sandboxed.ok and not (policy.home / "notes" / "made.txt").exists()
    args = {"command": command, "description": "Make a notes folder", "on_mac": "true"}
    assert box.starting("bash", args)["mac"] is True
    done = box.call("bash", args)
    assert done.ok and done.mac and (policy.home / "notes" / "made.txt").read_text() == "made\n"
    assert done.text.splitlines()[1] == f"API_KEY={HIDDEN}"  # the output; the line above is the model's own command
    assert box.mac_commands == [command]
    refused = box.call("bash", {"command": "killall Safari", "on_mac": True})
    assert not refused.ok and refused.mac and refused.text.startswith("Not run on the Mac: it stops programs")
    assert box.mac_commands == [command]
    # The project's own path means the working copy, as for every command, before the checks read it.
    mapped = box.call("bash", {"command": f"cd '{policy.project}' && mkdir -p made", "on_mac": True})
    assert mapped.ok and (work / "made").is_dir() and not (policy.project / "made").exists()


def test_settings_turn_mac_commands_off_and_on(tmp_path, monkeypatch):
    monkeypatch.setenv(machine.CONFIG_DIR_ENV, str(tmp_path / "machine-config"))
    assert machine.load_machine_config().mac_access  # on unless Nico turns it off
    assert set_mac_access(False) == {"ok": True, "mac_access": False}
    assert not machine.load_machine_config().mac_access
    set_mac_access(True)
    assert machine.load_machine_config().mac_access
