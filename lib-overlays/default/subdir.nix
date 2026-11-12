# SPDX-License-Identifier: MIT
#
# The conda platforms ("subdirs") and the Nix systems they are for.
rec {
  systems = {
    linux-64 = "x86_64-linux";
    linux-aarch64 = "aarch64-linux";
    linux-ppc64le = "powerpc64le-linux";
    osx-64 = "x86_64-darwin";
    osx-arm64 = "aarch64-darwin";
  };

  # The conda platform of a Nix system, or null when conda has none.
  ofSystem =
    system:
    let
      found = builtins.filter (subdir: systems.${subdir} == system) (builtins.attrNames systems);
    in
    if found == [ ] then null else builtins.head found;
}
