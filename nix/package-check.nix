{
  pkgs,
  workers ? 1,
  group ? null,
}:
pkgs.runCommand "framewisp-package-test-${toString workers}-${toString group}"
  {
    nativeBuildInputs = [ pkgs.gobject-introspection ];
    buildInputs = [
      pkgs.gtk4
      pkgs.libadwaita
    ];
  }
  ''
    export HOME="$TMPDIR"
    mkdir -p "$out"
    ${pkgs.coreutils}/bin/env -i \
      ${pkgs.framewisp}/bin/framewisp --agent-skill > agent-skill.md
    ${pkgs.diffutils}/bin/diff ${../framewisp/SKILL.md} agent-skill.md
    ${pkgs.coreutils}/bin/env -i \
      HOME="$HOME" \
      PATH="${pkgs.framewisp}/bin:${pkgs.ffmpeg}/bin:${pkgs.qt6.qtdeclarative}/bin:${pkgs.git}/bin" \
      QML_IMPORT_PATH="${pkgs.qt6.qtdeclarative}/lib/qt-6/qml" \
      QT_PLUGIN_PATH="${pkgs.qt6.qtbase}/lib/qt-6/plugins" \
      GI_TYPELIB_PATH="$GI_TYPELIB_PATH" \
      FRAMEWISP_TEST_GTK_APP="${pkgs.gtk4.dev}/bin/gtk4-demo" \
      FRAMEWISP_TEST_DCONF_MODULES="${pkgs.dconf.lib}/lib/gio/modules" \
      FRAMEWISP_TEST_DCONF_SERVICE="${pkgs.dconf}/share/dbus-1/services/ca.desrt.dconf.service" \
      FRAMEWISP_TEST_GTK_SCHEMAS="${pkgs.gtk4}/share/gsettings-schemas/${pkgs.gtk4.name}/glib-2.0/schemas" \
      FRAMEWISP_TEST_ISOLATED_PYTHON=1 \
      ${
        (pkgs.python3.withPackages (ps: [
          ps.pytest
          ps.pytest-xdist
          ps.pillow
          ps.pygobject3
        ]))
      }/bin/python \
      -m pytest -c ${../pyproject.toml} ${../tests}/test_profiles.py ${../tests}/test_integration.py ${../tests}/test_python_environment.py ${
        pkgs.lib.optionalString (workers > 1) "-n ${toString workers} --dist worksteal"
      } ${
        pkgs.lib.optionalString (group != null) "--test-shards 8 --test-shard ${toString group}"
      } --rootdir ${../tests} --durations=0 --junitxml "$out/junit.xml" --basetemp "$TMPDIR/tests"
  ''
