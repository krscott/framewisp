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
            ];
            venvDir = ".venv";
            postVenvCreation = ''
              pip install -e '.[dev]'
            '';
            shellHook = ''
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
