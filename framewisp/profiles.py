"""Disposable app preferences for opt-in, repeatable headless runs."""

from pathlib import Path

from framewisp.errors import SessionError


def fresh_environment(runtime: Path, env: dict[str, str]) -> dict[str, str]:
    """Create settings under the owned runtime, before starting any children."""
    fonts = env.get("FRAMEWISP_PROFILE_FONTCONFIG_FILE")
    if not fonts or not Path(fonts).is_file():
        raise SessionError(
            "--profile fresh requires FRAMEWISP_PROFILE_FONTCONFIG_FILE. "
            "Use the Nix package or nix develop for bundled fonts."
        )
    root = runtime / "profile"
    for name in ("home", "config", "cache", "data", "state", "tmp"):
        (root / name).mkdir(parents=True, mode=0o700)
    result = {
        key: value
        for key, value in env.items()
        if not key.startswith(("LC_", "QT_", "GTK_", "GDK_", "XKB_"))
        and key
        not in {
            "LANGUAGE",
            "FONTCONFIG_PATH",
            "FONTCONFIG_SYSROOT",
            "FC_LANG",
            "GSETTINGS_SCHEMA_DIR",
            "DCONF_PROFILE",
            "XENVIRONMENT",
            "RESOURCE_MANAGER",
            "XCURSOR_PATH",
        }
    }
    result.update(
        HOME=str(root / "home"),
        XDG_CONFIG_HOME=str(root / "config"),
        XDG_CACHE_HOME=str(root / "cache"),
        XDG_DATA_HOME=str(root / "data"),
        XDG_STATE_HOME=str(root / "state"),
        XDG_CONFIG_DIRS=str(root / "config"),
        TMPDIR=str(root / "tmp"),
        LANG="C.UTF-8",
        LC_ALL="C.UTF-8",
        FONTCONFIG_FILE=fonts,
        FRAMEWISP_FONTCONFIG_FILE=fonts,
        FRAMEWISP_APP_PROFILE="fresh",
        GSETTINGS_BACKEND="keyfile",
        GTK_THEME="Adwaita",
        GDK_BACKEND="wayland",
        GDK_SCALE="1",
        GDK_DPI_SCALE="1",
        GTK_IM_MODULE="gtk-im-context-simple",
        GTK_OVERLAY_SCROLLING="0",
        QT_QPA_PLATFORM="wayland",
        QT_STYLE_OVERRIDE="Fusion",
        QT_QUICK_BACKEND="software",
        QT_FONT_DPI="96",
        QT_SCALE_FACTOR="1",
        QT_AUTO_SCREEN_SCALE_FACTOR="0",
        QT_ENABLE_HIGHDPI_SCALING="0",
        QT_IM_MODULE="",
        XMODIFIERS="",
        XCURSOR_THEME="Adwaita",
        XCURSOR_SIZE="24",
    )
    # Keep Nix's plugin lookup paths; they select installed toolkit binaries.
    for key in ("QT_PLUGIN_PATH", "QML_IMPORT_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        if key in env:
            result[key] = env[key]
    settings = (
        "[Settings]\n"
        "gtk-theme-name=Adwaita\n"
        "gtk-icon-theme-name=Adwaita\n"
        "gtk-font-name=DejaVu Sans 11\n"
        "gtk-xft-dpi=98304\n"
        "gtk-application-prefer-dark-theme=false\n"
        "gtk-enable-animations=false\n"
    )
    for version in ("3.0", "4.0"):
        directory = root / "config" / f"gtk-{version}"
        directory.mkdir(mode=0o700)
        (directory / "settings.ini").write_text(settings)
    return result
