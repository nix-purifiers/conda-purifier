# SPDX-License-Identifier: MIT
#
# The flake's checks. Every one builds in the sandbox; the only network
# access is the fixed-output fetches of locked files. The conformance-*
# checks are those of the purifier contract; conformance-relock and
# conformance-rebuild need the network or a second build, and run as CI
# jobs of the same names.
{
  pkgs,
  # The flake's default package overlay, which adds the conda-purifier
  # scope with buildCondaProject.
  overlay,
  # The flake's library, with lib.conda-purifier.readLock.
  lib,
  # The conda-purifier command.
  cli,
}:
let
  # Every fixture builds the way a consumer's package does: through the
  # builder that the overlay adds to the package set.
  withOverlay = pkgs.extend overlay;
  build = withOverlay.conda-purifier.buildCondaProject;

  # An environment.yml with python and numpy from conda-forge, and a pip:
  # section with a pure wheel, a wheel per platform, an sdist, a git
  # requirement and the project's own package.
  app = build { src = ./fixtures/app; };

  # Explicit spec files, one per platform, with MD5 hashes only, as
  # `conda list --explicit --md5` writes them.
  explicit = build {
    src = ./fixtures/explicit;
    name = "bzip2-env";
  };

  # A pip: section with a Mercurial and a Subversion requirement.
  vcs = build { src = ./fixtures/vcs; };

  # A conda-lock.yml, reused as it is.
  condaLock = build {
    src = ./fixtures/conda-lock;
    name = "zstd-env";
  };

  # A real project: the development environment of geopandas, pinned to a
  # commit, with a lockfile that conda-purifier wrote for it and that lives
  # here, outside the project's source. The source is fetched at evaluation
  # time, since lock.nix reads its environment file.
  geopandasSrc = builtins.fetchTarball {
    url = "https://github.com/geopandas/geopandas/archive/ead9a4fe380b3e5cb189fca03328af4e1db5fa33.tar.gz";
    sha256 = "sha256-9sR1nMgm6iiDQSD4KN8cI9WD38eB/mARv/MHGbOZwIo=";
  };
  geopandas = build {
    src = geopandasSrc;
    lockFile = ./real/geopandas/conda-purifier.lock.json;
  };

  # The lock in memory of a fixture, as the builder reads it.
  readLock =
    src: lockFile:
    lib.conda-purifier.readLock {
      inherit src;
      lockFile = lockFile;
    };
  appLock = readLock ./fixtures/app ./fixtures/app/conda-purifier.lock.json;

  # A lockfile check passes when `assertion` holds; it fails with
  # `message` otherwise.
  evalCheck =
    name: assertion: message:
    if assertion then
      pkgs.runCommand name { } "touch $out"
    else
      throw "conda-purifier check ${name}: ${message}";

  fails = value: !(builtins.tryEval (builtins.deepSeq value value)).success;

  # A refused lockfile fails at Nix evaluation, and the command refuses it
  # before it builds anything, with a message that names the command that
  # fixes it. `files` are copied into a project; `expected` is the message.
  refusal =
    name: refusedAtEvaluation: files: expected:
    assert lib.assertMsg refusedAtEvaluation
      "conda-purifier check ${name}: the build accepted the lockfile";
    pkgs.runCommand name { nativeBuildInputs = [ cli ]; } ''
      mkdir project
      ${lib.concatStrings (lib.mapAttrsToList (to: from: "cp ${from} project/${to}\n") files)}
      if conda-purifier -C project prefetch 2> message.txt; then
        echo "conda-purifier accepted the lockfile" >&2
        exit 1
      fi
      cat message.txt
      grep -qF ${lib.escapeShellArg expected} message.txt
      grep -qF 'run `conda-purifier lock' message.txt
      touch $out
    '';

  explicitInputs = {
    "linux-64.txt" = ./fixtures/explicit/linux-64.txt;
    "linux-aarch64.txt" = ./fixtures/explicit/linux-aarch64.txt;
  };
in
{
  # The project's command runs with the environment's variables and finds
  # a package of every kind the pip: section has, and numpy from conda.
  app =
    pkgs.runCommand "app-check"
      {
        nativeBuildInputs = [ app.packages.hello-conda ];
      }
      ''
        line=$(hello-conda 41)
        echo "$line"
        [ "$line" = "hello from conda: n+1=42 inverse-sum=1.5 six=1.17.0 numpy=2" ]
        touch $out
      '';

  # numpy comes from the conda package, not from a wheel on PyPI, and the
  # command of the git requirement is a program of the environment.
  app-environment =
    let
      environment = app.packages.environment;
    in
    pkgs.runCommand "app-environment-check"
      {
        nativeBuildInputs = [ app.packages.sample ];
      }
      ''
        ls ${environment}/conda-meta/numpy-2.*.json
        ${environment}/bin/python -c 'import importlib.metadata as m; i = m.distribution("numpy").read_text("INSTALLER").strip(); assert i == "conda", i'
        sample
        # Nothing from a nixpkgs Python reaches the environment: the build
        # inputs set no PYTHONPATH, and every directory on sys.path is the
        # environment's own.
        ${environment}/bin/python -c 'import os, sys; assert "PYTHONPATH" not in os.environ; bad = [p for p in sys.path if p and not p.startswith("${environment}")]; assert not bad, bad'
        grep -q HELLO_CONDA_GREETING ${environment}/conda-meta/state
        # pip says what to do instead of installing into the environment.
        if ${environment}/bin/python -m pip install --no-index six > pip.log 2>&1; then
          echo "pip installed into the environment" >&2
          exit 1
        fi
        grep -q 'run `conda-purifier lock`' pip.log
        touch $out
      '';

  # The release holds the wheel of the project's own package, and the
  # project's tests run against the environment.
  app-release = pkgs.runCommand "app-release-check" { } ''
    ls ${app.release}/hello_conda-0.1.0-py3-none-any.whl
    test -e ${app.tests}
    touch $out
  '';

  # The development shell holds the environment and sets its variables.
  app-shell =
    assert lib.elem app.packages.environment app.shell.nativeBuildInputs;
    pkgs.runCommand "app-shell-check" { } ''
      ${app.shell.shellHook}
      [ "$HELLO_CONDA_GREETING" = "hello from conda" ]
      [ "$CONDA_PREFIX" = ${app.packages.environment} ]
      touch $out
    '';

  # `fetches` lists the library entries of every download, and none of the
  # project's own directories.
  app-fetches = evalCheck "app-fetches" (
    lib.all (e: e ? url || e ? git) app.fetches
    && lib.any (e: e.name == "numpy") app.fetches
    && lib.any (e: e ? git) app.fetches
    && !(lib.any (e: e.name == "hello-conda") app.fetches)
  ) "fetches does not list the downloads of the app fixture";

  explicit = pkgs.runCommand "explicit-check" { nativeBuildInputs = [ explicit.packages.bzip2 ]; } ''
    echo hello | bzip2 | bzip2 -d | grep -qx hello
    touch $out
  '';

  conda-lock =
    pkgs.runCommand "conda-lock-check" { nativeBuildInputs = [ condaLock.packages.zstd ]; }
      ''
        echo hello | zstd | zstd -d | grep -qx hello
        touch $out
      '';

  # The Mercurial and Subversion requirements are installed from their
  # checkouts, with their commands.
  vcs = pkgs.runCommand "vcs-check" { nativeBuildInputs = [ vcs.packages.yoyo ]; } ''
    ${vcs.packages.environment}/bin/python -c 'import yoyo, docutils.core; print(docutils.core.publish_string("*x*", writer="html").count(b"<em>x</em>"))' | grep -qx 1
    yoyo --help > /dev/null
    touch $out
  '';

  # Each platform has its own packages and programs: the conda linker of
  # binutils is named for its platform.
  lock-platforms =
    let
      x86 = appLock.outputs.environment.platforms.x86_64-linux;
      arm = appLock.outputs.environment.platforms.aarch64-linux;
    in
    evalCheck "lock-platforms" (
      x86.subdir == "linux-64"
      && arm.subdir == "linux-aarch64"
      && lib.elem "x86_64-conda-linux-gnu-ld" x86.programs
      && lib.elem "aarch64-conda-linux-gnu-ld" arm.programs
      && x86.packages == { hello-conda = "hello"; }
      # The environment takes the name environment.yml gives it.
      && app.packages.environment.name == "hello-conda"
      # A platform the lock does not cover is refused.
      &&
        fails
          (
            (build.override {
              stdenv = withOverlay.stdenv // {
                hostPlatform = lib.systems.elaborate "powerpc64le-linux";
              };
            })
              { src = ./fixtures/app; }
          ).packages.environment.drvPath
    ) "the lock reader did not select each platform's packages and programs";

  # A package written for callPackage takes the conda-purifier scope by
  # name. override replaces what the builder takes, and overrideScope
  # replaces what everything in the scope shares.
  builder-package =
    let
      consumer =
        { conda-purifier }:
        (conda-purifier.buildCondaProject { src = ./fixtures/explicit; }).packages.environment;
      replaced = build.override {
        fetchurl = args: throw "replaced fetchurl was called for ${args.name or args.url}";
      };
      scoped = withOverlay.conda-purifier.overrideScope (
        final: prev: {
          fetchurl = args: throw "scope fetchurl was called for ${args.name or args.url}";
        }
      );
    in
    evalCheck "builder-package" (
      (withOverlay.callPackage consumer { }).drvPath == (build { src = ./fixtures/explicit; })
      .packages.environment.drvPath
      && build ? override
      && !((builtins.tryEval (replaced { src = ./fixtures/explicit; }).packages.environment.drvPath)
        .success
      )
      && !((builtins.tryEval
        (scoped.buildCondaProject { src = ./fixtures/explicit; }).packages.environment.drvPath
      ).success
      )
      # Nothing but the scope is added to the package set.
      && !(withOverlay ? buildCondaProject)
    ) "the builder is not a package of the conda-purifier scope that callPackage and override handle";

  # Every fixture's lock has the shape of the purifier contract. A noarch
  # package is one entry for every system, and a package for one platform
  # is an entry for its system only.
  lock-shape =
    let
      locks = [
        appLock
        (readLock ./fixtures/explicit ./fixtures/explicit/conda-purifier.lock.json)
        (readLock ./fixtures/conda-lock ./fixtures/conda-lock/conda-purifier.lock.json)
        (readLock ./fixtures/vcs ./fixtures/vcs/conda-purifier.lock.json)
      ];
      entry = suffix: lib.findFirst (e: lib.hasSuffix suffix e.path) null appLock.entries;
    in
    evalCheck "lock-shape" (
      lib.all (l: lib.purifier.checkLock l == l) locks
      && lib.all (l: l.entries != [ ]) locks
      &&
        lib.sort lib.lessThan (entry "six-1.17.0-py2.py3-none-any.whl").systems == [
          "aarch64-darwin"
          "aarch64-linux"
          "x86_64-linux"
        ]
      && (entry "manylinux2014_x86_64.whl").systems == [ "x86_64-linux" ]
      && app.fetches == appLock.entries
    ) "a fixture's lock does not have the shape of the purifier contract";

  # The project's own package and the git requirement build without version
  # control metadata, so setuptools-scm gets the versions the lock run saw.
  scm-versions = evalCheck "scm-versions" (
    app.packages.environment.SETUPTOOLS_SCM_PRETEND_VERSION_FOR_HELLO_CONDA == "0.1.0"
    && app.packages.environment.SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SAMPLEPROJECT == "4.0.0"
    && !(app.packages.environment ? SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SIX)
  ) "the builds do not pass the locked versions to setuptools-scm";

  # geopandas' own tests of its geometry methods and coordinate reference
  # systems pass in the environment, offline in the sandbox, against the
  # GDAL, GEOS and PROJ of conda-forge.
  real-geopandas = pkgs.runCommand "real-geopandas-check" { } ''
    cp -r ${geopandasSrc} source
    chmod -R u+w source
    cd source
    export HOME=$TMPDIR
    # setuptools-scm writes this file when geopandas is installed from a
    # checkout; the tests run from the source tree.
    echo '__version__ = "1.1.2.dev0+gead9a4fe"' > geopandas/_version.py
    ${geopandas.packages.environment}/bin/python -m pytest -q -p no:cacheprovider \
      geopandas/tests/test_geom_methods.py geopandas/tests/test_crs.py
    touch $out
  '';

  # The fixtures themselves, which the conformance-rebuild job of CI builds
  # twice with `nix build --rebuild`.
  fixture-app = app.packages.environment;
  fixture-explicit = explicit.packages.environment;

  conformance-stale =
    refusal "conformance-stale"
      (fails
        (build {
          src = ./fixtures/stale;
          lockFile = ./fixtures/app/conda-purifier.lock.json;
        }).packages.environment.drvPath
      )
      {
        "environment.yml" = ./fixtures/stale/environment.yml;
        "conda-purifier.lock.json" = ./fixtures/app/conda-purifier.lock.json;
      }
      "environment.yml changed after conda-purifier.lock.json was written";

  conformance-tampered =
    refusal "conformance-tampered"
      (fails
        (build {
          src = ./fixtures/explicit;
          lockFile = ./fixtures/tampered-hash/conda-purifier.lock.json;
        }).packages.environment.drvPath
      )
      (
        explicitInputs
        // {
          "conda-purifier.lock.json" = ./fixtures/tampered-hash/conda-purifier.lock.json;
        }
      )
      "was changed after conda-purifier wrote it";

  conformance-missing =
    refusal "conformance-missing"
      (fails
        (build {
          src = ./fixtures/explicit;
          lockFile = ./fixtures/missing-entry/conda-purifier.lock.json;
        }).packages.environment.drvPath
      )
      (
        explicitInputs
        // {
          "conda-purifier.lock.json" = ./fixtures/missing-entry/conda-purifier.lock.json;
        }
      )
      "has no SHA-256 for https://conda.anaconda.org/conda-forge/linux-64/_openmp_mutex-4.5-20_gnu.conda";

  # Nix cannot catch the parse error of a lockfile that is not valid JSON,
  # so only the command's refusal is checked here.
  conformance-malformed = refusal "conformance-malformed" true (
    explicitInputs // { "conda-purifier.lock.json" = ./fixtures/malformed/conda-purifier.lock.json; }
  ) "is not valid JSON";

  # A project whose source holds only the spec files builds from a lockfile
  # kept outside it. The source is a copy of the explicit fixture without
  # its lockfile, and the check fails when the two copies differ.
  conformance-external =
    let
      external = build {
        src = ./fixtures/external;
        lockFile = ./fixtures/explicit/conda-purifier.lock.json;
        name = "bzip2-env";
      };
      sameSpec =
        file:
        builtins.readFile (./fixtures/external + "/${file}")
        == builtins.readFile (./fixtures/explicit + "/${file}");
      # The source holds a conda-lock.yml that differs from the one the
      # lockfile was made from, and the build takes the one kept outside.
      replacedCondaLock = build {
        src = ./fixtures/external-conda-lock;
        lockFile = ./fixtures/conda-lock/conda-purifier.lock.json;
        condaLock = ./fixtures/conda-lock/conda-lock.yml;
        name = "zstd-env";
      };
    in
    assert lib.assertMsg (lib.all sameSpec [
      "linux-64.txt"
      "linux-aarch64.txt"
      "osx-arm64.txt"
    ]) "conda-purifier check conformance-external: fixtures/external differs from fixtures/explicit";
    assert lib.assertMsg (fails
      (build {
        src = ./fixtures/external-conda-lock;
        lockFile = ./fixtures/conda-lock/conda-purifier.lock.json;
      }).packages.environment.drvPath
    ) "conda-purifier check conformance-external: the source's conda-lock.yml was accepted";
    pkgs.runCommand "conformance-external"
      {
        nativeBuildInputs = [
          external.packages.bzip2
          replacedCondaLock.packages.zstd
        ];
      }
      ''
        echo hello | bzip2 | bzip2 -d | grep -qx hello
        echo hello | zstd | zstd -d | grep -qx hello
        touch $out
      '';

  # The everyday loop works in the environment `conda-purifier prefetch`
  # builds, activated as `conda activate` does it (its bin/ first on PATH,
  # CONDA_PREFIX set), with no network: the sandbox has none. The project's
  # program, python with its packages and the refusal of pip all work.
  conformance-offline = pkgs.runCommand "conformance-offline" { } ''
    export HOME=$TMPDIR
    export CONDA_PREFIX=${app.packages.environment}
    export PATH=$CONDA_PREFIX/bin:$PATH
    hello-conda 41 | tee out.txt
    grep -q 'n+1=42' out.txt
    python -c 'import numpy, msgpack, docopt, sample, hello_conda'
    if python -m pip install --no-index six > pip.log 2>&1; then
      echo "pip installed into the environment" >&2
      exit 1
    fi
    grep -q 'run `conda-purifier lock`' pip.log
    touch $out
  '';
}
