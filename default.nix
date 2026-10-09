{
  buildPythonPackage,
  python,
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
  dbus,
  at-spi2-core,
  sway-unwrapped,
  wayvnc,
  grim,
  wf-recorder,
  wtype,
  bash,
  git,
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
  profileFonts = makeFontsConf {
    fontDirectories = [
      dejavu_fonts
      noto-fonts
      noto-fonts-cjk-sans
      noto-fonts-monochrome-emoji
    ];
    impureFontDirectories = [ ];
    includes = [ ];
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
  # Nix adds the dependency paths to these scripts during fixup. Isolated mode
  # ignores the caller's Python settings without removing them from the app env.
  postInstall = ''
    sed -i '1s/$/ -I/' "$out/bin/framewisp" "$out/bin/framewisp-demo"
  '';
  preFixup = ''
    makeWrapperArgs+=(
      "''${gappsWrapperArgs[@]}"
      --set FRAMEWISP_FONTCONFIG_FILE "${captionFonts}"
      --set FRAMEWISP_PROFILE_FONTCONFIG_FILE "${profileFonts}"
      --set FRAMEWISP_ATSPI_REGISTRY "${at-spi2-core}/libexec/at-spi2-registryd"
      --prefix GST_PLUGIN_SYSTEM_PATH_1_0 : "${capturePlugins}"
      --prefix PATH : "$out/bin:${
        lib.makeBinPath [
          sway-unwrapped
          wayvnc
          grim
          wf-recorder
          wtype
          bash
          git
          dbus
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

  # Install outside Python fixup: this entry point must see the environment
  # before any generated wrapper changes it, including Python's own PATH prefix.
  postFixup = ''
    mv "$out/bin/framewisp" "$out/bin/.framewisp-runtime"
    cat > "$out/bin/framewisp" <<EOF
    #!${python.interpreter} -I
    import runpy
    import sys
    sys.argv = ["${placeholder "out"}/${python.sitePackages}/framewisp/_launch.py", "${placeholder "out"}/bin/.framewisp-runtime", *sys.argv[1:]]
    runpy.run_path(sys.argv[0], run_name="__main__")
    EOF
    chmod +x "$out/bin/framewisp"
  '';

  passthru = {
    inherit captionFonts profileFonts capturePlugins;
    atspiRegistry = "${at-spi2-core}/libexec/at-spi2-registryd";
  };

  propagatedBuildInputs = [
    vncdotool
    pygobject3
  ];

  nativeCheckInputs = [
    pytestCheckHook
    pygobject-stubs
    pillow
    ffmpeg
    dbus
    git
  ];

  preCheck = ''
    export FRAMEWISP_ATSPI_REGISTRY="${at-spi2-core}/libexec/at-spi2-registryd"
    export FRAMEWISP_FONTCONFIG_FILE="${captionFonts}"
  '';

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
