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
  bash,
  xwayland,
  xdotool,
  xmodmap,
  ffmpeg,
  gst_all_1,
  pipewire,
  makeFontsConf,
  dejavu_fonts,
  noto-fonts,
  noto-fonts-cjk-sans,
  noto-fonts-monochrome-emoji,
}:
let
  captionFonts = makeFontsConf {
    fontDirectories = [
      dejavu_fonts
      noto-fonts
      noto-fonts-cjk-sans
      noto-fonts-monochrome-emoji
    ];
  };
  capturePlugins = lib.makeSearchPath "lib/gstreamer-1.0" [
    (lib.getLib gst_all_1.gstreamer)
    pipewire
    gst_all_1.gst-plugins-base
    gst_all_1.gst-plugins-good
  ];
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
  buildInputs = [
    gtk4
    gst_all_1.gstreamer
  ];

  dontWrapGApps = true;
  preFixup = ''
    makeWrapperArgs+=(
      "''${gappsWrapperArgs[@]}"
      --set FRAMEWISP_FONTCONFIG_FILE "${captionFonts}"
      --prefix GST_PLUGIN_SYSTEM_PATH_1_0 : "${capturePlugins}"
      --prefix PATH : "$out/bin:${
        lib.makeBinPath [
          sway-unwrapped
          wayvnc
          grim
          wf-recorder
          wtype
          bash
          xwayland
          xdotool
          xmodmap
          ffmpeg
          gst_all_1.gstreamer
          vncdotool
        ]
      }"
    )
  '';

  passthru = { inherit captionFonts capturePlugins; };

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
