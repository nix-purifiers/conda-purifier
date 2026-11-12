# SPDX-License-Identifier: MIT
#
# The `conda-purifier` library namespace. It holds `readLock`, which reads a
# project's lockfiles into the lock in memory of the purifier contract
# (lib-overlays/default/lock.nix). The builder needs a package set, so it is a member of
# the `conda-purifier` scope that the default package overlay adds.
{ ... }:
{

  overlay = final: prev: {
    conda-purifier = (prev.conda-purifier or { }) // {
      readLock = import ./lock.nix { lib = final; };
    };
  };

}
