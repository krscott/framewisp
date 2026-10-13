final: prev: {
  sway-unwrapped = prev.sway-unwrapped.overrideAttrs (old: {
    patches = (old.patches or [ ]) ++ [
      ./patches/sway-cursor-position.patch
    ];
  });

  wayvnc = prev.wayvnc.overrideAttrs (old: {
    patches = (old.patches or [ ]) ++ [
      ./patches/wayvnc-disable-clipboard.patch
    ];
  });

  python3 = prev.python3.override {
    packageOverrides = _: _: {
      framewisp = prev.python3.pkgs.callPackage ./default.nix {
        inherit (final) wayvnc sway-unwrapped;
      };
    };
  };

  python3Packages = final.python3.pkgs;

  framewisp = prev.python3.pkgs.toPythonApplication final.python3.pkgs.framewisp;
}
