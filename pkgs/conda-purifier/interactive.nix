# SPDX-License-Identifier: MIT
#
# What `conda-purifier prefetch` and `publish` build for a project: the same
# pkgs.conda-purifier.buildCondaProject a sandboxed build uses, from the
# project directory as it is on disk. The command evaluates this file with
# the conda-purifier flake it was built from and the project's absolute
# path, and keeps the results alive with GC roots under .conda-purifier/.
# The flake's lock pins the nixpkgs and the shared purifier library the
# build uses.
{
  # The source of the conda-purifier flake, a path in the store.
  flake,
  projectDir,
  # The lockfile, when it is kept outside the project; an absolute path.
  lockFile ? "",
  # The conda-lock.yml, when it is kept outside the project; an absolute
  # path.
  condaLock ? "",
}:
let
  conda-purifier = builtins.getFlake (toString flake);
  pkgs = import conda-purifier.inputs.nixpkgs {
    system = builtins.currentSystem;
    overlays = [ conda-purifier.overlays.default ];
  };
  inherit (conda-purifier) lib;
  # The project without what is not its source: its version control, the
  # links conda-purifier and nix-build leave in it.
  src = builtins.path {
    path = projectDir;
    name = "source";
    filter =
      path: type:
      let
        base = baseNameOf path;
      in
      !(base == ".git" || base == ".conda-purifier" || lib.hasPrefix "result" base);
  };
in
pkgs.conda-purifier.buildCondaProject (
  {
    inherit src;
  }
  // lib.optionalAttrs (lockFile != "") { lockFile = /. + lockFile; }
  // lib.optionalAttrs (condaLock != "") { condaLock = /. + condaLock; }
)
