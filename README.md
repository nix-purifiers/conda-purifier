# conda-purifier

conda-purifier keeps Conda's development workflow on locked,
Nix-resolved dependencies. Use the normal build and test commands in your
working tree; building the project itself in the Nix sandbox is an
optional next step.

## Quickstart

From the project root:

```zsh
nix shell 'github:nix-purifiers/conda-purifier'
conda-purifier lock
conda-purifier prefetch
conda activate ./.conda-purifier/env
python -m pytest
```

Commit the lockfiles that the lock command writes, including the build
tool's own lockfile when it has one. Keep using the project's usual build,
test and editor commands with this setup. Nix provides the locked
dependencies; the development commands above run outside the Nix sandbox.
No project Nix files are needed for this workflow.

Use your existing Conda shell initialization for `conda activate`. The
prepared environment is read-only; update dependencies through the lock.

Sandboxed builds of the whole project are an optional
[next step](#building-the-project-with-nix).

For persistent use, add these packages through your usual Nix configuration.


## Development loop

Work in the environment that `conda-purifier prefetch` builds:

```console
$ conda-purifier prefetch
conda-purifier: the environment is /nix/store/...-hello-conda
conda-purifier: activate it with `conda activate ./.conda-purifier/env`; editors take ./.conda-purifier/env/bin/python
$ conda activate ./.conda-purifier/env
$ python -m pytest
```

`.conda-purifier/` belongs in `.gitignore`. Activation sets `CONDA_PREFIX`,
`PATH` and the `variables:` of `environment.yml`. The environment is
read-only: `pip install` stops and says what to do instead.

To add a package, put it in `environment.yml` and run
`conda-purifier lock`. The lock run keeps the version of every package
that the file does not name, in the conda section and in the `pip:`
section, so adding one package does not move the others. The next
`prefetch` builds the new environment.

To move packages, run `conda-purifier update`. It solves the whole
environment again, or only the packages you name:

```console
$ conda-purifier update numpy pandas
```

conda has no lockfile of its own for an `environment.yml`, so
`conda-purifier.lock.json` holds the solve, and `lock` and `update` are the
two ways to change it. A `conda-lock.yml` is the lockfile that conda-lock
writes: `lock` keeps the resolution that file records, and `update` runs
the update of conda-lock on it first and then relocks. Explicit spec files
pin every package and conda has no resolver for them, so `update` does not
apply to them: write them again with `conda list --explicit` and run
`conda-purifier lock`.

`conda-purifier publish` builds `release` with Nix and uploads the wheels
with `uv publish`. Arguments after `--` go to `uv publish`:

```console
$ conda-purifier publish -- --publish-url https://test.pypi.org/legacy/ --token "$TOKEN"
```

A package of the project that setuptools-scm or hatch-vcs versions gets
the version that pip saw in the lock run, since the sandbox has no `.git`.

### Keeping the lockfiles outside the project

A flake can build a project that does not carry the lockfiles, such as one
whose committed `conda-lock.yml` is missing, stale or written by another
version of conda-lock. The flake keeps `conda-purifier.lock.json` and, if
the project has one, its own copy of `conda-lock.yml`, and passes them to
`buildCondaProject` as `lockFile` and `condaLock`. The build uses them in
place of the copies in `src`.

Every command takes `--lockfile <path>` for `conda-purifier.lock.json`, and
`--tool-lockfile <path>` for `conda-lock.yml`. The environment variables
`CONDA_PURIFIER_LOCKFILE` and `CONDA_PURIFIER_TOOL_LOCKFILE` stand in for
the flags, and a development shell can set them to the paths it passes to
the builder. With a tool lockfile, `lock` and `update` write
`conda-lock.yml` there and leave the copy in the project as it is. When the
file at that path does not exist yet, they start from the copy in the
project. The GC roots go in `.conda-purifier/` beside `conda-purifier.lock.json`.

## Building the project with Nix

Once the native development workflow is working, you can also build and
test the whole project in the Nix sandbox. For this step, keep a flake or
another Nix build definition in the project's repository, or maintain an
external packaging repository that points at its sources and lockfiles.
The builder consumes the same lockfiles as the development workflow.

A flake is optional and can live in the project or in another repository:

```nix
{
  inputs.nixpkgs.url =
    "github:NixOS/nixpkgs/nixos-unstable";
  inputs.conda-purifier.url =
    "github:nix-purifiers/conda-purifier";

  outputs =
    { nixpkgs, conda-purifier, ... }:
    let
      pkgs = import nixpkgs {
        system = "x86_64-linux";
        overlays = [ conda-purifier.overlays.default ];
      };
      project = pkgs.conda-purifier.buildCondaProject {
        src = ./.;
      };
    in
    {
      packages.x86_64-linux = project.packages;
      checks.x86_64-linux.tests = project.tests;
      devShells.x86_64-linux.default = project.shell;
    };
}
```

`pkgs.conda-purifier.buildCondaProject.override` replaces the packages that
the builder takes, such as `micromamba` or `fetchurl`, and
`pkgs.conda-purifier.overrideScope` replaces what everything in the scope
shares. A package in an overlay takes the scope by name, as the argument
`conda-purifier`.

The builder takes `src`, the project directory, and these optional
arguments. `lockFile` defaults to `conda-purifier.lock.json` in `src`, and
`condaLock` defaults to `conda-lock.yml` in `src`. `fetchers` replaces
fetchers for the downloads, `name` defaults to the `name` in
`environment.yml`, `runLinkScripts` runs the post-link scripts of conda
packages, `testCommand` replaces the test command, and `environmentYml`
supplies an environment file that is not in `src`. The builder returns
these attributes:

| Attribute | What it is |
|---|---|
| `packages.environment` | The environment, with the conda packages and the `pip:` section installed. |
| `packages.<program>` | Each executable in the `bin/` directory of the environment, wrapped so that it runs with the variables of the environment. The lockfile lists the programs of each platform. |
| `tests` | The project's test command, run in the sandbox with the environment active. The default is `python -m pytest` when the environment has pytest. The `testCommand` argument replaces it. |
| `release` | A directory with the wheel of each of the project's own Python packages. `conda-purifier publish` uploads it. |
| `fetches` | The entries of the purifier library that the build downloads, for SBOM, license and vulnerability tools. |
| `shell` | A development shell with the environment active. |

`lib.conda-purifier.readLock { src; lockFile; condaLock; }` reads the
lockfiles into the shape every purifier shares: `{ purifier; entries; outputs; problems; }`.
Each entry carries the Nix `systems` that need it.

### One-off builds without a project Nix file

For an ad hoc build, the builder can also be evaluated directly from the
project root. Replace the example package name with one from your project:

```zsh
nix build --impure --expr 'let
  purifier = builtins.getFlake "github:nix-purifiers/conda-purifier";
  pkgs = import purifier.inputs.nixpkgs {
    system = builtins.currentSystem;
    overlays = [ purifier.overlays.default ];
  };
in
(pkgs.conda-purifier.buildCondaProject { src = ./.; })
.packages.environment'
```

This uses the purifier's pinned nixpkgs, matching the lock command above.
To build with ambient `<nixpkgs>` from your Nix search path instead,
replace `import purifier.inputs.nixpkgs` with `import <nixpkgs>`.
Both forms need no Nix files in your project. The build tool and language
versions must match the lockfile; with ambient nixpkgs, lock with the
purifier from that package set or pass a compatible toolchain to the builder.

## Working offline

`conda-purifier prefetch` builds the environment and every locked download
for the current system, and keeps them alive with GC roots:
`.conda-purifier/env` and `.conda-purifier/downloads`. After a prefetch,
the development loop works with the network cut off, including a rebuild
after a change to the project's own packages, since the downloads are
already in the Nix store.

### Private channels

A private channel puts its access token in the URL (`/t/<token>/`). The
lockfile records the URL without the token, so the token stays out of your
repository. The build fetches files with the `fetchers.fetchurl` that you
pass to `buildCondaProject`, which for a private channel has to bring
credentials of its own, such as nixpkgs' `fetchurl` with a `netrcPhase`.

## Editor

Point the editor at `.conda-purifier/env/bin/python`, the way you would
point it at a conda environment. In VS Code, run "Python: Select
Interpreter". For pyright, pass
`--pythonpath .conda-purifier/env/bin/python`. Run
`conda-purifier prefetch` first, so that the interpreter exists.

## Troubleshooting

Each of these stops with a message that names the command to run, in the
commands and at Nix evaluation:

| What happened | What conda-purifier says |
|---|---|
| The environment file changed after the lock | `environment.yml changed after conda-purifier.lock.json was written; run conda-purifier lock again` |
| The lockfile was edited by hand | `conda-purifier.lock.json was changed after conda-purifier wrote it; run conda-purifier lock to write it again` |
| The lockfile is not valid JSON | `conda-purifier.lock.json is not valid JSON; run conda-purifier lock to write it again` |
| The lockfile lacks the hash of a file | `conda-purifier.lock.json has no SHA-256 for <url>; run conda-purifier lock to write it again` |
| The lockfile has an older format | `conda-purifier.lock.json is format 1; this conda-purifier reads format 2. Run conda-purifier lock to write it again` |
| A platform is missing from the lock | `conda-purifier.lock.json has no packages for linux-aarch64; run conda-purifier lock --platform linux-aarch64 together with the other platforms` |
| A kept version conflicts with a change | `The environment file conflicts with the versions the lock keeps; run conda-purifier update with the packages to move` |
| `pip install` inside the environment | `This environment is built by conda-purifier ... Add the package to the environment file ..., then run conda-purifier lock` |

## Supported language repositories

conda-purifier reads one of three inputs, found in this order:

- **`conda-lock.yml`**, as [conda-lock](https://github.com/conda/conda-lock)
  writes it (format 1). It already records every package and hash, so the
  lockfile copies none of them: Nix reads the packages from `conda-lock.yml`.
- **`environment.yml`**. conda-purifier solves it with micromamba for each
  platform, as conda would, and resolves its `pip:` section with the
  environment's own pip.
- **Explicit spec files**, as `conda list --explicit` writes them, one per
  platform, passed with `--file`. Nix reads the packages from these files.

For `conda-lock.yml` and explicit spec files, the lockfile adds only what
those files lack: the SHA-256 of a file listed with an MD5 hash only, and
the programs of each platform. For an `environment.yml`, the lockfile
records the solve, which nothing else records: the URL and hash of each
conda package and of each `pip:` download, the revision of each checkout,
the version and directory of each of the project's own packages, and the
build requirements of the `pip:` section. Nix derives the name, version and
path of every download from the URL.

The `pip:` section of an `environment.yml` may hold any requirement pip
accepts:

- packages from PyPI or another index (`--index-url` and
  `--extra-index-url` lines apply);
- wheels, and sdists, whose build requirements are locked as wheels;
- git, Mercurial, Subversion and Bazaar requirements, pinned to the
  revision pip resolves;
- the project's own packages, as directories inside the project;
- `-r` requirement files, which become inputs of the lock.

pip installs only what the conda packages do not already provide. By
default the lock covers `linux-64`, `linux-aarch64` and the platform of the
machine it runs on, and `--platform` names others. A package with a wheel
per platform gets the wheel of each platform; when a platform has no wheel,
its lock holds the sdist of the package and the build requirements of the
sdist.

The lock run uses its own micromamba root and package cache, and ignores
your `.condarc` and `CONDA_*` settings, so two machines produce the same
lock from the same inputs for the same platforms. It installs the `pip:`
section offline from the recorded files before it writes the lockfile.

Limits that affect a project:

- A `conda-lock.yml` has to be in format 1 and laid out as conda-lock
  writes it. Nix reads that file line by line, and the lock run compares
  the result with what a YAML parser reads.
- Every build requirement of the `pip:` section needs a wheel for each
  platform. The error names a requirement that has none.
- The `pip:` section resolves for Linux and macOS platforms, and
  conda-purifier builds on Linux and on Apple Silicon Macs. The wheels of
  the machine that locks are the ones its own pip picks: on a Mac, for the
  macOS version it runs. The wheels of the other platforms are those for
  the glibc and the macOS version of the Nix package set.
- The build runs the post-link scripts of conda packages and not their
  pre-link scripts.

## Examples

For day-to-day work on these projects, use the native-tool setup and
commands in [Quickstart](#quickstart) and [Development loop](#development-loop).
The Nix expressions below show optional sandboxed packaging of those
projects.

### geopandas

[geopandas](https://github.com/geopandas/geopandas) keeps its development
environment in `environment-dev.yml`: pandas, pyarrow, scipy, matplotlib and
the GDAL, GEOS and PROJ libraries. To work on it with a conda environment
from Nix, clone it and lock that file. geopandas does not carry a
`conda-purifier.lock.json`, so the lockfile lives in your own directory,
outside the clone:

```console
$ git clone https://github.com/geopandas/geopandas
$ export CONDA_PURIFIER_LOCKFILE=$PWD/conda-purifier.lock.json
$ conda-purifier -C geopandas lock --file environment-dev.yml
$ conda-purifier -C geopandas prefetch
$ conda activate ./.conda-purifier/env
$ cd geopandas && python -m pytest geopandas/tests/test_crs.py
```

To build the same environment in a flake of your own, next to
`conda-purifier.lock.json`, take geopandas as a non-flake input:

```nix
{
  inputs.nixpkgs.url =
    "github:NixOS/nixpkgs/nixos-unstable";
  inputs.conda-purifier.url =
    "github:nix-purifiers/conda-purifier";
  inputs.geopandas = {
    url = "github:geopandas/geopandas";
    flake = false;
  };

  outputs =
    {
      nixpkgs,
      conda-purifier,
      geopandas,
      ...
    }:
    let
      pkgs = import nixpkgs {
        system = "x86_64-linux";
        overlays = [ conda-purifier.overlays.default ];
      };
      project = pkgs.conda-purifier.buildCondaProject {
        src = geopandas;
        lockFile = ./conda-purifier.lock.json;
      };
    in
    {
      packages.x86_64-linux.default =
        project.packages.environment;
      devShells.x86_64-linux.default = project.shell;
    };
}
```

To install geopandas itself into the environment, add `pip` and a `pip:`
section with `-e .` to `environment-dev.yml`, then lock again.

## Credits

The purifier idea was inspired by nixcloud's
[cargo (libnix)](https://github.com/nixcloud/cargo).

conda-purifier uses [micromamba](https://github.com/mamba-org/mamba) to
solve and install, and [uv](https://github.com/astral-sh/uv) to publish.

Released under the MIT license; see [LICENSE](LICENSE).
