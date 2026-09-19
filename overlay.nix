final: prev: {
  wayvnc = prev.wayvnc.overrideAttrs (old: {
    patches = (old.patches or [ ]) ++ [
      ./patches/wayvnc-disable-clipboard.patch
    ];
  });

  python3 = prev.python3.override {
    packageOverrides = _: _: {
      framewisp = prev.python3.pkgs.callPackage ./default.nix { inherit (final) wayvnc; };
    };
  };

  python3Packages = final.python3.pkgs;

  framewisp = prev.python3.pkgs.toPythonApplication final.python3.pkgs.framewisp;
}
