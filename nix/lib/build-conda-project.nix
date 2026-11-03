# SPDX-License-Identifier: MIT
#
# `buildCondaProject`: builds a conda project from its
# conda-purifier.lock.json in the Nix sandbox, offline.
#
# The file is a package for callPackage: the first argument set is what it
# takes from the `conda-purifier` scope, and the result is the builder. The
# conda-purifier overlay (`overlays.default`) adds that scope to the package
# set, so a package written for callPackage takes the scope by name:
#
#   { conda-purifier }:
#   (conda-purifier.buildCondaProject { src = ./.; }).packages.environment
#
# `buildCondaProject.override` replaces what the builder takes, and
# `conda-purifier.overrideScope` replaces what everything in the scope
# shares. The result has the attributes of the purifier contract:
#
#   packages.environment  the environment: conda's packages, created by
#                         micromamba from the locked files, with the pip:
#                         section installed into it
#   packages.<program>    each executable of the environment's bin/ on this
#                         platform, wrapped so that it runs with the
#                         environment's variables
#   tests                 the project's test command, run in the
#                         environment
#   release               the wheels of the project's own Python packages,
#                         which `conda-purifier publish` uploads
#   fetches               the purifier library entries the build fetches
#   shell                 a development shell with the environment active
#
# conda's binaries ask for the dynamic linker at /lib64, which the sandbox
# and NixOS do not have. The build points every ELF file of the environment
# at the linker of the package set; the libraries stay conda's own, found
# through their $ORIGIN run paths.
{
  # The library of this flake's closure, with the shared purifier library
  # under lib.purifier and readLock under lib.conda-purifier.
  lib,
  stdenv,
  stdenvNoCC,
  writeText,
  micromamba,
  patchelf,
  makeWrapper,
  runCommand,
  mkShell,
  # What the entries of the lock need besides the fetchers: fetchEntry
  # takes them by name.
  unzip,
  zstd,
  python3,
  # The fetchers of nixpkgs for the downloads of the lock; `fetchers`
  # replaces any of them.
  fetchurl,
  fetchgit,
  fetchhg,
  fetchsvn,
  fetchbzr,
}:
{
  # The project directory, holding conda-purifier.lock.json and the files
  # it was made from.
  src,
  # Fetchers for the purifier library's fetchEntry and tree, in place of
  # nixpkgs' own: a fetchurl that brings credentials for a private channel,
  # or one that goes through a company mirror.
  fetchers ? { },
  # The lockfile. One kept outside `src` lets a packaging flake build a
  # project its author does not change; it replaces any copy in `src`.
  lockFile ? src + "/conda-purifier.lock.json",
  # The conda-lock.yml the lock was made from, when the lock is made from
  # one. A copy kept outside `src` replaces the copy in `src`.
  condaLock ? null,
  # The environment file the lock was made from, when `src` does not hold
  # it. It replaces any copy in `src`.
  environmentYml ? null,
  # The name of the environment in the store. The default is the `name` of
  # environment.yml, or conda-env.
  name ? null,
  # Runs the conda packages' post-link scripts after the interpreters are
  # patched, as conda does when it installs them.
  runLinkScripts ? true,
  # The project's test command, run in a writable copy of `src` with the
  # environment active. The default runs pytest when the environment has
  # it, and otherwise starts the environment's python.
  testCommand ? null,
}:
let
  # readLock and checkLock make every check of the lockfile and its inputs.
  lock = lib.purifier.checkLock (
    lib.conda-purifier.readLock {
      inherit
        src
        lockFile
        condaLock
        environmentYml
        ;
    }
  );

  inherit (stdenv.hostPlatform) system;
  subdir = (import ./subdir.nix).ofSystem system;
  locked =
    lock.outputs.environment
    // lock.outputs.environment.platforms.${system} or (throw (
      if subdir == null then
        "conda-purifier: conda has no platform for ${system}."
      else
        "conda-purifier: ${baseNameOf (toString lockFile)} has no packages for ${subdir}; run `conda-purifier lock --platform ${subdir}` together with the other platforms."
    ));

  envName =
    if name != null then
      name
    else if locked.name != null then
      locked.name
    else
      "conda-env";

  # This system's downloads, at their paths: the conda packages under pkgs/,
  # the pip: section's files under pip/ and its checkouts beside them.
  dependencies = lib.purifier.tree (
    {
      inherit
        runCommand
        unzip
        zstd
        python3
        writeText
        fetchurl
        fetchgit
        fetchhg
        fetchsvn
        fetchbzr
        ;
    }
    // fetchers
    // {
      name = "${envName}-dependencies";
      entries = lib.purifier.entriesFor system lock;
    }
  );

  hex =
    sri:
    builtins.convertHash {
      hash = sri;
      toHashFormat = "base16";
    };

  # micromamba installs from this list, in order, with the hash of each
  # package, from a package cache it finds every file in.
  explicit = writeText "${envName}-explicit.txt" (
    "@EXPLICIT\n" + lib.concatMapStrings (e: "${e.url}#sha256:${hex e.hash}\n") locked.conda
  );

  # A package's post-link script runs with the package's name, version and
  # build number, which its file name holds.
  linkScripts = writeText "${envName}-packages.tsv" (
    lib.concatMapStrings (
      e:
      let
        stem = lib.removeSuffix ".tar.bz2" (lib.removeSuffix ".conda" (baseNameOf e.path));
        parts = lib.splitString "-" stem;
        build = lib.last parts;
        buildNumber = lib.last (lib.splitString "_" build);
      in
      "${e.name}\t${e.version}\t${buildNumber}\n"
    ) locked.conda
  );

  # What pip installs, one tab-separated line each: a downloaded file, or a
  # directory to build (a checkout, or one of the project's own directories)
  # with the version the lock run recorded for it. pip builds a directory in
  # place, so the build copies it.
  pipTargets = lib.concatMapStrings (
    e:
    if e ? local then
      "build\t${e.version}\t${src}/${e.local}\n"
    else if e ? url then
      "file\t${e.version}\t${dependencies}/${e.path}\n"
    else
      "build\t${e.version}\t${dependencies}/${e.path}\n"
  ) locked.pip;

  # The build has no version control metadata. A package built from a
  # directory or a checkout whose version setuptools-scm computes gets the
  # version the lock run recorded, where pip saw the repository.
  scmNormalize =
    name:
    lib.toUpper (
      lib.concatStringsSep "_" (
        builtins.filter (s: builtins.isString s && s != "") (builtins.split "[-_.]+" name)
      )
    );
  scmVersions = lib.listToAttrs (
    map (e: lib.nameValuePair "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_${scmNormalize e.name}" e.version) (
      builtins.filter (e: !(e ? url)) locked.pip
    )
  );

  # Makes the direct_url.json of each directory pip built name it under
  # /build, and RECORD that file's new hash.
  pipRecords = writeText "pip-records.py" ''
    import base64, csv, hashlib, os, sys, sysconfig

    build = "file://" + sys.argv[1] + "/"
    site = sysconfig.get_path("purelib")
    for info in sorted(os.listdir(site)):
        url = os.path.join(site, info, "direct_url.json")
        if not info.endswith(".dist-info") or not os.path.isfile(url):
            continue
        with open(url, encoding="utf-8") as f:
            text = f.read()
        if build not in text:
            continue
        data = text.replace(build, "file:///build/").encode()
        with open(url, "wb") as f:
            f.write(data)
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        name = info + "/direct_url.json"
        record = os.path.join(site, info, "RECORD")
        with open(record, newline="", encoding="utf-8") as f:
            rows = [[name, f"sha256={digest}", str(len(data))] if r and r[0] == name else r for r in csv.reader(f)]
        with open(record, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
  '';

  environment = stdenvNoCC.mkDerivation (
    scmVersions
    // {
      name = envName;
      dontUnpack = true;
      nativeBuildInputs = [
        micromamba
        patchelf
      ];
      passAsFile = [
        "pipTargets"
        "stateJson"
      ];
      inherit pipTargets;
      stateJson = builtins.toJSON { env_vars = locked.variables; };
      dynamicLinker = stdenv.cc.bintools.dynamicLinker;
      buildPhase = ''
        runHook preBuild
        export HOME=$TMPDIR/home MAMBA_ROOT_PREFIX=$TMPDIR/root CONDA_PKGS_DIRS=$TMPDIR/pkgs
        # Python caches what the build imports with the time of the build;
        # compileall writes every cache at the end instead, by content, so
        # that the environment builds the same each time.
        export PYTHONDONTWRITEBYTECODE=1
        mkdir -p "$HOME" "$CONDA_PKGS_DIRS"
        for f in ${dependencies}/pkgs/*; do
          cp "$f" "$CONDA_PKGS_DIRS/$(basename "$f")"
        done

        micromamba create --yes --no-rc --offline --no-pyc --copy \
          --skip-run-link-scripts --prefix "$out" --file ${explicit}

        ${lib.optionalString stdenv.hostPlatform.isLinux ''
          while IFS= read -r -d "" f; do
            if [ ! -L "$f" ] && head -c 4 "$f" | grep -q $'\x7fELF' \
                && patchelf --print-interpreter "$f" >/dev/null 2>&1; then
              chmod u+w "$f"
              patchelf --set-interpreter "$dynamicLinker" "$f"
            fi
          done < <(find "$out" -type f -print0)
        ''}

        ${lib.optionalString runLinkScripts ''
          while IFS=$'\t' read -r pkg version build; do
            script="$out/bin/.$pkg-post-link.sh"
            if [ -f "$script" ]; then
              echo "running the post-link script of $pkg"
              PREFIX="$out" PKG_NAME="$pkg" PKG_VERSION="$version" PKG_BUILDNUM="$build" \
                PATH="$out/bin:$PATH" bash "$script"
            fi
          done < ${linkScripts}
        ''}

        if [ -s "$pipTargetsPath" ]; then
          pipInstall() {
            PIP_CONFIG_FILE=/dev/null "$out/bin/python" -m pip install --disable-pip-version-check \
              --no-index --no-deps --no-cache-dir --no-compile --no-warn-script-location \
              --find-links ${dependencies}/pip "$@"
          }
          files=()
          n=0
          while IFS=$'\t' read -r kind version path; do
            if [ "$kind" = build ]; then
              # Each directory builds on its own, so that setuptools-scm,
              # hatch-vcs and the other version-control plugins get its
              # version without the .git the build lacks.
              n=$((n + 1))
              cp -rL --no-preserve=mode "$path" "$TMPDIR/pip-$n"
              SETUPTOOLS_SCM_PRETEND_VERSION="$version" VCS_VERSIONING_PRETEND_VERSION="$version" \
                pipInstall "$TMPDIR/pip-$n"
            else
              files+=("$path")
            fi
          done < "$pipTargetsPath"
          if [ "''${#files[@]}" -gt 0 ]; then
            pipInstall "''${files[@]}"
          fi
        fi
        ${lib.optionalString stdenv.hostPlatform.isDarwin ''
          # conda-meta records micromamba's package cache, and pip each
          # directory it built, in the build directory. The Linux sandbox
          # always builds in /build; on darwin every build has a directory
          # of its own, so the records name /build here too.
          sed -i "s|''${TMPDIR%/}/|/build/|g" "$out"/conda-meta/*.json
          if [ -s "$pipTargetsPath" ]; then
            "$out/bin/python" ${pipRecords} "''${TMPDIR%/}"
          fi
        ''}
        if [ -x "$out/bin/python" ]; then
          stdlib=$("$out/bin/python" -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')
          "$out/bin/python" -m compileall -q -j0 --invalidation-mode unchecked-hash \
            "$stdlib" >/dev/null || true
          # pip refuses to install into the environment and says why (PEP 668).
          cat > "$stdlib/EXTERNALLY-MANAGED" <<'EOF'
        [externally-managed]
        Error=This environment is built by conda-purifier from conda-purifier.lock.json and is read-only. Add the package to the environment file, to its dependencies or to its pip: section, then run `conda-purifier lock`.
        EOF
        fi

        # conda reads the environment's variables from here when it activates
        # the environment.
        cp "$stateJsonPath" "$out/conda-meta/state"
        # The history records when the environment was made.
        echo "==> conda-purifier <==" > "$out/conda-meta/history"
        runHook postBuild
      '';
      dontInstall = true;
      dontFixup = true;
      passthru = {
        inherit dependencies locked;
      };
    }
  );

  setVariables = lib.concatStringsSep " " (
    lib.mapAttrsToList (k: v: "--set ${lib.escapeShellArg k} ${lib.escapeShellArg v}") locked.variables
  );

  # A store name allows fewer characters than a file name.
  storeSafe =
    builtins.replaceStrings
      [
        "@"
        "%"
        "~"
        "!"
        "["
        "]"
        ":"
      ]
      [
        "_"
        "_"
        "_"
        "_"
        "_"
        "_"
        "_"
      ];

  program =
    exe:
    runCommand (storeSafe exe)
      {
        nativeBuildInputs = [ makeWrapper ];
        meta.mainProgram = exe;
      }
      ''
        makeWrapper ${environment}/bin/${lib.escapeShellArg exe} "$out/bin/"${lib.escapeShellArg exe} \
          --set CONDA_PREFIX ${environment} --prefix PATH : ${environment}/bin ${setVariables}
      '';

  programs =
    if lib.elem "environment" locked.programs then
      throw "conda-purifier: the environment has a program named `environment`, which would hide packages.environment."
    else
      lib.genAttrs locked.programs program;

  wheel =
    pname: dir:
    let
      version = (lib.findFirst (e: e.name == pname) { version = "0"; } locked.pip).version;
    in
    stdenvNoCC.mkDerivation (
      scmVersions
      // {
        name = "${pname}-wheel";
        # The version the lock run recorded, for version-control plugins
        # that build without a .git.
        SETUPTOOLS_SCM_PRETEND_VERSION = version;
        VCS_VERSIONING_PRETEND_VERSION = version;
        dontUnpack = true;
        buildPhase = ''
          runHook preBuild
          export HOME=$TMPDIR/home
          cp -rL --no-preserve=mode ${lib.escapeShellArg "${src}/${dir}"} "$TMPDIR/source"
          mkdir -p "$out"
          PIP_CONFIG_FILE=/dev/null ${environment}/bin/python -m pip wheel --disable-pip-version-check \
            --no-index --no-deps --no-cache-dir --find-links ${dependencies}/pip \
            --wheel-dir "$out" "$TMPDIR/source"
          runHook postBuild
        '';
        dontInstall = true;
        dontFixup = true;
      }
    );

  wheels = lib.mapAttrs wheel locked.packages;

  activate = ''
    export CONDA_PREFIX=${environment}
    export PATH=${environment}/bin:$PATH
  ''
  + lib.concatStrings (
    lib.mapAttrsToList (k: v: "export ${k}=${lib.escapeShellArg v}\n") locked.variables
  );

  defaultTest =
    if lib.elem "pytest" locked.programs then
      "python -m pytest"
    else if lib.elem "python" locked.programs then
      "python -c pass"
    else
      "true";
in
{
  packages = programs // {
    inherit environment;
  };

  tests = runCommand "${envName}-tests" { } ''
    cp -rL --no-preserve=mode ${src} source
    cd source
    export HOME=$TMPDIR
    ${activate}
    ${if testCommand != null then testCommand else defaultTest}
    touch $out
  '';

  # One directory with the wheel of every package of the project. The wheel
  # files are named by their builds, so the directory links them at build
  # time rather than reading their names during evaluation.
  release = runCommand "${envName}-release" { } ''
    mkdir -p $out
    ${lib.concatMapStrings (drv: "ln -s ${drv}/*.whl $out/\n") (lib.attrValues wheels)}
  '';

  fetches = lock.entries;

  shell = mkShell {
    packages = [ environment ];
    shellHook = activate;
  };
}
