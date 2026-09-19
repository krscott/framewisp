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
  ffmpeg,
  makeFontsConf,
  noto-fonts,
  noto-fonts-cjk-sans,
  noto-fonts-monochrome-emoji,
}:
let
  captionFonts = makeFontsConf {
    fontDirectories = [
      noto-fonts
      noto-fonts-cjk-sans
      noto-fonts-monochrome-emoji
    ];
  };
in
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
      --set FRAMEWISP_FONTCONFIG_FILE "${captionFonts}"
      --prefix PATH : "$out/bin:${
        lib.makeBinPath [
          sway-unwrapped
          wayvnc
          grim
          wf-recorder
          wtype
          ffmpeg
          vncdotool
        ]
      }"
    )
  '';

  passthru.captionFonts = captionFonts;

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
