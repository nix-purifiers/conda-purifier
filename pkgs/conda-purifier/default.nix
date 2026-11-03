# SPDX-License-Identifier: MIT
#
# The conda-purifier command: `lock` writes conda-purifier.lock.json, and
# `env`, `run`, `shell` and `publish` use the environment Nix builds from
# it. The command carries the Nix library and the nixpkgs it was built with,
# so the environment it builds is the one buildCondaProject builds.
{
  lib,
  stdenvNoCC,
  stdenv,
  python3,
  makeWrapper,
  micromamba,
  patchelf,
  nix-prefetch-git,
  nix-prefetch-hg,
  nix-prefetch-svn,
  uv,
  glibc,
  git,
  mercurial,
  subversion,
  breezy,
  nix-prefetch-bzr,
}:
let
  python = python3.withPackages (ps: [ ps.pyyaml ]);
in
stdenvNoCC.mkDerivation {
  pname = "conda-purifier";
  version = "0.1.0";
  src = lib.fileset.toSource {
    root = ../..;
    fileset = lib.fileset.unions [
      ./conda_purifier.py
      ./test_conda_purifier.py
      ../../nix/lib
    ];
  };
  nativeBuildInputs = [ makeWrapper ];
  buildPhase = ''
    runHook preBuild
    (cd pkgs/conda-purifier && ${python.interpreter} -m unittest -v test_conda_purifier)
    runHook postBuild
  '';
  installPhase = ''
    runHook preInstall
    mkdir -p "$out/bin" "$out/libexec/conda-purifier" "$out/share/conda-purifier"
    cp pkgs/conda-purifier/conda_purifier.py "$out/libexec/conda-purifier/"
    cp -r nix/lib "$out/share/conda-purifier/nix-lib"
    makeWrapper ${python.interpreter} "$out/bin/conda-purifier" \
      --add-flags "$out/libexec/conda-purifier/conda_purifier.py" \
      --prefix PATH : ${
        lib.makeBinPath [
          git
          mercurial
          subversion
          breezy
        ]
      } \
      --set CONDA_PURIFIER_PREFETCH_BZR ${nix-prefetch-bzr}/bin/nix-prefetch-bzr \
      --set CONDA_PURIFIER_MICROMAMBA ${lib.getExe micromamba} \
      --set CONDA_PURIFIER_PATCHELF ${lib.getExe patchelf} \
      --set CONDA_PURIFIER_PREFETCH_GIT ${lib.getExe nix-prefetch-git} \
      --set CONDA_PURIFIER_PREFETCH_HG ${nix-prefetch-hg}/bin/nix-prefetch-hg \
      --set CONDA_PURIFIER_PREFETCH_SVN ${nix-prefetch-svn}/bin/nix-prefetch-svn \
      --set CONDA_PURIFIER_UV ${lib.getExe uv} \
      --set CONDA_PURIFIER_DYNAMIC_LINKER ${lib.escapeShellArg (lib.optionalString stdenv.hostPlatform.isLinux stdenv.cc.bintools.dynamicLinker)} \
      --set CONDA_PURIFIER_GLIBC ${glibc.version} \
      --set CONDA_PURIFIER_MACOS ${stdenv.hostPlatform.darwinMinVersion} \
      --set CONDA_PURIFIER_NIX_LIB "$out/share/conda-purifier/nix-lib" \
      --set CONDA_PURIFIER_FLAKE ${../..}
    runHook postInstall
  '';
  meta = {
    description = "Locks a conda environment for Nix and builds it offline";
    license = lib.licenses.mit;
    mainProgram = "conda-purifier";
    platforms = lib.platforms.linux ++ lib.platforms.darwin;
  };
}
