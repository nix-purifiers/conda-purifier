# SPDX-License-Identifier: MIT
#
# `lib.conda-purifier.readLock`: reads a project's lockfiles into the lock
# in memory of the purifier contract:
#
#   purifier  "conda-purifier"
#   entries   every download, as an entry of the purifier library, with
#             `systems`: the Nix systems whose environment needs it
#   outputs   outputs.environment = {
#               name       the environment's name in environment.yml, or null
#               variables  the environment variables of environment.yml
#               platforms.<system> = {
#                 subdir    the conda platform, such as linux-64
#                 conda     the conda packages, in install order
#                 pip       the packages of the pip: section: downloads, and
#                           the project's own directories ({ name, version,
#                           local })
#                 pipBuild  the wheels that building the pip: section needs
#                 programs  the executables the environment has in bin/
#                 packages  the project's own Python packages, by name, as
#                           their directory relative to the project
#               }
#             }
#   problems  why the lockfile cannot be used, each naming the command
#             that fixes it
#
# conda-purifier.lock.json records only what the project's own files do
# not. For an environment.yml, that is the solve: each conda package's URL
# and hash, and the resolved pip: section. For a conda-lock.yml or explicit
# spec files, this file reads the packages from those files, and the
# lockfile adds the SHA-256 of a file they list with an MD5 hash only. In
# every case the lockfile records the programs, which come from the
# packages' contents.
{ lib }:
{
  # The project directory.
  src,
  lockFile ? src + "/conda-purifier.lock.json",
  # The conda-lock.yml and the environment file the lock was made from,
  # when the caller keeps them outside `src`. Each replaces the copy in
  # `src`; a conda-lock.yml takes the place of the input of that name, and
  # an environment file the place of the first input.
  condaLock ? null,
  environmentYml ? null,
}:
let
  subdirs = import ./subdir.nix;

  # The lockfile by its name in the project, not by its copy in the store.
  lockName = baseNameOf (toString lockFile);
  rerun = "run `conda-purifier lock` to write it again";

  # Nix cannot catch a JSON parse error, so a lockfile that is not valid
  # JSON fails evaluation, with the command that writes it again in the
  # error context.
  lock = builtins.addErrorContext "conda-purifier: ${lockName} is not valid JSON; ${rerun}." (
    builtins.fromJSON (builtins.readFile lockFile)
  );

  inputFiles =
    lib.optionalAttrs (condaLock != null) { "conda-lock.yml" = condaLock; }
    // lib.optionalAttrs (environmentYml != null) { ${builtins.head lock.inputs} = environmentYml; };
  readInput = name: builtins.readFile (inputFiles.${name} or (src + "/${name}"));

  # The same text conda-purifier hashes: each input's name and contents.
  inputsHash = builtins.hashString "sha256" (
    lib.concatMapStrings (name: name + "\n" + readInput name + "\n") lock.inputs
  );

  lockProblems =
    if
      (lock.lockHash or null)
      != builtins.hashString "sha256" (builtins.toJSON (removeAttrs lock [ "lockHash" ]))
    then
      [ "conda-purifier: ${lockName} was changed after conda-purifier wrote it; ${rerun}." ]
    else if (lock.version or null) != 2 then
      [
        "conda-purifier: ${lockName} is format ${
          toString (lock.version or "unknown")
        }; this builder reads format 2. Run `conda-purifier lock` with this version."
      ]
    else if inputsHash != lock.inputsHash then
      [
        "conda-purifier: ${lib.concatStringsSep ", " lock.inputs} changed after ${lockName} was written; run `conda-purifier lock` again."
      ]
    else
      [ ];

  # A channel URL without the access token of a private channel (/t/<token>/),
  # as conda-purifier records it.
  withoutToken =
    url:
    let
      segments = lib.splitString "/" url;
      n = builtins.length segments;
      at = lib.findFirst (i: builtins.elemAt segments i == "t" && i + 2 < n) null (lib.range 1 (n - 1));
    in
    if at == null then
      url
    else
      lib.concatStringsSep "/" (lib.take at segments ++ lib.drop (at + 2) segments);

  # The hash of a file the project's own lockfile lists: its SHA-256 there,
  # or the one the lock run computed; null when the lockfile has none.
  hashOf =
    url: sha256:
    if sha256 != null then
      builtins.convertHash {
        hash = sha256;
        hashAlgo = "sha256";
        toHashFormat = "sri";
      }
    else
      lock.hashes.${url} or null;

  # A conda package's file name is <name>-<version>-<build>; neither the
  # version nor the build holds a dash.
  condaEntry =
    url: hash:
    let
      file = baseNameOf url;
      parts = lib.splitString "-" (lib.removeSuffix ".tar.bz2" (lib.removeSuffix ".conda" file));
      n = builtins.length parts;
    in
    {
      name = lib.concatStringsSep "-" (lib.take (n - 2) parts);
      version = builtins.elemAt parts (n - 2);
      path = "pkgs/${file}";
      mirrors = [ ];
      inherit url hash;
    };

  # A pip entry with what the lockfile leaves out: the path of a download,
  # and the checkout options pip itself uses (it checks out a git
  # repository's submodules).
  checkoutOptions = {
    git = {
      submodules = true;
      leaveDotGit = false;
      commitObject = null;
    };
    hg.subrepos = false;
    svn = {
      ignoreExternals = false;
      ignoreKeywords = false;
    };
    bzr = { };
  };
  pipEntry =
    e:
    if e ? local then
      e
    else if e ? url then
      e
      // {
        path = "pip/${baseNameOf e.url}";
        mirrors = [ ];
      }
    else
      let
        vcs = lib.findFirst (k: e ? ${k}) null (builtins.attrNames checkoutOptions);
      in
      e
      // {
        path = "pip-${vcs}/${e.name}";
        ${vcs} = e.${vcs} // checkoutOptions.${vcs};
      };

  unquote =
    value:
    let
      single = builtins.match "'(.*)'" value;
      double = builtins.match ''"(.*)"'' value;
    in
    if single != null then
      builtins.replaceStrings [ "''" ] [ "'" ] (builtins.head single)
    else if double != null then
      builtins.head double
    else
      value;

  # The packages of a conda-lock.yml, read line by line with the rules of
  # conda_lock_packages in conda_purifier.py. The lock run checks that those
  # rules read the file as a YAML parser does.
  condaLockPackages =
    yaml:
    let
      lines = lib.splitString "\n" yaml;
      header = lib.lists.findFirstIndex (l: builtins.match "package:[[:space:]]*" l != null) null lines;
      afterHeader = if header == null then [ ] else lib.drop (header + 1) lines;
      step =
        state: line:
        if state.done then
          state
        else
          let
            starts = lib.hasPrefix "- " line;
            ends = line != "" && !starts && !(lib.hasPrefix " " line);
            items = if starts then state.items ++ [ { hash = { }; } ] else state.items;
            field = builtins.match "(- |  )([A-Za-z_]+):( (.*))?" line;
            hashField = builtins.match "    ([A-Za-z0-9_]+): (.*)" line;
            update = f: lib.take (builtins.length items - 1) items ++ [ (f (lib.last items)) ];
          in
          if ends then
            state // { done = true; }
          else if items == [ ] then
            state
          else if field != null then
            let
              key = builtins.elemAt field 1;
              value = builtins.elemAt field 3;
            in
            if value != null && value != "" then
              {
                inherit (state) done;
                parent = null;
                items = update (item: item // { ${key} = unquote value; });
              }
            else
              {
                inherit (state) done;
                parent = key;
                inherit items;
              }
          else if hashField != null && state.parent == "hash" then
            {
              inherit (state) done parent;
              items = update (
                item:
                item
                // {
                  hash = item.hash // {
                    ${builtins.elemAt hashField 0} = unquote (builtins.elemAt hashField 1);
                  };
                }
              );
            }
          else
            state // { inherit items; };
    in
    (builtins.foldl' step {
      done = false;
      parent = null;
      items = [ ];
    } afterHeader).items;

  fromCondaLock =
    subdir:
    let
      packages = builtins.filter (p: p.platform == subdir) (
        condaLockPackages (readInput (builtins.head lock.inputs))
      );
      hashOfPackage = p: hashOf (withoutToken p.url) (p.hash.sha256 or null);
    in
    {
      conda = map (p: condaEntry (withoutToken p.url) (hashOfPackage p)) (
        builtins.filter (p: p.manager == "conda") packages
      );
      pip = map (
        p:
        pipEntry {
          inherit (p) name version url;
          hash = hashOfPackage p;
        }
      ) (builtins.filter (p: p.manager == "pip") packages);
    };

  # The packages of the explicit spec file for a platform, read as
  # lock_explicit in conda_purifier.py reads it. A file with no
  # `# platform:` line is for the lock's only platform.
  explicitLines = name: map lib.trim (lib.splitString "\n" (readInput name));
  explicitPlatform =
    name:
    let
      found = lib.findFirst (m: m != null) null (
        map (builtins.match "#[[:space:]]*platform:[[:space:]]*([^[:space:]]+).*") (explicitLines name)
      );
    in
    if found != null then
      builtins.head found
    else if builtins.length (builtins.attrNames lock.platforms) == 1 then
      builtins.head (builtins.attrNames lock.platforms)
    else
      null;
  fromExplicit =
    subdir:
    let
      file = lib.findFirst (name: explicitPlatform name == subdir) null lock.inputs;
      entry =
        line:
        let
          parts = builtins.match "([^#]*)(#(.*))?" line;
          url = withoutToken (builtins.head parts);
          fragment = builtins.elemAt parts 2;
          sha256 =
            if fragment == null then
              null
            else
              let
                m = builtins.match "(sha256:)?([0-9a-f]{64})" fragment;
              in
              if m == null then null else builtins.elemAt m 1;
        in
        condaEntry url (hashOf url sha256);
    in
    {
      conda =
        if file == null then
          [ ]
        else
          map entry (
            builtins.filter (l: l != "" && !(lib.hasPrefix "#" l) && !(lib.hasPrefix "@" l)) (
              explicitLines file
            )
          );
      pip = [ ];
    };

  isExplicit = lib.any (l: lib.trim l == "@EXPLICIT") (
    lib.splitString "\n" (readInput (builtins.head lock.inputs))
  );

  platformOf =
    subdir: locked:
    let
      packages =
        if locked ? conda then
          {
            conda = map (e: condaEntry e.url e.hash) locked.conda;
            pip = map pipEntry locked.pip;
          }
        else if isExplicit then
          fromExplicit subdir
        else
          fromCondaLock subdir;
    in
    {
      inherit subdir;
      inherit (packages) conda pip;
      pipBuild = map pipEntry (locked.pipBuild or [ ]);
      inherit (locked) programs;
      packages = lib.listToAttrs (
        map (e: lib.nameValuePair e.name e.local) (builtins.filter (e: e ? local) packages.pip)
      );
    };

  # The platforms by Nix system. A conda platform with no Nix system, such
  # as win-64, has nothing for Nix to build.
  platforms = lib.listToAttrs (
    lib.concatMap (
      subdir:
      lib.optional (subdirs.systems ? ${subdir}) (
        lib.nameValuePair subdirs.systems.${subdir} (platformOf subdir lock.platforms.${subdir})
      )
    ) (builtins.attrNames lock.platforms)
  );

  # Every download once, with the systems that need it. Two platforms can
  # have different files at one path, such as the _openmp_mutex package of
  # the same name and build on linux-64 and linux-aarch64, so an entry is
  # the same download only when every field is the same.
  downloadsOf = p: p.conda ++ builtins.filter (e: !(e ? local)) (p.pip ++ p.pipBuild);
  entries =
    let
      bySystem = lib.mapAttrsToList (
        system: p: map (e: e // { systems = [ system ]; }) (downloadsOf p)
      ) platforms;
      merged = builtins.foldl' (
        acc: e:
        let
          key = builtins.toJSON (removeAttrs e [ "systems" ]);
        in
        acc
        // {
          ${key} = if acc ? ${key} then acc.${key} // { systems = acc.${key}.systems ++ e.systems; } else e;
        }
      ) { } (lib.concatLists bySystem);
    in
    builtins.attrValues merged;

  missingHashes = map (e: e.url) (builtins.filter (e: e ? url && e.hash == null) entries);
  problems =
    if lockProblems != [ ] then
      lockProblems
    else
      map (url: "conda-purifier: ${lockName} has no SHA-256 for ${url}; ${rerun}.") missingHashes
      ++ lib.optional (lib.any (p: p.conda == [ ]) (
        builtins.attrValues platforms
      )) "conda-purifier: ${lockName} has a platform with no conda packages; ${rerun}.";
in
{
  purifier = "conda-purifier";
  entries = if lockProblems == [ ] then entries else [ ];
  outputs.environment = {
    name = lock.outputs.name or null;
    variables = lock.variables or { };
    platforms = if lockProblems == [ ] then platforms else { };
  };
  inherit problems;
}
