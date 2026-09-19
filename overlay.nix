final: prev: {
  wayvnc = prev.wayvnc.overrideAttrs (old: {
    # https://github.com/any1/wayvnc/pull/456
    patches = (old.patches or [ ]) ++ [
      (prev.fetchpatch {
        url = "https://github.com/krscott/wayvnc/commit/a567db5577f3303e14cbde8af25faf47fc9fae3f.patch";
        hash = "sha256-FF0XRizwGNk67zAQd0X5Fe1c2JBzzOpXGi0Wad4Juak=";
      })
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
