# SPDX-License-Identifier: MIT
"""conda-purifier: lock a conda environment for Nix, and use it offline.

`conda-purifier lock` records every file a conda environment needs in
conda-purifier.lock.json: the conda packages of each platform, and the
packages of the environment's `pip:` section with what their builds need.
`lib.conda-purifier.buildCondaProject` (nix/lib/build-conda-project.nix)
builds the environment from that lockfile in the Nix sandbox.
`conda-purifier prefetch` builds the same environment for day-to-day work
and keeps it with GC roots, and `conda-purifier publish` uploads the wheels
Nix built of the project's own packages.

The wrapper that installs this script sets CONDA_PURIFIER_* variables to
the tools it uses: micromamba, patchelf, the nix-prefetch scripts, the
dynamic linker, glibc version and macOS version of the package set, and the
Nix library.
"""

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.parse
import urllib.request
import zipfile

import yaml

LOCKFILE = "conda-purifier.lock.json"
FORMAT = 2
ENVIRONMENT_FILES = ("environment.yml", "environment.yaml")
CONDA_LOCK_FILE = "conda-lock.yml"
DEFAULT_PLATFORMS = ("linux-64", "linux-aarch64")

MICROMAMBA = os.environ.get("CONDA_PURIFIER_MICROMAMBA", "micromamba")
PATCHELF = os.environ.get("CONDA_PURIFIER_PATCHELF", "patchelf")
DYNAMIC_LINKER = os.environ.get("CONDA_PURIFIER_DYNAMIC_LINKER", "")
GLIBC = os.environ.get("CONDA_PURIFIER_GLIBC", "")
MACOS = os.environ.get("CONDA_PURIFIER_MACOS", "")
PREFETCH = {
    "git": os.environ.get("CONDA_PURIFIER_PREFETCH_GIT", "nix-prefetch-git"),
    "hg": os.environ.get("CONDA_PURIFIER_PREFETCH_HG", "nix-prefetch-hg"),
    "svn": os.environ.get("CONDA_PURIFIER_PREFETCH_SVN", "nix-prefetch-svn"),
    "bzr": os.environ.get("CONDA_PURIFIER_PREFETCH_BZR", "nix-prefetch-bzr"),
}
NIX_LIB = os.environ.get("CONDA_PURIFIER_NIX_LIB", "")
# The source of the conda-purifier flake this command was built from.
FLAKE = os.environ.get("CONDA_PURIFIER_FLAKE", "")

# The CPU names of conda subdirs, as they appear in wheel platform tags.
WHEEL_ARCH = {
    "linux-64": "x86_64",
    "linux-aarch64": "aarch64",
    "linux-ppc64le": "ppc64le",
}
MACOS_ARCH = {
    "osx-64": "x86_64",
    "osx-arm64": "arm64",
}


class Failure(Exception):
    """An error the user can act on; main() prints it without a traceback."""


def die(message):
    raise Failure(message)


def log(message):
    print(f"conda-purifier: {message}", file=sys.stderr)


def sri(hex_digest, algorithm="sha256"):
    return f"{algorithm}-" + base64.b64encode(bytes.fromhex(hex_digest)).decode()


def host_subdir():
    machine = platform.machine()
    if sys.platform.startswith("linux"):
        return {"x86_64": "linux-64", "aarch64": "linux-aarch64", "ppc64le": "linux-ppc64le"}.get(
            machine, f"linux-{machine}"
        )
    if sys.platform == "darwin":
        return "osx-arm64" if machine == "arm64" else "osx-64"
    die(f"conda-purifier runs on Linux and macOS, not {sys.platform}.")


def vcs_env(work):
    """The environment for commands that may run svn, as pip does.

    git, Mercurial and Bazaar take a proxy from https_proxy, and Subversion
    only from ~/.subversion/servers. With a proxy set, the lock run writes
    that file in its work directory and gives those commands a home there.
    pip keeps its own configuration in the real config directory.
    """
    env = dict(os.environ)
    proxy = env.get("https_proxy") or env.get("HTTPS_PROXY")
    if not proxy:
        return env
    parts = urllib.parse.urlsplit(proxy)
    home = os.path.join(work, "svn-home")
    config = os.path.join(home, ".subversion")
    os.makedirs(config, exist_ok=True)
    lines = ["[global]", f"http-proxy-host = {parts.hostname}", f"http-proxy-port = {parts.port or 80}"]
    if parts.username:
        lines.append(f"http-proxy-username = {urllib.parse.unquote(parts.username)}")
    if parts.password:
        lines.append(f"http-proxy-password = {urllib.parse.unquote(parts.password)}")
    with open(os.path.join(config, "servers"), "w") as f:
        f.write("\n".join(lines) + "\n")
    real_home = os.path.expanduser("~")
    env.setdefault("XDG_CONFIG_HOME", os.path.join(real_home, ".config"))
    env.setdefault("XDG_CACHE_HOME", os.path.join(real_home, ".cache"))
    env["HOME"] = home
    return env


def run(cmd, **kwargs):
    """Runs a command; a failure stops the lock run with its output."""
    result = subprocess.run(cmd, text=True, capture_output=True, **kwargs)
    if result.returncode != 0:
        die(
            f"{' '.join(cmd[:4])} ... failed with exit code {result.returncode}:\n"
            + result.stderr[-4000:]
            + result.stdout[-2000:]
        )
    return result.stdout


def download(url, target):
    """Downloads a URL to a file and returns its SHA-256 as hex."""
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "conda-purifier"})
    with urllib.request.urlopen(request) as response, open(target, "wb") as out:
        while chunk := response.read(1 << 20):
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest()


def basename(url):
    return urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])


# Inputs ------------------------------------------------------------------


# The conda-lock.yml that --tool-lockfile names, as an absolute path, or ""
# for the one in the project; set by main.
_tool_lockfile = ""


def input_path(root, name):
    """The file of a lock input: the --tool-lockfile stands in for conda-lock.yml."""
    if _tool_lockfile and name == CONDA_LOCK_FILE:
        return _tool_lockfile
    return os.path.join(root, name)


def inputs_hash(root, inputs):
    """The hash of the lock's inputs; nix/lib/lock.nix computes the same."""
    text = ""
    for name in inputs:
        # newline="" keeps the bytes builtins.readFile sees.
        with open(input_path(root, name), encoding="utf-8", newline="") as f:
            text += name + "\n" + f.read() + "\n"
    return hashlib.sha256(text.encode()).hexdigest()


def lock_hash(lock):
    """The hash of a lockfile without its lockHash; nix/lib/lock.nix computes the same.

    The JSON is the form builtins.toJSON writes: sorted keys, no spaces and
    characters as they are.
    """
    body = {k: v for k, v in lock.items() if k != "lockHash"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


# The lockfile --lockfile names, as an absolute path, or "" for the one in
# the project; set by main.
_lockfile = ""


def lock_file(root):
    """The project's lockfile: the one --lockfile names, or the one in root."""
    return _lockfile or os.path.join(root, LOCKFILE)


def read_lock(root):
    """Reads a project's lockfile, refusing a malformed or edited one."""
    path = lock_file(root)
    try:
        with open(path, encoding="utf-8") as f:
            lock = json.load(f)
    except json.JSONDecodeError as e:
        die(f"{path} is not valid JSON ({e}); run `conda-purifier lock` to write it again.")
    if lock.get("lockHash") != lock_hash(lock):
        die(f"{path} was changed after conda-purifier wrote it; run `conda-purifier lock` to write it again.")
    if lock.get("version") != FORMAT:
        die(f"{path} is format {lock.get('version')}; this conda-purifier reads format {FORMAT}. "
            "Run `conda-purifier lock` to write it again.")
    return lock


def find_source(root, files):
    """Returns (kind, [files]) for what the lock is made from.

    Without --file, a project that has a lockfile is locked again from the
    files it was made from, so `conda-purifier lock` keeps working on an
    environment-dev.yml or on explicit spec files.
    """
    if not files and os.path.exists(lock_file(root)):
        try:
            with open(lock_file(root), encoding="utf-8") as f:
                previous = json.load(f)
            files = previous["inputs"]
            # An environment file's other inputs are the requirement files
            # it includes, which the lock run finds again.
            if source_kind(input_path(root, files[0])) == "environment":
                files = files[:1]
        except (json.JSONDecodeError, KeyError, IndexError, OSError):
            files = []
    if files:
        kinds = {source_kind(input_path(root, f)) for f in files}
        if len(kinds) > 1:
            die("--file names files of different kinds; pass one environment.yml, one conda-lock.yml, or explicit spec files.")
        kind = kinds.pop()
        if kind != "explicit" and len(files) > 1:
            die(f"--file takes one {kind} file.")
        return kind, list(files)
    if os.path.exists(os.path.join(root, CONDA_LOCK_FILE)) or os.path.exists(input_path(root, CONDA_LOCK_FILE)):
        return "conda-lock", [CONDA_LOCK_FILE]
    for name in ENVIRONMENT_FILES:
        if os.path.exists(os.path.join(root, name)):
            return "environment", [name]
    die(f"{root} has no {CONDA_LOCK_FILE} or environment.yml; pass --file.")


def default_platforms(host):
    """The platforms a first lock covers: Linux's, and this machine's."""
    return list(DEFAULT_PLATFORMS) + ([host] if host not in DEFAULT_PLATFORMS else [])


def previous_platforms(root):
    """The platforms an existing lockfile covers, which a relock keeps."""
    try:
        with open(lock_file(root), encoding="utf-8") as f:
            return sorted(json.load(f).get("platforms", {}))
    except (json.JSONDecodeError, OSError, AttributeError):
        return []


def check_fresh(root, lock):
    """Refuses a lock whose inputs changed, naming them as the project does."""
    if inputs_hash(root, lock["inputs"]) != lock.get("inputsHash"):
        die(f"{', '.join(lock['inputs'])} changed after {os.path.basename(lock_file(root))} was written; run `conda-purifier lock` again.")


def check_complete(root, lock):
    """Refuses a lock that lacks the SHA-256 of a file its inputs list.

    A conda-lock.yml or an explicit spec file may list a file with an MD5
    hash only; the lockfile records the SHA-256 the lock run computed, by
    URL without a token. nix/lib/lock.nix makes the same check.
    """
    kind = source_kind(input_path(root, lock["inputs"][0]))
    urls = []
    if kind == "explicit":
        for name in lock["inputs"]:
            with open(input_path(root, name), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith(("#", "@")):
                        continue
                    url, _, fragment = line.partition("#")
                    if not re.fullmatch(r"(sha256:)?[0-9a-f]{64}", fragment):
                        urls.append(url)
    elif kind == "conda-lock":
        with open(input_path(root, lock["inputs"][0]), encoding="utf-8") as f:
            for p in conda_lock_packages(f.read()):
                if p.get("platform") in lock["platforms"] and not p["hash"].get("sha256"):
                    urls.append(p["url"])
    for url in urls:
        if without_token(url) not in lock.get("hashes", {}):
            die(f"{os.path.basename(lock_file(root))} has no SHA-256 for {without_token(url)}; "
                "run `conda-purifier lock` to write it again.")


def source_kind(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if re.search(r"^@EXPLICIT\s*$", text, re.M):
        return "explicit"
    data = yaml.safe_load(text)
    if isinstance(data, dict) and "package" in data and "metadata" in data:
        return "conda-lock"
    return "environment"


# Conda packages ----------------------------------------------------------


def without_token(url):
    """A channel URL without the access token of a private channel.

    anaconda.org and other servers put the token in the path, as
    /t/<token>/. The lockfile is committed, so the token stays out of it;
    the build fetches such a file with credentials of its own (the
    `fetchurl` argument of buildCondaProject).
    """
    return re.sub(r"/t/[^/]+/", "/", url, count=1)


# The URLs with their tokens, which the lock run itself downloads from.
_token_urls: dict[str, str] = {}


def lock_run_url(url):
    return _token_urls.get(url, url)


def conda_entry(url, sha256_hex, name, version):
    public = without_token(url)
    if public != url:
        _token_urls[public] = url
    url = public
    return {
        "name": name,
        "version": version,
        "path": f"pkgs/{basename(url)}",
        "url": url,
        "mirrors": [],
        "hash": sri(sha256_hex),
    }


class Micromamba:
    """micromamba with its own root, package cache and caches."""

    def __init__(self, work):
        self.work = work
        # The user's own conda settings stay out of the lock run, which
        # passes --no-rc for the same reason. micromamba records every
        # environment it creates under ~/.conda; the lock run's environments
        # stay out of the user's home.
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("CONDA_", "MAMBA_"))}
        self.env.update(
            HOME=os.path.join(work, "home"),
            MAMBA_ROOT_PREFIX=os.path.join(work, "root"),
            CONDA_PKGS_DIRS=os.path.join(work, "pkgs"),
            XDG_CACHE_HOME=os.path.join(work, "cache"),
        )
        os.makedirs(self.env["HOME"], exist_ok=True)

    def solve(self, environment, subdir, pins=()):
        """Solves an environment file for a platform.

        `pins` are exact specs of packages the previous lock resolved, which
        the solve keeps. They go into a copy of the environment file, without
        its pip: section, which a solve does not read.
        """
        env = dict(self.env)
        if subdir.startswith("linux-") and GLIBC:
            # The environment runs on the glibc of the Nix package set.
            env.setdefault("CONDA_OVERRIDE_GLIBC", GLIBC)
        elif subdir.startswith("osx-") and MACOS:
            # The environment runs on the macOS versions the Nix package
            # set builds for.
            env.setdefault("CONDA_OVERRIDE_OSX", MACOS)
        if pins:
            with open(environment, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            data["dependencies"] = [
                d for d in data.get("dependencies") or [] if isinstance(d, str)
            ] + list(pins)
            environment = os.path.join(self.work, f"pinned-{subdir}.yml")
            with open(environment, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f)
        try:
            out = run(
                [MICROMAMBA, "create", "--yes", "--dry-run", "--json", "--no-rc",
                 "--platform", subdir, "--prefix", os.path.join(self.work, "solve", subdir),
                 "--file", environment],
                env=env,
            )
        except Failure as e:
            if not pins:
                raise
            die(f"{e}\nThe environment file conflicts with the versions the lock keeps; "
                "run `conda-purifier update` with the packages to move.")
        actions = json.loads(out).get("actions", {})
        packages = actions.get("LINK") or actions.get("FETCH") or []
        if not packages:
            die(f"micromamba solved no packages for {subdir}.")
        return [conda_entry(p["url"], p["sha256"], p["name"], p["version"]) for p in packages]

    def create(self, prefix, entries):
        """Creates an environment from locked packages, for the lock run's pip."""
        explicit = self.explicit("host-explicit.txt", entries)
        run([MICROMAMBA, "create", "--yes", "--no-rc", "--prefix", prefix,
             "--file", explicit], env=self.env)
        patch_interpreters(prefix)

    def explicit(self, name, entries):
        """An explicit spec of locked packages, with the URLs the lock run uses."""
        path = os.path.join(self.work, name)
        with open(path, "w") as f:
            f.write("@EXPLICIT\n")
            f.writelines(
                f"{lock_run_url(e['url'])}#sha256:{base64.b64decode(e['hash'].split('-', 1)[1]).hex()}\n"
                for e in entries
            )
        return path

    def programs(self, subdir, entries):
        """The executables a platform's packages put in bin/.

        micromamba downloads and extracts the packages of any platform
        without running them, so the programs of every platform are known.
        """
        explicit = self.explicit(f"programs-{subdir}.txt", entries)
        run([MICROMAMBA, "create", "--yes", "--no-rc", "--download-only",
             "--platform", subdir, "--prefix", os.path.join(self.work, f"programs-{subdir}"),
             "--file", explicit], env=self.env)
        extracted = self.extracted()
        names = set()
        for e in entries:
            stem = re.sub(r"\.(conda|tar\.bz2)$", "", basename(e["url"]))
            key = (lock_run_url(e["url"]), stem)
            if key not in extracted:
                die(f"micromamba did not extract {e['url']}.")
            with open(os.path.join(extracted[key], "info", "paths.json")) as f:
                for p in json.load(f)["paths"]:
                    parts = p["_path"].split("/")
                    if len(parts) == 2 and parts[0] == "bin" and not parts[1].startswith("."):
                        names.add(parts[1])
        return names

    def extracted(self):
        """The extracted packages of micromamba's caches, by URL and name.

        micromamba 2 keeps them under the root prefix, in directories laid
        out after their URLs; older versions keep them flat in
        CONDA_PKGS_DIRS. Each one records its URL in info/repodata_record.json.
        """
        found = {}
        for cache in (os.path.join(self.env["MAMBA_ROOT_PREFIX"], "pkgs"), self.env["CONDA_PKGS_DIRS"]):
            for directory, subdirs, files in os.walk(cache):
                if "info" in subdirs and os.path.exists(os.path.join(directory, "info", "paths.json")):
                    subdirs.clear()
                    record = os.path.join(directory, "info", "repodata_record.json")
                    url = ""
                    if os.path.exists(record):
                        with open(record) as f:
                            url = json.load(f).get("url", "")
                    found[(url, os.path.basename(directory))] = directory
        return found


def patch_interpreters(prefix):
    """Points the ELF files of an environment at the package set's linker.

    conda's binaries name /lib64/ld-linux-*.so.2, which NixOS and the Nix
    sandbox do not have. build-conda-project.nix does the same.
    """
    if not sys.platform.startswith("linux") or not DYNAMIC_LINKER:
        return
    for directory, _, names in os.walk(prefix):
        for name in names:
            path = os.path.join(directory, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            with open(path, "rb") as f:
                if f.read(4) != b"\x7fELF":
                    continue
            probe = subprocess.run([PATCHELF, "--print-interpreter", path], capture_output=True)
            if probe.returncode == 0:
                os.chmod(path, os.stat(path).st_mode | 0o200)
                run([PATCHELF, "--set-interpreter", DYNAMIC_LINKER, path])


# Where the lock run keeps the files it downloads to hash or read them; set
# by cmd_lock for the length of the run.
_download_dir: str = ""

# The SHA-256 the lock run computed for files that a conda-lock.yml or an
# explicit spec file lists without one, by URL without a token. The
# lockfile records these, since Nix fetches a file only by its SHA-256.
_computed_hashes: dict[str, str] = {}


def download_hash(url):
    target = os.path.join(_download_dir, hashlib.sha256(url.encode()).hexdigest())
    sha = download(url, target)
    _computed_hashes[without_token(url)] = sri(sha)
    return sha


def verify_md5(url, md5):
    """Downloads a package that is known only by MD5 and returns its SHA-256."""
    target = os.path.join(_download_dir, hashlib.sha256(url.encode()).hexdigest())
    sha = download(url, target)
    with open(target, "rb") as f:
        actual = hashlib.md5(f.read()).hexdigest()
    if actual != md5:
        die(f"{url} has MD5 {actual}, and the spec says {md5}.")
    _computed_hashes[without_token(url)] = sri(sha)
    return sha


def unquote(value):
    """A YAML scalar as conda-lock writes it: plain, or in quotes."""
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def conda_lock_packages(text):
    """The packages of a conda-lock.yml, read line by line.

    Nix has no YAML reader, so nix/lib/lock.nix reads conda-lock.yml with
    these same rules, which fit the layout conda-lock writes. The lock run
    compares the result with a YAML parser's and refuses a file whose
    layout differs.
    """
    items, inside, parent = [], False, None
    for line in text.split("\n"):
        if not inside:
            inside = re.fullmatch(r"package:\s*", line) is not None
            continue
        if line.startswith("- "):
            items.append({"hash": {}})
            parent = None
        elif line and not line.startswith(" "):
            break
        if not items:
            continue
        m = re.fullmatch(r"(?:- |  )([A-Za-z_]+):(?: (.*))?", line)
        if m:
            if m.group(2):
                items[-1][m.group(1)] = unquote(m.group(2))
                parent = None
            else:
                parent = m.group(1)
            continue
        m = re.fullmatch(r"    ([A-Za-z0-9_]+): (.*)", line)
        if m and parent == "hash":
            items[-1]["hash"][m.group(1)] = unquote(m.group(2))
    return items


def lock_conda_lock(root, name, platforms):
    with open(input_path(root, name), encoding="utf-8") as f:
        text = f.read()
    data = yaml.safe_load(text)
    version = data.get("version")
    if version != 1:
        die(f"{name} is conda-lock format {version}; conda-purifier reads format 1.")

    def fields(p):
        return [str(p.get(k)) for k in ("platform", "manager", "url")] + [str(p.get("hash", {}).get("sha256"))]

    if [fields(p) for p in data["package"]] != [fields(p) for p in conda_lock_packages(text)]:
        die(f"{name} is not laid out as conda-lock writes it, and Nix reads it line by line; "
            "run `conda-lock` to write it again.")
    result = {}
    for p in data["package"]:
        subdir = p["platform"]
        if platforms and subdir not in platforms:
            continue
        target = result.setdefault(subdir, {"conda": [], "pip": [], "pipBuild": []})
        sha = p.get("hash", {}).get("sha256")
        if p["manager"] == "conda":
            if not sha:
                sha = verify_md5(p["url"], p["hash"]["md5"])
            target["conda"].append(conda_entry(p["url"], sha, p["name"], p["version"]))
        elif p["manager"] == "pip":
            if not sha:
                sha = download_hash(p["url"])
            target["pip"].append(pip_file_entry(p["url"], sha, p["name"], p["version"]))
        else:
            die(f"{name}: package {p['name']} has manager {p['manager']}.")
    return result


def update_conda_lock(root, name, packages):
    """Re-resolves a conda-lock.yml with conda-lock's own update.

    conda-lock reads the sources and platforms the lockfile records. With
    package names, it moves only those packages.
    """
    conda_lock = shutil.which("conda-lock")
    if not conda_lock:
        die(f"{name} is conda-lock's lockfile, and `conda-purifier update` runs conda-lock to update it; "
            "conda-lock is not on PATH.")
    lockfile = input_path(root, name)
    with open(lockfile, encoding="utf-8") as f:
        metadata = (yaml.safe_load(f) or {}).get("metadata", {})
    command = [conda_lock, "lock", "--lockfile", lockfile]
    for source in metadata.get("sources", []):
        command += ["--file", source]
    for platform in metadata.get("platforms", []):
        command += ["--platform", platform]
    for package in packages:
        command += ["--update", package]
    log(f"updating {name} with conda-lock")
    result = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        die(f"conda-lock failed:\n{result.stderr[-3000:]}")


def lock_explicit(root, names, platforms):
    result = {}
    for name in names:
        subdir = None
        entries = []
        with open(input_path(root, name), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                m = re.match(r"#\s*platform:\s*(\S+)", line)
                if m:
                    subdir = m.group(1)
                if not line or line.startswith("#") or line.startswith("@"):
                    continue
                url, _, fragment = line.partition("#")
                if fragment.startswith("sha256:"):
                    sha = fragment[len("sha256:"):]
                elif re.fullmatch(r"[0-9a-f]{64}", fragment):
                    sha = fragment
                elif re.fullmatch(r"[0-9a-f]{32}", fragment):
                    sha = verify_md5(url, fragment)
                else:
                    sha = download_hash(url)
                stem = re.sub(r"\.(conda|tar\.bz2)$", "", basename(url))
                pkg, version, _build = stem.rsplit("-", 2)
                entries.append(conda_entry(url, sha, pkg, version))
        if subdir is None:
            if len(platforms) == 1:
                subdir = platforms[0]
            else:
                die(f"{name} has no '# platform:' line; pass the one --platform it is for.")
        if subdir in result:
            die(f"two explicit spec files are for {subdir}.")
        result[subdir] = {"conda": entries, "pip": [], "pipBuild": []}
    return result


# pip packages ------------------------------------------------------------


def pip_file_entry(url, sha256_hex, name, version):
    return {
        "name": name,
        "version": version,
        "path": f"pip/{basename(url)}",
        "url": url,
        "mirrors": [],
        "hash": sri(sha256_hex),
    }


def prefetch_vcs(vcs, url, rev):
    """Returns (hash, store path) of a checkout, as the Nix fetchers make it."""
    if vcs == "git":
        out = run([PREFETCH["git"], "--quiet", "--url", url, "--rev", rev, "--fetch-submodules"])
        data = json.loads(out)
        return data.get("hash") or nix_sri(data["sha256"]), data["path"]
    if vcs == "svn":
        # nix-prefetch-svn runs svn without a home, so it cannot reach a
        # repository through a proxy. The export below is the one nixpkgs'
        # fetchsvn makes, and its NAR hash is the hash fetchsvn checks.
        work = os.path.dirname(_download_dir)
        target = os.path.join(work, "svn-export-" + hashlib.sha256(f"{url}@{rev}".encode()).hexdigest()[:16])
        if not os.path.exists(target):
            # svn expands keywords such as $Date$ in the local time zone and
            # locale; the sandbox has UTC and no locale archive.
            run(["svn", "export", "--trust-server-cert", "--non-interactive", "--quiet",
                 "-r", rev, url, target], env=dict(vcs_env(work), TZ="UTC", LC_ALL="C"))
        digest = run(["nix", "hash", "path", "--algo", "sha256", target]).strip()
        return digest, target
    if vcs in ("hg", "bzr"):
        # With PRINT_PATH set, these scripts print the hash and then the
        # store path of the checkout.
        result = subprocess.run([PREFETCH[vcs], url, rev], text=True, capture_output=True,
                                env=dict(vcs_env(os.path.dirname(_download_dir)), PRINT_PATH="1"))
        if result.returncode != 0:
            die(f"nix-prefetch-{vcs} {url} {rev} failed:\n{result.stderr[-3000:]}")
        digest, path = result.stdout.split()[:2]
        return nix_sri(digest), path
    die(f"{url} is a {vcs} repository; conda-purifier fetches git, Mercurial, Subversion and Bazaar.")


def nix_sri(digest):
    """Converts a Nix base-32 SHA-256 to SRI."""
    if digest.startswith("sha256-"):
        return digest
    out = run(["nix", "hash", "convert", "--hash-algo", "sha256", "--to", "sri", digest])
    return out.strip()


def wheel_platform_specific(filename):
    if not filename.endswith(".whl"):
        return False
    tags = filename[:-4].split("-")[-1]
    return any(t != "any" for t in tags.split("."))


def pyproject_build_requires(text):
    """The build requirements a pyproject.toml declares, or pip's default."""
    if text is None:
        return ["setuptools>=40.8.0"]
    data = tomllib.loads(text)
    build = data.get("build-system")
    if not build:
        return ["setuptools>=40.8.0"]
    return build.get("requires", [])


def read_pyproject_from_archive(path):
    """Reads the top-level pyproject.toml of an sdist."""
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                names = [n for n in z.namelist() if n.count("/") == 1 and n.endswith("/pyproject.toml")]
                return z.read(names[0]).decode() if names else None
        with tarfile.open(path) as t:
            for member in t.getmembers():
                if member.name.count("/") == 1 and member.name.endswith("/pyproject.toml"):
                    data = t.extractfile(member)
                    return data.read().decode() if data else None
    except (tarfile.TarError, zipfile.BadZipFile) as e:
        die(f"cannot read {path}: {e}")
    return None


class Pip:
    """pip of the lock run's host environment."""

    def __init__(self, python, cwd, index_options, work, constraints=()):
        self.python = python
        self.cwd = cwd
        self.index_options = index_options
        self.work = work
        self.count = 0
        # The versions the previous lock resolved, which every resolution
        # keeps (pip's constraints file).
        self.constraint_options = []
        if constraints:
            path = os.path.join(work, "constraints.txt")
            with open(path, "w") as f:
                f.write("\n".join(constraints) + "\n")
            self.constraint_options = ["-c", path]

    def report(self, args, isolated=True):
        """What pip would install, as its installation report.

        The pip: section resolves against the conda packages already in the
        environment (isolated=False), so pip adds only what conda does not
        provide. Build requirements and single wheels resolve on their own.
        """
        self.count += 1
        report = os.path.join(self.work, f"report-{self.count}.json")
        isolation = ["--ignore-installed"] if isolated else []
        run([self.python, "-m", "pip", "install", "--disable-pip-version-check", "--dry-run",
             *isolation, "--quiet", "--report", report, *self.index_options,
             *self.constraint_options, *args],
            cwd=self.cwd, env=vcs_env(self.work))
        with open(report) as f:
            return json.load(f)["install"]

    def python_version(self):
        out = run([self.python, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"])
        return out.strip()


def index_options_of(lines):
    options = []
    for line in lines:
        for flag in ("--index-url", "-i", "--extra-index-url", "--find-links", "-f"):
            if line.startswith(flag + " ") or line.startswith(flag + "="):
                options.extend(re.split(r"[ =]", line, maxsplit=1))
    return options


def manylinux_tags(subdir):
    arch = WHEEL_ARCH.get(subdir)
    if not arch:
        die(f"conda-purifier resolves pip packages for Linux platforms, not {subdir}.")
    major, minor = (int(x) for x in (GLIBC or "2.17").split("."))
    tags = [f"manylinux_{major}_{m}_{arch}" for m in range(minor, 16, -1)]
    return tags + [f"manylinux2014_{arch}", f"linux_{arch}"]


def macos_tags(subdir):
    arch = MACOS_ARCH[subdir]
    major, minor = (MACOS or "11.0").split(".")[:2]
    # pip adds the earlier macOS versions and universal2 to this tag.
    return [f"macosx_{major}_{minor}_{arch}"]


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def lock_pip(root, env_dir, requirements, host_python, host, platforms, work, constraints):
    """Locks an environment.yml `pip:` section for every platform."""
    lines = [str(r).strip() for r in requirements]
    reqs = os.path.join(work, "requirements.txt")
    with open(reqs, "w") as f:
        f.write("\n".join(lines) + "\n")
    pip = Pip(host_python, env_dir, index_options_of(lines), work, constraints)
    try:
        items = pip.report(["-r", reqs], isolated=False)
    except Failure as e:
        if not constraints:
            raise
        die(f"{e}\nThe pip: section conflicts with the versions the lock keeps; "
            "run `conda-purifier update` with the packages to move.")
    pyver = pip.python_version()

    shared = []  # entries every platform shares
    specific = []  # platform-specific wheels, as (name, version, entry)
    pending_builds = []  # the pyproject.toml text of what is built from source
    for item in items:
        name = item["metadata"]["name"]
        version = item["metadata"]["version"]
        info = item["download_info"]
        url = info["url"]
        if "vcs_info" in info:
            vcs = info["vcs_info"]["vcs"]
            rev = info["vcs_info"]["commit_id"]
            digest, path = prefetch_vcs(vcs, url, rev)
            source = {"url": url, "rev": rev}
            # The fields of the shared library's entries, as the hashes above
            # were computed.
            if vcs == "git":
                # pip checks out a git requirement's submodules.
                source.update(submodules=True, leaveDotGit=False, commitObject=None)
            elif vcs == "hg":
                source.update(subrepos=False)
            elif vcs == "svn":
                source.update(ignoreExternals=False, ignoreKeywords=False)
            shared.append({"name": name, "version": version, "path": f"pip-{vcs}/{name}",
                           "hash": digest, vcs: source})
            pending_builds.append(read_text(os.path.join(path, "pyproject.toml")))
        elif "dir_info" in info or url.startswith("file:"):
            local = urllib.request.url2pathname(urllib.parse.urlsplit(url).path)
            rel = os.path.relpath(local, root)
            if rel.startswith(".."):
                die(f"{name} comes from {local}, outside the project at {root}.")
            shared.append({"name": name, "version": version, "local": rel})
            if os.path.isdir(local):
                pending_builds.append(read_text(os.path.join(local, "pyproject.toml")))
            elif not local.endswith(".whl"):
                pending_builds.append(read_pyproject_from_archive(local))
        else:
            sha = info.get("archive_info", {}).get("hashes", {}).get("sha256") or download_hash(url)
            entry = pip_file_entry(url, sha, name, version)
            if wheel_platform_specific(basename(url)):
                specific.append((name, version, entry))
            else:
                shared.append(entry)
                if not basename(url).endswith(".whl"):
                    target = os.path.join(_download_dir, sha)
                    if not os.path.exists(target):
                        download(url, target)
                    pending_builds.append(read_pyproject_from_archive(target))

    # What the builds from source need, as wheels. pip builds each package
    # in its own isolated environment, so each one's requirements resolve
    # on their own.
    build_shared, build_specific = [], []
    seen = set()
    for text in pending_builds:
        requires = pyproject_build_requires(text)
        if not requires:
            continue
        for b in pip.report(["--only-binary=:all:", *requires]):
            url = b["download_info"]["url"]
            if url in seen:
                continue
            seen.add(url)
            sha = b["download_info"]["archive_info"]["hashes"]["sha256"]
            entry = pip_file_entry(url, sha, b["metadata"]["name"], b["metadata"]["version"])
            if wheel_platform_specific(basename(url)):
                build_specific.append((entry["name"], entry["version"], entry))
            else:
                build_shared.append(entry)

    result = {}
    for subdir in platforms:
        if subdir == host:
            result[subdir] = {
                "pip": shared + [e for _, _, e in specific],
                "pipBuild": build_shared + [e for _, _, e in build_specific],
            }
            continue
        pip_entries = list(shared)
        build_entries = list(build_shared)
        sdist_builds = []
        for name, version, _ in specific:
            entry = foreign_wheel(pip, name, version, subdir, pyver)
            if entry is None:
                # pip builds the package from its sdist on that platform.
                entry, pyproject = sdist_of(pip, name, version)
                sdist_builds.append(pyproject)
            pip_entries.append(entry)
        for name, version, _ in build_specific:
            build_entries.append(required_foreign_wheel(pip, name, version, subdir, pyver))
        for text in sdist_builds:
            for b in pip.report(["--only-binary=:all:", *pyproject_build_requires(text)]):
                url = b["download_info"]["url"]
                name, version = b["metadata"]["name"], b["metadata"]["version"]
                if wheel_platform_specific(basename(url)):
                    build_entries.append(required_foreign_wheel(pip, name, version, subdir, pyver))
                else:
                    sha = b["download_info"]["archive_info"]["hashes"]["sha256"]
                    build_entries.append(pip_file_entry(url, sha, name, version))
        unique = {e["url"]: e for e in build_entries}
        result[subdir] = {"pip": pip_entries, "pipBuild": list(unique.values())}
    return result


def foreign_wheel(pip, name, version, subdir, pyver):
    """The wheel of name==version for another platform, or None."""
    args = ["--only-binary=:all:", "--no-deps", "--python-version", pyver,
            "--implementation", "cp", "--abi", "cp" + pyver.replace(".", "")]
    for tag in macos_tags(subdir) if subdir in MACOS_ARCH else manylinux_tags(subdir):
        args += ["--platform", tag]
    try:
        found = pip.report(args + [f"{name}=={version}"])
    except Failure:
        return None
    info = found[0]["download_info"]
    return pip_file_entry(info["url"], info["archive_info"]["hashes"]["sha256"], name, version)


def required_foreign_wheel(pip, name, version, subdir, pyver):
    """The wheel of a build requirement for another platform.

    pip installs build requirements only as wheels here, as it does on
    this machine, so a build requirement needs a wheel for each platform.
    """
    entry = foreign_wheel(pip, name, version, subdir, pyver)
    if entry is None:
        die(f"the build requirement {name} {version} has no wheel for {subdir}.")
    return entry


def sdist_of(pip, name, version):
    """The sdist of name==version, and the pyproject.toml it holds."""
    found = pip.report(["--no-binary", name, "--no-deps", f"{name}=={version}"])
    info = found[0]["download_info"]
    url = info["url"]
    sha = info.get("archive_info", {}).get("hashes", {}).get("sha256")
    target = os.path.join(_download_dir, hashlib.sha256(url.encode()).hexdigest())
    actual = download(url, target)
    if sha and sha != actual:
        die(f"{url} does not match the hash its index gives.")
    return pip_file_entry(url, actual, name, version), read_pyproject_from_archive(target)


def verify_pip(root, env_dir, python, entries, build_entries, work):
    """Installs the pip packages offline from the locked files only."""
    files = os.path.join(work, "verify-files")
    build = os.path.join(work, "verify-build")
    os.makedirs(files, exist_ok=True)
    os.makedirs(build, exist_ok=True)
    targets = []
    for e in entries:
        if "url" in e:
            target = os.path.join(files, basename(e["url"]))
            sha = download(e["url"], target)
            if sri(sha) != e["hash"]:
                die(f"{e['url']} changed while locking.")
            targets.append(target)
        elif "local" in e:
            targets.append(os.path.join(root, e["local"]))
        else:
            vcs = next(k for k in ("git", "hg", "svn", "bzr") if k in e)
            _, path = prefetch_vcs(vcs, e[vcs]["url"], e[vcs]["rev"])
            copy = os.path.join(work, "verify-" + e["name"])
            shutil.copytree(path, copy, dirs_exist_ok=True)
            for d, _, names in os.walk(copy):
                os.chmod(d, 0o755)
                for n in names:
                    p = os.path.join(d, n)
                    if not os.path.islink(p):
                        os.chmod(p, 0o644 | (os.stat(p).st_mode & 0o111))
            targets.append(copy)
    for e in build_entries:
        download(e["url"], os.path.join(build, basename(e["url"])))
    target_dir = os.path.join(work, "verify-target")
    run([python, "-m", "pip", "install", "--disable-pip-version-check", "--quiet", "--no-index",
         "--no-deps", "--no-cache-dir", "--find-links", build, "--target", target_dir, *targets],
        cwd=env_dir, env=dict(os.environ, PIP_CONFIG_FILE=os.devnull))
    # The console scripts of the pip: section are programs of the environment.
    scripts = os.path.join(target_dir, "bin")
    return sorted(os.listdir(scripts)) if os.path.isdir(scripts) else []


# The lockfile ------------------------------------------------------------


def recorded_entry(e):
    """The fields of a pip entry that nix/lib/lock.nix cannot derive.

    lock.nix derives the path of a download from its URL or its name, and
    sets the checkout options pip itself uses.
    """
    if "local" in e:
        return {k: e[k] for k in ("name", "version", "local")}
    if "url" in e:
        return {k: e[k] for k in ("name", "version", "url", "hash")}
    vcs = next(k for k in ("git", "hg", "svn", "bzr") if k in e)
    return {"name": e["name"], "version": e["version"], "hash": e["hash"],
            vcs: {"url": e[vcs]["url"], "rev": e[vcs]["rev"]}}


def recorded_platforms(kind, found):
    """What the lockfile records for each platform.

    For an environment.yml, that is micromamba's solve and pip's resolution,
    which nothing else records; lock.nix derives each conda package's name,
    version and path from its file name. For a conda-lock.yml or explicit
    spec files, lock.nix reads the packages from those files, and the
    lockfile records only the programs, and under `hashes` the SHA-256 of
    files listed without one.
    """
    recorded = {}
    for subdir, locked in found.items():
        recorded[subdir] = {"programs": locked["programs"]}
        if kind == "environment":
            recorded[subdir].update(
                conda=[{"url": e["url"], "hash": e["hash"]} for e in locked["conda"]],
                pip=[recorded_entry(e) for e in locked["pip"]],
                pipBuild=[recorded_entry(e) for e in locked["pipBuild"]],
            )
    return recorded


def previous_lock(root):
    """The platforms of the existing lockfile, or {} when there is none.

    `lock` keeps their versions; `update` re-resolves.
    """
    try:
        with open(lock_file(root), encoding="utf-8") as f:
            lock = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    if lock.get("version") != FORMAT or lock.get("lockHash") != lock_hash(lock):
        return {}
    return {
        subdir: {
            "conda": [e["url"] for e in locked.get("conda", [])],
            "pip": locked.get("pip", []),
            "pipBuild": locked.get("pipBuild", []),
        }
        for subdir, locked in lock.get("platforms", {}).items()
    }


def conda_pin(url):
    """The exact spec of a conda package file: <name>==<version>=<build>."""
    stem = re.sub(r"\.(conda|tar\.bz2)$", "", basename(url))
    name, version, build = stem.rsplit("-", 2)
    return f"{name}=={version}={build}"


def requirement_name(requirement):
    """The package a conda spec or a pip requirement names, in lower case."""
    spec = str(requirement).split("::")[-1].strip()
    m = re.match(r"[A-Za-z0-9_.\-]+", spec)
    return m.group(0).lower() if m and not spec.startswith("-") else ""


def local_packages(lock):
    """The project's own Python packages, by name, as their directories."""
    return {
        e["name"]: e["local"]
        for locked in lock["platforms"].values()
        for e in locked.get("pip", [])
        if "local" in e
    }


# Commands ----------------------------------------------------------------


def start_tool_lockfile(root):
    """Starts the conda-lock.yml kept outside the project from the project's copy.

    With --tool-lockfile, `lock` and `update` write conda-lock.yml at that
    path and leave the copy in the project as it is.
    """
    tree_copy = os.path.join(root, CONDA_LOCK_FILE)
    if _tool_lockfile and not os.path.exists(_tool_lockfile) and os.path.exists(tree_copy):
        os.makedirs(os.path.dirname(_tool_lockfile), exist_ok=True)
        shutil.copyfile(tree_copy, _tool_lockfile)


def cmd_lock(args, update=None):
    """Writes the lockfile.

    `update` is None for `conda-purifier lock`, which keeps the versions of
    the previous lock; [] for `conda-purifier update`, which re-resolves
    every package; and the package names `update` was given otherwise.
    """
    global _download_dir
    root = os.path.abspath(args.directory)
    start_tool_lockfile(root)
    kind, inputs = find_source(root, args.file)
    if update is not None and kind == "explicit":
        die("explicit spec files pin every package and have no resolver; write them again "
            "with `conda list --explicit --md5`, then run `conda-purifier lock`.")
    if update is not None and kind == "conda-lock":
        update_conda_lock(root, inputs[0], update)
    host = host_subdir()
    platforms = list(args.platform or previous_platforms(root) or default_platforms(host))
    variables = {}
    pip_requirements = []
    env_name = None
    with tempfile.TemporaryDirectory(prefix="conda-purifier-") as work:
        _download_dir = os.path.join(work, "downloads")
        os.makedirs(_download_dir)
        mamba = Micromamba(work)
        if kind == "conda-lock":
            found = lock_conda_lock(root, inputs[0], args.platform)
        elif kind == "explicit":
            found = lock_explicit(root, inputs, platforms)
        else:
            path = os.path.join(root, inputs[0])
            with open(path, encoding="utf-8") as f:
                environment = yaml.safe_load(f) or {}
            variables = {k: str(v) for k, v in (environment.get("variables") or {}).items()}
            env_name = environment.get("name")
            for dep in environment.get("dependencies") or []:
                if isinstance(dep, dict):
                    section = dep.get("pip") or []
                    pip_requirements += section
                    # A requirements file the section includes is an input
                    # of the lock too.
                    inputs += [
                        os.path.relpath(os.path.join(os.path.dirname(path), str(r).split(None, 1)[1]), root)
                        for r in section if str(r).startswith("-r ")
                    ]
            # The previous lock's versions stay, except for the packages the
            # environment file names (its own specs decide them) and the
            # ones `update` moves.
            previous = previous_lock(root) if update != [] else {}
            free = {requirement_name(d) for d in environment.get("dependencies") or [] if isinstance(d, str)}
            free |= {n.lower() for n in update or []}
            found = {}
            for subdir in platforms:
                pins = [
                    pin for url in previous.get(subdir, {}).get("conda", [])
                    if (pin := conda_pin(url)).split("==")[0].lower() not in free
                ]
                log(f"solving {inputs[0]} for {subdir}" + (f", keeping {len(pins)} locked packages" if pins else ""))
                solved = mamba.solve(path, subdir, pins)
                # The same packages as before stay in the same install order,
                # which the pins would otherwise change: that order is valid,
                # and an unchanged project then relocks to the same bytes.
                prior = previous.get(subdir, {}).get("conda", [])
                by_url = {e["url"]: e for e in solved}
                if prior and sorted(by_url) == sorted(prior):
                    solved = [by_url[url] for url in prior]
                found[subdir] = {"conda": solved, "pip": [], "pipBuild": []}
            pip_free = free | {requirement_name(r) for r in pip_requirements}
            # The pip: section resolved once, on the machine of the previous
            # lock, so a platform that lock lacks keeps the versions of another.
            kept = previous.get(host) or next(iter(previous.values()), {})
            constraints = sorted({
                f"{e['name']}=={e['version']}"
                for e in kept.get("pip", []) + kept.get("pipBuild", [])
                if "url" in e and e["name"].lower() not in pip_free
            })
        if not found:
            die("no packages were locked; check --platform.")
        pip_programs = []
        if kind == "environment" and pip_requirements:
            if host not in found:
                die(f"the environment's pip resolves the pip: section, so --platform must include {host}, the platform of this machine.")
            prefix = os.path.join(work, "host-env")
            log(f"creating the {host} environment to resolve the pip: section")
            mamba.create(prefix, found[host]["conda"])
            python = os.path.join(prefix, "bin", "python")
            if not os.path.exists(python):
                die("the environment has a pip: section but no python.")
            env_dir = os.path.dirname(path)
            log("resolving the pip: section")
            pip_found = lock_pip(root, env_dir, pip_requirements, python, host, list(found), work, constraints)
            for subdir in found:
                found[subdir].update(pip_found[subdir])
            log("installing the pip: section offline from the locked files")
            pip_programs = verify_pip(root, env_dir, python, found[host]["pip"], found[host]["pipBuild"], work)
        for subdir, locked in found.items():
            log(f"reading the programs of the {subdir} packages")
            programs = set(mamba.programs(subdir, locked["conda"])) | set(pip_programs)
            locked["programs"] = sorted(programs)
    lock = {
        "version": FORMAT,
        "inputs": inputs,
        "inputsHash": inputs_hash(root, inputs),
        "platforms": recorded_platforms(kind, found),
    }
    if variables:
        lock["variables"] = variables
    if env_name:
        lock["outputs"] = {"name": str(env_name)}
    if kind != "environment" and _computed_hashes:
        lock["hashes"] = dict(sorted(_computed_hashes.items()))
    lock["lockHash"] = lock_hash(lock)
    with open(lock_file(root), "w") as f:
        json.dump(lock, f, indent=2, sort_keys=True)
        f.write("\n")
    counts = ", ".join(
        f"{s}: {len(p['conda'])} conda, {len(p['pip'])} pip" for s, p in sorted(found.items())
    )
    log(f"wrote {lock_file(root)} ({counts})")


def project_root(start):
    """The project directory: the nearest one upwards with a lockfile.

    With --lockfile or CONDA_PURIFIER_LOCKFILE, the lockfile is elsewhere,
    and the project is the directory -C names.
    """
    directory = os.path.abspath(start)
    if _lockfile:
        if not os.path.exists(_lockfile):
            die(f"{_lockfile} does not exist; run `conda-purifier lock --lockfile {_lockfile}` first.")
        return directory
    while not os.path.exists(os.path.join(directory, LOCKFILE)):
        parent = os.path.dirname(directory)
        if parent == directory:
            die(f"no {LOCKFILE} in {start} or above; run `conda-purifier lock` first.")
        directory = parent
    return directory


def gc_roots(root):
    """The directory of the GC roots: .conda-purifier/ beside the lockfile."""
    return os.path.join(os.path.dirname(lock_file(root)), ".conda-purifier")


def nix_build(root, attribute, link_name):
    """Builds an output of the project through Nix, with a GC root."""
    link = os.path.join(gc_roots(root), link_name)
    os.makedirs(os.path.dirname(link), exist_ok=True)
    result = subprocess.run(
        ["nix-build", os.path.join(NIX_LIB, "interactive.nix"), "--attr", attribute,
         "--argstr", "flake", FLAKE, "--argstr", "projectDir", root,
         "--argstr", "lockFile", _lockfile, "--argstr", "condaLock", _tool_lockfile,
         "--out-link", link],
        text=True, capture_output=True,
    )
    if result.returncode != 0:
        die(f"building {attribute} failed:\n" + result.stderr[-4000:])
    return os.path.realpath(link)


def cmd_prefetch(args):
    """Realizes what day-to-day development needs, with GC roots.

    .conda-purifier/env is the environment, which conda activates by path
    and editors take as their interpreter. .conda-purifier/downloads keeps
    every locked download, so that a rebuild after a change to the
    project's own packages works with the network cut off.
    """
    root = project_root(args.directory)
    # The lockfile's own errors read better here than from Nix.
    lock = read_lock(root)
    check_fresh(root, lock)
    check_complete(root, lock)
    prefix = nix_build(root, "packages.environment", "env")
    nix_build(root, "packages.environment.dependencies", "downloads")
    env_link = os.path.join(gc_roots(root), "env")
    log(f"the environment is {prefix}")
    log(f"activate it with `conda activate {env_link}`; editors take {env_link}/bin/python")


def cmd_publish(args):
    """Uploads the wheels Nix built of the project's own Python packages.

    uv publish uploads them; twine refuses the metadata version (2.5) that
    current build backends such as hatchling write.
    """
    root = project_root(args.directory)
    lock = read_lock(root)
    check_fresh(root, lock)
    check_complete(root, lock)
    if not local_packages(lock):
        die(f"{lock_file(root)} names no packages of the project; the pip: section has no local directories.")
    built = nix_build(root, "release", "release")
    wheels = [os.path.join(built, w) for w in sorted(os.listdir(built))]
    uv = os.environ.get("CONDA_PURIFIER_UV", "uv")
    os.execvp(uv, [uv, "publish", *args.publish_args, *wheels])


def main(argv=None):
    global _lockfile, _tool_lockfile
    parser = argparse.ArgumentParser(prog="conda-purifier", description=__doc__.split("\n")[0])
    parser.add_argument("-C", "--directory", default=".", help="the project directory")
    parser.add_argument("--lockfile", default=os.environ.get("CONDA_PURIFIER_LOCKFILE", ""),
                        help="the lockfile, when it is kept outside the project "
                        f"(default: $CONDA_PURIFIER_LOCKFILE, or {LOCKFILE} in the project)")
    parser.add_argument("--tool-lockfile", default=os.environ.get("CONDA_PURIFIER_TOOL_LOCKFILE", ""),
                        help="the conda-lock.yml, when it is kept outside the project; lock and update "
                        "write it there and leave the project's copy as it is "
                        f"(default: $CONDA_PURIFIER_TOOL_LOCKFILE, or {CONDA_LOCK_FILE} in the project)")
    sub = parser.add_subparsers(dest="command_name", required=True)
    lock = sub.add_parser("lock", help=f"write {LOCKFILE}")
    lock.add_argument("--file", "-f", action="append", default=[],
                      help="environment.yml, conda-lock.yml or explicit spec file(s), relative to the project")
    lock.add_argument("--platform", "-p", action="append",
                      help=f"a conda platform to lock for (default: {', '.join(DEFAULT_PLATFORMS)} and this machine's)")
    lock.set_defaults(func=cmd_lock)
    update = sub.add_parser(
        "update", help=f"re-resolve the packages (all, or the named ones) and write {LOCKFILE}"
    )
    update.add_argument("packages", nargs="*", help="the packages to move (default: all)")
    update.set_defaults(func=lambda a: cmd_lock(a, update=a.packages), file=[], platform=None)
    sub.add_parser(
        "prefetch", help="build the environment and keep it and every download with GC roots"
    ).set_defaults(func=cmd_prefetch)
    publish = sub.add_parser("publish", help="upload the wheels Nix built of the project's packages")
    publish.add_argument("publish_args", nargs=argparse.REMAINDER,
                         help="arguments for `uv publish`, after --")
    publish.set_defaults(func=cmd_publish)
    args = parser.parse_args(argv)
    _lockfile = os.path.abspath(args.lockfile) if args.lockfile else ""
    _tool_lockfile = os.path.abspath(args.tool_lockfile) if args.tool_lockfile else ""
    if getattr(args, "publish_args", None) and args.publish_args[0] == "--":
        args.publish_args = args.publish_args[1:]
    try:
        args.func(args)
    except Failure as e:
        print(f"conda-purifier: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
