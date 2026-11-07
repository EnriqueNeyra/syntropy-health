"""What releases are built from: fixed versions, checked downloads, and a Compose file every Compose can read."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from app import cli
from app.core import config, settings

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _compose() -> dict:
    return yaml.safe_load(_read("docker-compose.yml"))["services"]["syntropy"]


# ---------------------------------------------------------------------------
# One set of versions
# ---------------------------------------------------------------------------

def test_the_lock_file_matches_pyproject_and_has_hashes():
    project = tomllib.loads(_read("pyproject.toml"))["project"]
    lock = tomllib.loads(_read("uv.lock"))
    packages = {p["name"]: p for p in lock["package"]}
    ours = packages["syntropy-health"]
    locked = {f"{d['name']}{('[' + ','.join(d['extras']) + ']') if d.get('extras') else ''}{d.get('specifier', '')}"
              for d in ours["metadata"]["requires-dist"] if "extra" not in d.get("marker", "")}
    # One computer's dependencies ("; sys_platform == 'darwin'") are locked under the same name and version.
    wanted = {re.sub(r"\s", "", d).split(";")[0].lower() for d in project["dependencies"]}
    assert wanted <= {d.lower() for d in locked}, "run uv lock"
    for package in lock["package"]:
        if package["name"] != "syntropy-health":
            files = [*package.get("wheels", []), *([package["sdist"]] if "sdist" in package else [])]
            assert files and all(f["hash"].startswith("sha256:") for f in files), package["name"]


@pytest.mark.parametrize("name, floor", [("pillow", (13, 0, 0)), ("pypdf", (6, 21, 2))])
def test_upload_parsers_are_past_their_published_problems(name, floor):
    """Both the floor anyone can resolve to, and what ships."""
    lock = {p["name"]: p.get("version") for p in tomllib.loads(_read("uv.lock"))["package"]}
    assert tuple(int(n) for n in lock[name].split(".")[:3]) >= floor
    dependencies = tomllib.loads(_read("pyproject.toml"))["project"]["dependencies"]
    spec = next(d for d in dependencies if d.lower().startswith(name))
    assert tuple(int(n) for n in spec.split(">=")[1].split(".")) >= floor


def test_every_place_that_uses_uv_uses_the_same_version():
    found = set(re.findall(r"astral-sh/uv:(\d+\.\d+\.\d+)@", _read("Dockerfile")))
    found |= set(re.findall(r"^UV_VERSION=(\S+)", _read("scripts/install.sh"), re.M))
    found |= set(re.findall(r'^\$UvVersion = "(\S+)"', _read("scripts/install.ps1"), re.M))
    assert len(found) == 1, found
    for workflow in WORKFLOWS:
        for job in yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"].values():
            for step in job.get("steps", []):
                if "setup-uv" in step.get("uses", ""):
                    assert step.get("with", {}).get("version") in found, f"{workflow.name}: setup-uv needs its version"


def test_the_windows_installer_stops_the_server_before_replacing_its_files():
    script = _read("scripts/install.ps1")
    assert script.index("Invoke-Quiet $Command service stop") < script.index("uv tool install --quiet")


def test_nothing_in_the_windows_installer_redirects_stderr_outside_its_helper():
    """Windows PowerShell stops the script at a redirected line of stderr, and uv reports its progress there."""
    lines = [line for line in _read("scripts/install.ps1").splitlines() if "2>$null" in line]
    assert len(lines) == 1 and "& $program @args" in lines[0]


def test_ci_runs_the_windows_installer_in_windows_powershell():
    steps = yaml.safe_load(_read(".github/workflows/ci.yml"))["jobs"]["windows-install"]["steps"]
    runs = [s for s in steps if "install.ps1 -Server" in s.get("run", "")]
    assert runs and all(s["shell"] == "powershell" for s in runs)


@pytest.mark.skipif(sys.platform == "win32", reason="a step of a Linux job, in a POSIX shell")
def test_ci_starts_the_sandbox_check_with_one_argument_per_setting(tmp_path):
    """A setting can hold spaces (RestrictAddressFamilies=AF_UNIX AF_INET ...). Split up, its second word is taken for the
    program to run. systemd-run itself only runs in CI, so this looks at what it would be given."""
    steps = yaml.safe_load(_read(".github/workflows/ci.yml"))["jobs"]["linux-install"]["steps"]
    script = next(s["run"] for s in steps if s.get("name") == "Lab photos can be read inside the service's sandbox")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("SYNTROPY_DATA_DIR", "/var/lib/syntropy-health")
        patch.setattr(cli, "_program", lambda: ["/opt/syntropy-health/venv/bin/python", "-m", "app"])
        patch.setattr(config.Path, "mkdir", lambda *_a, **_k: None)
        unit = cli._systemd_unit("0.0.0.0", 8000, "syntropy")
    (tmp_path / "unit").write_text(unit)
    script = script.replace("/etc/systemd/system/syntropy-health.service", str(tmp_path / "unit"))

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    stand_ins = {"sudo": 'exec "$@"\n', "apt-get": "exit 0\n", "systemd-analyze": "exit 0\n",
                 "systemd-run": f'for a in "$@"; do printf \'%s\\n--\\n\' "$a"; done > "{tmp_path}/argv"\n'}
    for name, body in stand_ins.items():
        (stubs / name).write_text(f"#!/bin/sh\n{body}")
        (stubs / name).chmod(0o755)
    env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", "GITHUB_STEP_SUMMARY": str(tmp_path / "summary")}
    subprocess.run(["bash", "-e", "-c", script], check=True, env=env, cwd=tmp_path)

    argv = (tmp_path / "argv").read_text().split("\n--\n")
    assert argv[:2] == ["--wait", "--pipe"]
    program = next(i for i, a in enumerate(argv) if not a.startswith("-"))
    assert argv[program:program + 2] == ["/opt/syntropy-health/venv/bin/python", "-c"] and "tesseract" in argv[program + 2]
    passed = argv[2:program]
    assert [a.removeprefix("--property=") for a in passed] == [line for line in unit.splitlines() if line.startswith((
        "User=", "NoNewPrivileges=", "Protect", "Private", "Restrict", "Lock", "Capability", "SystemCall", "ReadWritePaths="))]
    assert "--property=RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK" in passed


def test_the_installers_fetch_a_fixed_uv():
    assert "astral.sh/uv/$UV_VERSION/install.sh" in _read("scripts/install.sh")
    assert "astral.sh/uv/$UvVersion/install.ps1" in _read("scripts/install.ps1")
    assert "astral.sh/uv/install." not in _read("scripts/install.sh") + _read("scripts/install.ps1")


def test_the_image_is_built_from_fixed_digests_and_the_lock():
    dockerfile = _read("Dockerfile")
    bases = re.findall(r"^FROM (\S+)", dockerfile, re.M)
    assert bases and all(re.search(r"@sha256:[0-9a-f]{64}$", base) for base in bases), bases
    assert "uv export --frozen" in dockerfile and "pip install --require-hashes --no-deps" in dockerfile
    assert "tomllib" not in dockerfile


def _unlocked_pip_installs(text: str) -> list[str]:
    """Each `pip install` that could take versions or files nobody checked. One installs a list with hashes
    (--require-hashes), or this checkout alone (--no-deps .)."""
    found = []
    for line in text.splitlines():
        for command in re.split(r"&&|\|\||[;|]", line.split(" #")[0]):
            match = re.search(r"\bpip3? install\b(.*)", command)
            if not match:
                continue
            words = match.group(1).split()
            if "--require-hashes" in words or ("--no-deps" in words and [w for w in words if not w.startswith("-")] == ["."]):
                continue
            found.append(line.strip())
    return found


@pytest.mark.parametrize("line", [
    'pip install -q -e ".[checks]"',        # the directory refresh, which then pushed with the owner's token
    "pip install -e . --group dev",
    "python -m pip install -U pip && python -m pip install -r requirements.txt",
    "pip3 install --quiet playwright",
])
def test_an_install_without_the_lock_is_caught(line):
    assert _unlocked_pip_installs(f"        run: {line}")


def test_builds_install_from_the_lock():
    apps = _read(".github/workflows/apps.yml")
    assert "--require-hashes" in apps and 'pip install -e ".[desktop]"' not in apps
    assert "--version=" in apps.split("choco install innosetup")[1].splitlines()[0]
    assert not _unlocked_pip_installs('python -m pip install --require-hashes --no-deps -r "$RUNNER_TEMP/locked.txt"')
    assert not _unlocked_pip_installs("python -m pip install --no-deps -e .")
    for workflow in WORKFLOWS:
        assert not _unlocked_pip_installs(workflow.read_text(encoding="utf-8")), workflow.name


def test_the_directory_token_is_only_where_nothing_from_the_repository_runs():
    """The weekly refresh installs Playwright and runs the repository's scripts against the internet. DIRECTORY_TOKEN is
    the owner's and can push to this repository, so only a job that installs nothing, runs none of that code, and turns
    git's hooks off has it."""
    jobs = yaml.safe_load(_read(".github/workflows/directory.yml"))["jobs"]
    assert [name for name, job in jobs.items() if re.search(r"secrets\.DIRECTORY_TOKEN\s*}}", str(job))] == ["pull-request"]
    assert "DIRECTORY_TOKEN" not in str(jobs["refresh"])
    assert any("uv sync --frozen" in step.get("run", "") for step in jobs["refresh"]["steps"])
    for step in jobs["pull-request"]["steps"]:
        if "uses" in step:
            assert step["uses"].split("@")[0] in ("actions/checkout", "actions/download-artifact"), step["uses"]
            assert step["with"].get("persist-credentials", False) is False
        run = step.get("run", "")
        assert not re.search(r"\b(pip3?|uvx?|python3?|node|npm|npx|sh|bash|make|source)\b|scripts/|\.(sh|py)\b", run), step.get("name")
        if re.search(r"(^|[\s;&|(])git\s", run):
            assert run.splitlines()[0] == 'git() { command git -c core.hooksPath=/dev/null "$@"; }', step.get("name")


def test_actions_are_pinned_to_commits():
    for workflow in WORKFLOWS:
        for use in re.findall(r"^\s*(?:- )?uses:\s*(\S+)", workflow.read_text(encoding="utf-8"), re.M):
            if not use.startswith("./"):
                assert re.search(r"@[0-9a-f]{40}$", use), f"{workflow.name}: {use}"


def test_every_test_that_needs_playwright_runs_in_the_browser_job():
    """Only that job installs Playwright. A test that needs it anywhere but tests/e2e skips itself in every job, as the 52
    tests of Ask in a browser once did, and the job fails rather than pass without it."""
    uses_playwright = re.compile(r"^\s*(?:from|import) playwright\b|importorskip\(\s*[\"']playwright", re.M)
    outside = [p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").rglob("*.py")
               if (ROOT / "tests" / "e2e") not in p.parents and uses_playwright.search(p.read_text(encoding="utf-8"))]
    assert outside == []
    runs = [s.get("run", "") for s in yaml.safe_load(_read(".github/workflows/ci.yml"))["jobs"]["browser"]["steps"]]
    checked = next(i for i, run in enumerate(runs) if 'python -c "import playwright.sync_api"' in run)
    assert checked < next(i for i, run in enumerate(runs) if "pytest -q tests/e2e" in run)


def test_dependabot_moves_the_lock_and_the_base_image():
    updates = yaml.safe_load(_read(".github/dependabot.yml"))["updates"]
    ecosystems = {u["package-ecosystem"]: u for u in updates}
    assert ecosystems["uv"]["versioning-strategy"] == "lockfile-only" and "docker" in ecosystems and "pip" not in ecosystems


def test_a_stale_lock_or_a_known_problem_fails_ci():
    jobs = yaml.safe_load(_read(".github/workflows/ci.yml"))["jobs"]
    assert any("uv lock --check" in step.get("run", "") for step in jobs["lint"]["steps"])
    assert any("pip-audit" in step.get("run", "") for step in jobs["audit"]["steps"])
    assert "audit" in jobs["ci-passed"]["needs"]


def test_nothing_goes_unwatched_between_releases():
    for name in ("docker-refresh.yml", "supply-chain.yml"):
        workflow = yaml.safe_load(_read(f".github/workflows/{name}"))
        assert workflow[True]["schedule"], name      # (YAML reads `on:` as true)
    refresh = yaml.safe_load(_read(".github/workflows/docker-refresh.yml"))
    tags = next(s for s in refresh["jobs"]["publish"]["steps"] if s.get("id") == "tags")["run"]
    assert "$IMAGE:latest" in tags and "$IMAGE:$version\"" not in tags      # a version number keeps meaning one thing
    assert "--ignore=tests/test_deployment_files.py" in _read(".github/workflows/docker-refresh.yml")


def test_what_runs_pull_requests_has_no_write_permissions():
    workflow = yaml.safe_load(_read(".github/workflows/docker.yml"))
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert "image" in jobs, "main requires a check by this name"
    assert "permissions" not in jobs["image"] and "permissions" not in jobs["scan"]
    assert jobs["publish"]["if"] == "github.event_name != 'pull_request'" and jobs["publish"]["needs"] == "image"
    assert "secrets." not in str(jobs["image"])


def test_arm64_really_runs_before_it_is_published():
    steps = yaml.safe_load(_read(".github/workflows/docker.yml"))["jobs"]["publish"]["steps"]
    names = [s.get("name") for s in steps]
    assert names.index("Smoke test on arm64") < names.index("Build and publish (amd64 and arm64)")
    assert "linux/arm64" in next(s for s in steps if s.get("name") == "Smoke test on arm64")["run"]


def test_only_a_tag_on_main_is_released():
    """A v* tag on any other commit stops in the version job, which everything that signs or publishes waits for."""
    jobs = yaml.safe_load(_read(".github/workflows/desktop.yml"))["jobs"]
    steps = jobs["version"]["steps"]
    assert steps[0]["uses"].startswith("actions/checkout@") and steps[0]["with"]["fetch-depth"] == 0
    check = next(s for s in steps if s.get("name") == "A release's commit is on main")
    assert check["if"] == "startsWith(github.ref, 'refs/tags/v')"
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in check["run"]
    assert jobs["apps"]["needs"] == "version" and "version" in jobs["release"]["needs"]


def test_a_release_publishes_its_own_dependency_list_sboms_and_installers():
    release = _read(".github/workflows/desktop.yml")
    from app.services import updates
    for expected in (updates.REQUIREMENTS_ASSET, "cyclonedx1.5", "scripts/install.sh scripts/install.ps1 dist/"):
        assert expected in release
    assert "cp artifacts/wheel/* release/" in release


# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

def test_the_image_only_copies_what_docker_is_sent():
    allowed = {line[1:].rstrip("/") for line in _read(".dockerignore").splitlines() if line.startswith("!")}
    for sources in re.findall(r"^COPY (?!--from)(.+) \S+$", _read("Dockerfile"), re.M):
        for source in sources.split():
            assert source.rstrip("/") in allowed, source


def test_the_image_carries_the_licence_and_the_command():
    dockerfile = _read("Dockerfile")
    assert re.search(r"^COPY LICENSE NOTICE ", dockerfile, re.M)
    assert "COPY scripts/docker-syntropy-health.sh /usr/local/bin/syntropy-health" in dockerfile
    if sys.platform != "win32":      # no executable bit there
        assert (ROOT / "scripts" / "docker-syntropy-health.sh").stat().st_mode & 0o111
        assert (ROOT / "scripts" / "docker-entrypoint.sh").stat().st_mode & 0o111


def test_the_apps_carry_the_licence():
    spec = _read("desktop/syntropy-health.spec")
    assert '(str(ROOT / "LICENSE"), ".")' in spec and '(str(ROOT / "NOTICE"), ".")' in spec


def test_compose_has_no_syntax_older_composes_refuse():
    """env_file's path and required came with Compose 2.24, and some NAS systems ship older."""
    assert "env_file" not in _compose()


def test_settings_in_a_dot_env_file_still_reach_the_server():
    listed = {entry.split("=")[0] for entry in _compose()["environment"]}
    names = {"SYNTROPY_DB_PATH", "SYNTROPY_SECRET_KEY", "SYNTROPY_ALLOWED_HOSTS", "SYNTROPY_ALLOW_NO_PASSWORD", "SYNTROPY_UPDATE_CHECK",
             "SYNTROPY_SCHEDULER", "SYNTROPY_MDNS", "SYNTROPY_RELAY_KEY", "TZ", "FORWARDED_ALLOW_IPS"}
    for platform in settings.EHR_PLATFORMS.values():
        names |= {platform["env"], platform["sandbox_env"]} - {""}
    for wearable in settings.WEARABLES.values():
        names |= {wearable["id_env"], wearable["secret_env"]}
    assert names <= listed, names - listed


def test_compose_limits_the_container_and_its_logs():
    service = _compose()
    assert service["cap_drop"] == ["ALL"] and set(service["cap_add"]) == {"CHOWN", "DAC_READ_SEARCH", "SETUID", "SETGID"}
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["logging"]["options"]["max-size"] and service["logging"]["options"]["max-file"]


def test_the_smoke_test_runs_with_the_same_limits_as_compose():
    smoke = _read("scripts/docker_smoke.sh")
    for capability in _compose()["cap_add"]:
        assert f"--cap-add {capability}" in smoke
    assert "--cap-drop ALL" in smoke and "--security-opt no-new-privileges" in smoke


# ---------------------------------------------------------------------------
# The relay key
# ---------------------------------------------------------------------------

def test_a_release_without_the_relay_key_says_who_can_fix_it(monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("release_key_script", ROOT / "scripts" / "release_key.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    monkeypatch.delenv("SYNTROPY_RELAY_KEY", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", script.UPSTREAM)
    assert script.main() == 1
    out = capsys.readouterr().out
    assert "SYNTROPY_RELAY_KEY" in out and "owner" in out and "Secrets and variables" in out and out.startswith("::error::")
    monkeypatch.setenv("GITHUB_REPOSITORY", "someone/their-fork")
    assert script.main() == 0      # a fork's build uses its own registrations


def test_every_workflow_that_builds_for_people_runs_the_key_step():
    for name in ("docker.yml", "docker-refresh.yml", "apps.yml", "desktop.yml"):
        assert "scripts/release_key.py" in _read(f".github/workflows/{name}"), name
