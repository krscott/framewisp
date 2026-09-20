{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    let
      supportedSystems = [
        "x86_64-linux"
      ];
    in
    flake-utils.lib.eachSystem supportedSystems (
      system:
      let
        localOverlay = import ./overlay.nix;

        pkgs = import nixpkgs {
          inherit system;
          overlays = [ localOverlay ];
        };

        pythonDev = pkgs.python3.pkgs.framewisp.pythonModule.withPackages (
          ps:
          with ps;
          [
            black
            isort
            mypy
            pytest
            pygobject-stubs
          ]
          ++ pkgs.framewisp.propagatedBuildInputs
          ++ pkgs.framewisp.nativeBuildInputs
        );

        mkApp = text: {
          type = "app";
          program = pkgs.lib.getExe (
            pkgs.writeShellApplication {
              name = "app";
              runtimeInputs = [
                pythonDev
                pkgs.pyright
                pkgs.just
              ];
              inherit text;
            }
          );
        };
      in
      {
        packages = {
          inherit (pkgs) framewisp;
          default = pkgs.framewisp;
        };

        checks.package = pkgs.runCommand "framewisp-package-test" { } ''
          export HOME="$TMPDIR"
          ${pkgs.coreutils}/bin/env -i \
            ${pkgs.framewisp}/bin/framewisp --agent-skill > agent-skill.md
          ${pkgs.diffutils}/bin/diff ${./framewisp/SKILL.md} agent-skill.md
          ${pkgs.coreutils}/bin/env -i \
            HOME="$HOME" \
            PATH="${pkgs.framewisp}/bin:${pkgs.ffmpeg}/bin:${pkgs.qt6.qtdeclarative}/bin" \
            QML_IMPORT_PATH="${pkgs.qt6.qtdeclarative}/lib/qt-6/qml" \
            QT_PLUGIN_PATH="${pkgs.qt6.qtbase}/lib/qt-6/plugins" \
            ${
              (pkgs.python3.withPackages (ps: [
                ps.pytest
                ps.pillow
                ps.pygobject3
              ]))
            }/bin/python \
            -m pytest -c ${./pyproject.toml} ${./tests}/test_integration.py --basetemp "$TMPDIR/tests"
          touch "$out"
        '';

        devShells = {
          default = pkgs.mkShell {
            inputsFrom = [ pkgs.framewisp ];
            nativeBuildInputs = [
              pythonDev
              pkgs.pyright
              pkgs.nodejs # For missing libatomic in some environments
              pkgs.gobject-introspection
            ];
            buildInputs = [ pkgs.gtk4 ];
            packages = [
              pkgs.python3.pkgs.venvShellHook
              pkgs.sway-unwrapped
              pkgs.wayvnc
              pkgs.grim
              pkgs.wf-recorder
              pkgs.wtype
              pkgs.bash
              pkgs.dbus
              pkgs.xwayland
              pkgs.xdotool
              pkgs.xmodmap
              pkgs.ffmpeg
              pkgs.gst_all_1.gstreamer
              pkgs.qt6.qtdeclarative # Qt menu regression probe
            ];
            venvDir = ".venv";
            postVenvCreation = ''
              pip install -e '.[dev]'
            '';
            shellHook = ''
              export GST_PLUGIN_SYSTEM_PATH_1_0=${pkgs.framewisp.capturePlugins}
              export FRAMEWISP_FONTCONFIG_FILE=${pkgs.framewisp.captionFonts}
              export FRAMEWISP_ATSPI_REGISTRY=${pkgs.framewisp.atspiRegistry}
              export QML_IMPORT_PATH=${pkgs.qt6.qtdeclarative}/lib/qt-6/qml
              export QT_PLUGIN_PATH=${pkgs.qt6.qtbase}/lib/qt-6/plugins
              runHook venvShellHook
              export PYTHONPATH="''${PYTHONPATH:-}:."
            '';
          };
        };

        apps = {
          format = mkApp "just format";
          lint = mkApp "${pkgs.nix}/bin/nix develop --command ${pkgs.just}/bin/just lint";
        };

        formatter = pkgs.nixfmt;
      }
    );
}
