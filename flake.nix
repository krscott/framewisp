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
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
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

        pythonDev = pkgs.python3.pkgs.py-start.pythonModule.withPackages (
          ps:
          with ps;
          [
            black
            isort
            mypy
            pytest
          ]
          ++ pkgs.py-start.propagatedBuildInputs
          ++ pkgs.py-start.nativeBuildInputs
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
          inherit (pkgs) py-start;
          default = pkgs.py-start;
        };

        devShells = {
          default = pkgs.mkShell {
            inputsFrom = [ pkgs.py-start ];
            nativeBuildInputs = [
              pythonDev
              pkgs.pyright
              pkgs.nodejs # For missing libatomic in some environments
            ];
            packages = [ pkgs.python3.pkgs.venvShellHook ];
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
          lint = mkApp "just lint";
        };

        formatter = pkgs.nixfmt;
      }
    );
}
