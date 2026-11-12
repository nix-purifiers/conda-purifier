# SPDX-License-Identifier: MIT
#
# The members of the `conda-purifier` scope that the default package
# overlay adds to the package set.
{ callPackage, lib, ... }:
{
  # The command that writes conda-purifier.lock.json and uses the
  # environment Nix builds from it.
  conda-purifier = callPackage ./conda-purifier { };

  # The builder, a package for callPackage in this scope
  # (pkgs/build-conda-project/default.nix). `lib` is the library of this flake's
  # closure, with the shared purifier library under lib.purifier and
  # readLock under lib.conda-purifier.
  buildCondaProject = callPackage ./build-conda-project { inherit lib; };
}
