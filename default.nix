{
  buildPythonPackage,
  lib,
  pytestCheckHook,
  setuptools,
  vncdotool,
  pygobject3,
  pygobject-stubs,
  pillow,
  wrapGAppsHook4,
  gobject-introspection,
  gtk4,
  sway-unwrapped,
  wayvnc,
  grim,
  wf-recorder,
  wtype,
}:
buildPythonPackage {
  name = "framewisp";
  src = lib.cleanSource ./.;
  pyproject = true;

  nativeBuildInputs = [
    setuptools
    wrapGAppsHook4
    gobject-introspection
  ];
  buildInputs = [ gtk4 ];

  dontWrapGApps = true;
  preFixup = ''
    makeWrapperArgs+=(
      "''${gappsWrapperArgs[@]}"
      --prefix PATH : "$out/bin:${
        lib.makeBinPath [
          sway-unwrapped
          wayvnc
          grim
          wf-recorder
          wtype
          vncdotool
        ]
      }"
    )
  '';

  propagatedBuildInputs = [
    vncdotool
    pygobject3
  ];

  nativeCheckInputs = [
    pytestCheckHook
    pygobject-stubs
    pillow
  ];

  # Skip integration tests during build (they require the installed executable)
  disabledTestMarks = [ "integration" ];

  # pythonImportsCheck = [ "framewisp" ];

  meta = {
    mainProgram = "framewisp";
    # description = "A short description of my application";
    # homepage = "https://github.com";
    license = lib.licenses.gpl3Only;
  };
}
