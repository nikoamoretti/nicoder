"""Commands that run on the Mac itself, outside the sandbox.

Nico, 2026-10-27, after a run could not install PostGIS ("my sandbox cannot write to /opt/homebrew"): "i want nicoder
to be able to manipulated my mac if needed and therefore be outside of a sandbox". In a change request the model marks
such a command with bash's `on_mac`: installing a tool (brew install), starting or setting up a service or a database,
changing a setting, opening an app. It runs as the user with no sandbox at all: macOS refuses a sandbox inside a
restricted one, and Homebrew, SwiftPM and Xcode start their own. So `refusal` reads the command before it runs and
`hide_keys` reads its output before the model does. A Mac command never:
- reaches secrets: the Keychain, Nico's `key` tool, a command that prints a sign-in token, or a path the file tools
  refuse;
- acts on his online accounts: deploying, publishing, pushing (his signed-in tools work outside the sandbox);
- only reads or searches: that runs in the sandbox, where secret files stay unreadable (here a search would read them);
- runs as administrator, or acts as him in other apps: AppleScript that sends messages, types, quits or deletes, and
  Shortcuts or Automator workflows;
- stops a program, which may be one of his (a service restarts with `brew services`), or sleeps or restarts the Mac;
- deletes, moves away or overwrites his files: in the home folder outside the working copy, temp folders and caches,
  and the databases Homebrew services keep in /opt/homebrew/var (appending to ~/.zshrc or a .conf is fine);
- touches Nicoder itself: its data (chats, other requests), its app, or the project's own folder, which changes only
  through Keep.
These read the command's words (following `cd`, `bash -c` and a heredoc fed to a shell), so they catch a model's
slips, not a script written to get around them (`python -c`, a script piped from the network).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .access import ALLOWED_INSIDE_DENIED, DENIED_DIR_NAMES, DENIED_HOME_DIRS, AccessPolicy, secret_name
from .command_words import _FINDS, _LISTS, _READS, _unwrap
from .shell_paths import _REDIRECTS, _SEARCHES, _SHELLS, _tokens, _unquote

# ~/Library folders a Mac command may name: caches, logs, launch agents, Xcode's and SwiftPM's data, fonts.
LIBRARY_OPEN = (
    "Library/Caches", "Library/Logs", "Library/LaunchAgents", "Library/Developer", "Library/Fonts",
    "Library/org.swift.swiftpm",
)
# Home folders where deleting is housekeeping, not the user's files.
SCRATCH = (".cache", ".npm", "Library/Caches", "Library/Logs", "Library/Developer/Xcode/DerivedData")
# Where Homebrew services keep their data (Postgres, MongoDB, Redis …): deleting or overwriting there loses it, except
# a stale .pid or .lock, a log, or Homebrew's own locks.
DATA_ROOTS = (Path("/opt/homebrew/var"), Path("/usr/local/var"))
SECRET_PROGRAMS = frozenset({"security", "key", "op", "pass", "gpg", "gpg2", "ssh-add"})
# (program, subcommand) pairs that print a sign-in token or a stored password.
TOKEN_COMMANDS = frozenset({
    ("gh", "auth"), ("glab", "auth"), ("gcloud", "auth"), ("fly", "auth"), ("flyctl", "auth"), ("git", "credential"),
    ("npm", "token"), ("aws", "configure"), ("heroku", "auth:token"),
})
# Tools that act on his online accounts, and the subcommands of others that publish.
ACCOUNT_PROGRAMS = frozenset({
    "gh", "glab", "vercel", "netlify", "fly", "flyctl", "heroku", "railway", "firebase", "wrangler", "doctl", "az",
    "aws", "gcloud", "asc", "fastlane", "eas", "twine",
})
ACCOUNT_COMMANDS = frozenset({
    ("npm", "publish"), ("npm", "unpublish"), ("npm", "deprecate"), ("npm", "owner"), ("npm", "access"),
    ("npm", "login"), ("npm", "adduser"), ("pnpm", "publish"), ("yarn", "publish"), ("yarn", "npm"),
    ("bun", "publish"), ("cargo", "publish"), ("cargo", "login"), ("cargo", "yank"), ("gem", "push"),
    ("gem", "signin"), ("pod", "trunk"), ("docker", "push"), ("docker", "login"), ("xcrun", "altool"),
    ("xcrun", "notarytool"),
})
# Homebrew may add, start and restart, never take away what is there: his own apps use these services (SuperNico's
# mail store is postgresql@15) and tools. A task that needs another version runs it beside the old one (Challenger,
# 2026-10-27: Homebrew's PostGIS is built for postgresql@17/@18 only, so the PostGIS task would stop @15).
BREW_REMOVES = frozenset({"uninstall", "remove", "rm", "unlink", "untap"})
SERVICE_STOPS = frozenset({"stop", "kill"})
# git on the Mac only for what the sandbox can't do: cloning a tool somewhere, global settings.
GIT_ON_MAC = frozenset({"clone", "config", "version", "lfs"})
# Programs stop others by name (kill, pkill, killall, kill-port …); these system ones relaunch at once, and stopping
# them is how a Finder or Dock setting takes effect.
RELAUNCHING = frozenset({"Finder", "Dock", "SystemUIServer", "ControlCenter", "NotificationCenter", "cfprefsd"})
POWER = frozenset({"shutdown", "reboot", "halt", "pmset"})
LAUNCHCTL_STOPS = frozenset({"kill", "stop", "remove", "bootout", "unload", "disable", "reboot"})
DELETERS = frozenset({"rm", "rmdir", "unlink", "srm", "trash", "shred"})
ADMIN = frozenset({"sudo", "doas"})
# Commands that only set up the shell, and commands that only read or search.
NEUTRAL = frozenset({"cd", "pushd", "popd", "export", "set", "unset", "source", ".", "true", "clear", "for", "while",
                     "until", "if", "case", "esac", "fi", "done", "function"})
READ_ONLY = _READS | _LISTS | _FINDS | _SEARCHES | frozenset({
    "stat", "file", "readlink", "realpath", "cmp", "diff", "md5", "shasum", "sha256sum", "od", "hexdump", "echo",
    "printf", "pwd", "sleep", "printenv", "which", "type", "whoami", "id", "date",
})
_FIND_ACTIONS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-x", "-X", "--exec", "--exec-batch"})
_XARGS_VALUES = frozenset({"-I", "-J", "-L", "-n", "-P", "-R", "-S", "-s", "-E"})
_WRITES = frozenset({">", ">>", "&>", "&>>", ">|"})
_TRUNCATES = frozenset({">", "&>", ">|"})
# AppleScript that acts as the user in other apps: messaging apps, typing, quitting or deleting, a shell around the
# checks, power.
_SCRIPT_RISKS = re.compile(
    r"(?i)\b(messages|mail|outlook|slack|whatsapp|telegram|signal|discord|teams|keystroke|key code|"
    r"do shell script|quit|delete|empty|shut down|restart|log out|sleep)\b"
)
# The app or its engine by name (`open -a Nicoder`, `tell application "Nicoder"`, `python -m local_coder`).
_NICODER = re.compile(r"(?i)\bnicoder\b(?!\s+projects)|\blocal_coder\b")
# Key-shaped text: private keys, provider keys by their prefix, JWTs, a password in a URL, and a value given to a
# *KEY / *TOKEN / *SECRET / *PASSWORD name (as NAME=value, NAME: value or "name": "value").
_KEYS = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
    r"|\b(?:sk|rk)-[A-Za-z0-9_-]{20,}"
    r"|\bnvapi-[A-Za-z0-9_-]{20,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}"
    r"|\bglpat-[A-Za-z0-9_-]{20,}"
    r"|\bxox[abposr]-[A-Za-z0-9-]{10,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bAIza[0-9A-Za-z_-]{35}"
    r"|\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
)
# Every repeat is bounded: an unbounded name pattern backtracked for hours on a long line of TOKEN.TOKEN.…
# (Challenger, 2026-10-27), and the regex holds the GIL, Stop included. The key word stays inside the name pattern,
# so a harmless `Host=…` never swallows the `;Password=…` after it.
_SECRET_NAME = re.compile(r"(?i)API[_-]?KEY|_KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL")
_GIVEN = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]{0,40}(?:API[_-]?KEY|_KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL)[A-Za-z0-9_.-]{0,40})"
    r"(\s{0,8}[=:]\s{0,8})([\"']?)([^\s\"']{8,})"
)
_QUOTED = re.compile(r"(\"([A-Za-z0-9_.-]{1,80})\"\s{0,8}:\s{0,8}\")([^\"]{8,})(\")")
_URL_PASSWORD = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,20}://[^:/?#\s@]{1,100}:)([^@\s/]{1,200})(@)")
HIDDEN = "[hidden]"


def refusal(command: str, *, policy: AccessPolicy, cwd: Path, tmp: Path) -> str | None:
    """Why `command` may not run on the Mac, in words for the model; None when it may."""
    reason, acts = _Check(policy, cwd, tmp).script(command)
    if reason is None and not acts:
        return ("it only reads, so it runs in the sandbox, where secret files stay unreadable: run it again without "
                "on_mac.")
    return reason


def hide_keys(text: str) -> str:
    """The output with anything shaped like a key or a password replaced, before the model (and its provider) sees
    it."""
    text = _KEYS.sub(HIDDEN, text)
    text = _URL_PASSWORD.sub(lambda match: f"{match.group(1)}{HIDDEN}{match.group(3)}", text)
    text = _QUOTED.sub(
        lambda match: f"{match.group(1)}{HIDDEN}{match.group(4)}" if _SECRET_NAME.search(match.group(2))
        else match.group(0), text)
    return _GIVEN.sub(lambda match: f"{match.group(1)}{match.group(2)}{match.group(3)}{HIDDEN}", text)


def _under(path: Path, root: Path) -> bool:
    """`path` is `root` or inside it. A Mac disk ignores case: ~/Projects is ~/projects."""
    inner, outer = str(path).lower(), str(root).lower().rstrip("/")
    return inner == outer or inner.startswith(outer + "/")


def _simple(command: str) -> list[tuple[list[str], list[tuple[str, str]], list[str]]]:
    """Each simple command's words (unquoted, redirects left out), the files it writes as (operator, path), and the
    heredoc bodies it reads."""
    found: list[tuple[list[str], list[tuple[str, str]], list[str]]] = []
    owners: list[int] = []  # for each heredoc in order, the command that reads it
    bodies: list[str] = []
    words: list[str] = []
    writes: list[tuple[str, str]] = []
    redirect = ""
    end = -1
    for kind, start, stop in _tokens(command):
        text = command[start:stop]
        if kind == "body":
            bodies.append(text)
            continue
        if kind == "op" and text == "<<":
            owners.append(len(found))
        elif kind == "op" and (text in _REDIRECTS or text == "<<<"):
            if words and words[-1].isdigit() and end == start:  # the 2 of 2>&1
                words.pop()
            redirect = text
        elif kind == "op":
            if words or writes:
                found.append((words, writes, []))
            words, writes, redirect = [], [], ""
        elif redirect:
            target = _unquote(text)
            if redirect in _WRITES and not target.startswith("/dev/"):
                writes.append((redirect, target))
            redirect = ""
        else:
            words.append(_unquote(text))
        end = stop
    if words or writes:
        found.append((words, writes, []))
    for owner, body in zip(owners, bodies, strict=False):  # a heredoc left open has no body
        if owner < len(found):
            found[owner][2].append(body)
    return found


class _Check:
    def __init__(self, policy: AccessPolicy, cwd: Path, tmp: Path) -> None:
        from local_coder.engines.opencode_config import data_root
        from local_coder.machine import config_dir

        self.home = policy.home_real
        self.cwd = Path(os.path.realpath(cwd))
        self.project = policy.project_real
        self.own = [Path(os.path.realpath(tmp)), *([policy.workdir_real] if policy.workdir_real else [])]
        self.nicoder = [Path(os.path.realpath(path)) for path in (
            data_root(), config_dir(), self.home / ".local" / "share" / "local-coder-venv",
            self.home / "Applications" / "Nicoder.app", Path("/Applications/Nicoder.app"),
        )]

    def script(self, command: str) -> tuple[str | None, bool]:
        """Why the command may not run, and whether it does more than read."""
        text = command
        for folder in sorted({str(path) for path in self.own}, key=len, reverse=True):
            text = text.replace(folder, "")
        if _NICODER.search(text):
            return "it names Nicoder itself. A Mac command never touches Nicoder, its data or its app.", True
        acts = False
        for raw, writes, bodies in _simple(command):
            words = _unwrap(raw)
            if ADMIN & {os.path.basename(word) for word in raw[: len(raw) - len(words)]}:
                return "it runs as administrator, which needs the user's password. Nicoder never asks for it.", True
            if not words and not writes:
                continue
            reason, act = self._command(words, writes, bodies)
            if reason:
                return reason, True
            acts = acts or act
            if words and words[0] in {"cd", "pushd", "popd"}:
                self._move(words)
        return None, acts

    def _move(self, words: list[str]) -> None:
        """Follow `cd` so later relative paths resolve where they run; where a cd goes is unknown (`cd -`, popd), the
        home folder is assumed, the stricter place."""
        target = next((word for word in words[1:] if not word.startswith("-")), "~")
        place = self._resolve(target) if words[0] != "popd" and target != "-" else None
        self.cwd = place or self.home

    def _command(
        self, words: list[str], writes: list[tuple[str, str]], bodies: list[str] | None = None,
    ) -> tuple[str | None, bool]:
        """Why this simple command (or a lone redirect, `> file`) may not run, and whether it does more than read."""
        name, args = (os.path.basename(words[0]), words[1:]) if words else ("", [])
        flag = next((i for i, word in enumerate(args) if re.fullmatch(r"-[a-z]*c[a-z]*", word)), None)
        if name in _SHELLS and flag is not None and flag + 1 < len(args):
            return self.script(args[flag + 1])
        if name in _SHELLS and bodies:  # bash <<'EOF' … EOF: the heredoc is the script
            acts = False
            for body in bodies:
                reason, act = self.script(body)
                if reason:
                    return reason, True
                acts = acts or act
            return None, acts
        if name == "xargs":
            rest, skip = [], False
            for i, word in enumerate(args):
                if skip:
                    skip = False
                elif word.startswith("-"):
                    skip = word in _XARGS_VALUES
                else:
                    rest = _unwrap(args[i:])
                    break
            if rest and os.path.basename(rest[0]) in DELETERS | {"mv"}:
                return "it deletes or moves files chosen while it runs, and they may be the user's own.", True
            if rest:
                return self._command(rest, [])
        sub = next((word for word in args if not word.startswith("-")), "")
        reason = self._program(name, sub, args) if words else None
        if reason:
            return reason, True
        for word in self._paths(words, writes):
            path = self._resolve(word)
            if path is None or any(_under(path, own) for own in self.own):
                continue
            reason = self._place(path)
            if reason:
                return reason, True
        deleted = self._deleted(name, args)
        if name in DELETERS and not deleted:  # its files come from `...` or a pipe
            return "it deletes files chosen while it runs, and they may be the user's own.", True
        for word in deleted:
            path = self._resolve(word)
            if path is None or self._loses(path):
                return ("it deletes or moves the user's files or data outside the working copy. Nicoder never does "
                        "that on the Mac."), True
        reason = self._writes(writes) or self._overwritten(name, args)
        if reason:
            return reason, True
        reads = bool(words) and (name in NEUTRAL or name in READ_ONLY) and not writes and not deleted
        if name == "sed" and any(word.startswith("-i") for word in args):
            reads = False
        if name == "defaults" and sub in {"read", "read-type", "export", "find", "domains"}:
            reads = True
        return None, not reads

    def _program(self, name: str, sub: str, args: list[str]) -> str | None:
        """Why this program (with its subcommand) never runs on the Mac."""
        if name in SECRET_PROGRAMS:
            return "it reaches the Keychain or the user's keys, which stay private."
        if (name, sub) in TOKEN_COMMANDS:
            return "it would print a sign-in token, which stays private."
        if name in ACCOUNT_PROGRAMS or (name, sub) in ACCOUNT_COMMANDS:
            return ("it acts on the user's online accounts (deploying, publishing or pushing), which a task never does "
                    "from the Mac.")
        if name == "brew":
            words = [word for word in args if not word.startswith("-")]
            if sub in BREW_REMOVES:
                return ("it removes or unlinks a tool the user may rely on. Install what the task needs beside it and "
                        "call it by its full path (/opt/homebrew/opt/<name>/bin/…).")
            if sub == "services" and len(words) > 1 and words[1] in SERVICE_STOPS:
                return ("it stops a service the user's apps may use. Leave it running: if the task needs another "
                        "version, start that one beside it on another port (for example postgresql@17 on 5433).")
            if sub == "upgrade" and len(words) == 1:
                return "it upgrades every tool on the Mac. Upgrade only what the task needs: brew upgrade <name>."
        if name == "git" and sub not in GIT_ON_MAC:
            return ("git runs in the sandbox, where it can't push or change the user's other repositories: run it "
                    "again without on_mac.")
        relaunch = name == "killall" and all(word in RELAUNCHING for word in args if not word.startswith("-"))
        if ("kill" in name and not relaunch) or (name == "fuser" and any(
                word.startswith("-") and "k" in word for word in args)) or (
                name == "launchctl" and sub == "kickstart" and "-k" in args) or (
                name == "pg_ctl" and {"stop", "restart", "kill"} & set(args)):
            return ("it stops programs, and they may be the user's own. Restart a service with `brew services restart "
                    "<name>`.")
        if name in POWER:
            return "it would sleep, restart or shut down the Mac."
        if name == "launchctl" and sub in LAUNCHCTL_STOPS:
            return ("it stops a background job, which may be the user's own. Leave it running; a service the task "
                    "needs in another version starts beside it.")
        if name in {"shortcuts", "automator"}:
            return "it runs a Shortcut or a workflow, which can act as the user in other apps."
        if name == "osascript":
            script = " ".join(args[i + 1] for i, word in enumerate(args[:-1]) if word == "-e")
            if not script:
                return "only a script given with -e runs on the Mac, so Nicoder can read it first."
            if _SCRIPT_RISKS.search(script):
                return ("the script would act as the user in another app (send a message, type, quit, delete) or "
                        "around these checks.")
        if name == "open" and any("://" in word or word.startswith(("localhost", "127.0.0.1")) for word in args):
            return "opening a web page is Nicoder's job: its Preview panel shows the app."
        return None

    def _paths(self, words: list[str], writes: list[tuple[str, str]]) -> list[str]:
        """Every word that could be a path: the arguments (an option's value after =), the program when it is a path,
        and the files its redirects write."""
        found = [words[0]] if words and "/" in words[0] else []
        for word in words[1:]:
            if word.startswith("-"):
                if "=" in word:
                    found.append(word.split("=", 1)[1])
            else:
                found.append(word)
        return [*found, *(target for _, target in writes)]

    def _resolve(self, word: str) -> Path | None:
        """The path a word names, resolved where the command runs; None when that can't be told (a URL, a variable
        other than $HOME, $TMPDIR or $PWD, a command's output)."""
        text = word.strip()
        if not text or "://" in text:
            return None
        known = {"HOME": self.home, "TMPDIR": self.own[0], "PWD": self.cwd}
        for name, value in known.items():
            for form in (f"${{{name}}}", f"${name}"):
                if text == form or text.startswith(form + "/"):
                    text = str(value) + text[len(form):]
        if text.startswith("~"):  # ~/x, and ~name/x read as this user's own home folder
            text = str(self.home / text[1:].partition("/")[2])
        if "$" in text or "`" in text:
            return None
        path = Path(text)
        return Path(os.path.realpath(path if path.is_absolute() else self.cwd / path))

    def _place(self, path: Path) -> str | None:
        """Why a Mac command may not name this place."""
        if self.project is not None and _under(path, self.project):
            return ("it names the project's own folder, which changes only when the user keeps a result: use the "
                    "working copy.")
        if any(_under(path, place) for place in self.nicoder):
            return "it names Nicoder's own files (its chats, other requests and the app)."
        if _under(path, self.home):
            rel = str(path)[len(str(self.home)):].strip("/").lower()
            opened = [item.lower() for item in LIBRARY_OPEN + ALLOWED_INSIDE_DENIED]
            if not any(rel == item or rel.startswith(item + "/") for item in opened):
                for denied in DENIED_HOME_DIRS:
                    if rel == denied.lower() or rel.startswith(denied.lower() + "/"):
                        return f"it names ~/{denied}, which holds the user's private app data or credentials."
            if secret_name(path.name):
                return f"it names {path.name}, which looks like a secret."
        if any(part.lower() in DENIED_DIR_NAMES for part in path.parts):
            return "it names a folder of credentials."
        return None

    def _deleted(self, name: str, args: list[str]) -> list[str]:
        """The paths a command deletes or moves away."""
        operands = [word for word in args if not word.startswith("-")]
        if name in DELETERS:
            return operands
        if name == "mv":
            return operands[:-1]
        if name in {"find", "fd"} and _FIND_ACTIONS & set(args):
            if name == "fd":
                return operands
            starts = []
            for word in args:
                if word.startswith(("-", "(", "!")):
                    break
                starts.append(word)
            return starts or ["."]
        return []

    def _writes(self, writes: list[tuple[str, str]]) -> str | None:
        """Why a redirect may not write its file: `>` would wipe one of the user's files (`>>` appends)."""
        for operator, target in writes:
            path = self._resolve(target)
            if operator in _TRUNCATES and path is not None and self._wipes(path):
                return (f"it would overwrite {path.name}, one of the user's files. Append with >> or edit it in "
                        "place instead.")
        return None

    def _overwritten(self, name: str, args: list[str]) -> str | None:
        """Why `tee`, `cp`, `mv` or `ln -f` may not write their target: it is one of the user's files."""
        operands = [word for word in args if not word.startswith("-")]
        if name == "tee" and not {"-a", "--append"} & set(args):
            targets = operands
        elif name in {"cp", "mv", "ditto", "rsync"} and len(operands) > 1:
            targets = operands[-1:]
        elif name == "ln" and any(word.startswith("-") and "f" in word for word in args) and len(operands) > 1:
            targets = operands[-1:]
        else:
            return None
        for word in targets:
            path = self._resolve(word)
            if path is not None and self._wipes(path):
                return (f"it would overwrite {path.name}, one of the user's files. Append with >> or edit it in "
                        "place instead.")
        return None

    def _loses(self, path: Path) -> bool:
        """Deleting or moving this path would lose the user's files or data: it is in the home folder (or holds it),
        outside the working copy, temp folders and caches, or it is a service's data under /opt/homebrew/var."""
        if _under(self.home, path) or any(_under(root, path) for root in DATA_ROOTS):
            return True
        if any(_under(path, root) for root in DATA_ROOTS):
            housekeeping = path.name.lower().endswith((".pid", ".lock", ".log")) or any(
                part.lower() in {"log", "logs", "locks"} for part in path.parts)
            return not housekeeping
        if not _under(path, self.home) or any(_under(path, own) for own in self.own):
            return False
        return not any(_under(path, self.home / item) for item in SCRATCH)

    def _wipes(self, path: Path) -> bool:
        """Writing over this path would lose one of the user's files: an existing file that deleting would lose."""
        return path.is_file() and self._loses(path)
