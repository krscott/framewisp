final: prev:
let
  # Scoped to framewisp so overlay users' other compositors keep stock wlroots.
  sway-unwrapped = prev.sway-unwrapped.override {
    wlroots_0_20 = prev.wlroots_0_20.overrideAttrs (old: {
      patches = (old.patches or [ ]) ++ [ ./patches/wlroots-xwayland-map-race.patch ];
    });
  };
in
{
  # Adds --disable-clipboard, which turns off VNC clipboard forwarding and keeps
  # keyboard and pointer input. A short-lived client can destroy its clipboard
  # device while offers are pending; libwayland discards them and leaves gaps in
  # its object map, so a later offer fails with an invalid object ID
  # (https://github.com/krscott/framewisp/issues/30).
  wayvnc = prev.wayvnc.overrideAttrs (old: {
    patches = (old.patches or [ ]) ++ [
      ./patches/wayvnc-disable-clipboard.patch
    ];
  });

  python3 = prev.python3.override {
    packageOverrides = _: _: {
      framewisp = prev.python3.pkgs.callPackage ./default.nix {
        inherit (final) wayvnc;
        inherit sway-unwrapped;
      };
    };
  };

  python3Packages = final.python3.pkgs;

  framewisp = prev.python3.pkgs.toPythonApplication final.python3.pkgs.framewisp;
}
