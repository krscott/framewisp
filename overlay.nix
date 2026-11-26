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
  # keyboard and pointer input. With forwarding on, wayvnc 0.10.1 sends every
  # selection in the session to the VNC client, a GTK 4 app on X11 stops taking
  # input once it owns a selection of 256 KiB or more, and a client that
  # disconnects while offers are pending makes wayvnc exit
  # (https://github.com/krscott/framewisp/issues/30,
  # https://github.com/krscott/framewisp/issues/203).
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
