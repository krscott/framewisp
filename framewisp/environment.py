"""Keep the caller's app environment separate from framewisp's runtime."""

import os
from pathlib import Path
from urllib.parse import quote_from_bytes

_caller_environment: dict[str, str] | None = None

_UNSANITIZED_PREFIXES = ("GDK_PIXBUF_",)
_SANITIZED_PREFIXES = ("LC_", "QT_", "GTK_", "GDK_", "XKB_")
_SANITIZED_NAMES = {
    "LANGUAGE",
    "FONTCONFIG_PATH",
    "FONTCONFIG_SYSROOT",
    "FC_LANG",
    "GSETTINGS_SCHEMA_DIR",
    "DCONF_PROFILE",
    "XENVIRONMENT",
    "RESOURCE_MANAGER",
    "XCURSOR_PATH",
    # Toolkits choose palettes and fonts from these desktop identity signals.
    "XDG_CURRENT_DESKTOP",
    "XDG_SESSION_DESKTOP",
    "DESKTOP_SESSION",
    "GNOME_DESKTOP_SESSION_ID",
    "KDE_FULL_SESSION",
    "KDE_SESSION_VERSION",
}
# Nix's plugin lookup paths select installed toolkit binaries, not appearance.
_TOOLKIT_LOOKUP_PATHS = (
    "QT_PLUGIN_PATH",
    "QML_IMPORT_PATH",
    "QT_QPA_PLATFORM_PLUGIN_PATH",
)
_GTK_SETTINGS = """\
[Settings]
gtk-theme-name=Adwaita
gtk-icon-theme-name=Adwaita
gtk-font-name=DejaVu Sans 11
gtk-xft-dpi=98304
gtk-application-prefer-dark-theme=false
gtk-enable-animations=false
"""


def unix_bus_address(path: Path) -> str:
    # D-Bus values escape filesystem bytes; unlike URI values, '~' must be escaped.
    escaped = quote_from_bytes(os.fsencode(path), safe="/").replace("~", "%7E")
    return f"unix:path={escaped}"


def initialize_environment() -> None:
    """Consume and close the launcher descriptor before starting any children."""
    global _caller_environment
    descriptor = os.environ.pop("FRAMEWISP_CALLER_ENV_FD", None)
    if descriptor is None:
        return
    with os.fdopen(int(descriptor), "rb") as snapshot:
        entries = snapshot.read().split(b"\0")
    _caller_environment = {}
    for entry in entries:
        if entry:
            key, value = entry.split(b"=", 1)
            _caller_environment[os.fsdecode(key)] = os.fsdecode(value)
    _caller_environment.pop("FRAMEWISP_CALLER_ENV_FD", None)


def caller_environment() -> dict[str, str]:
    """Editable installs have no wrapper, so their current environment is the caller's."""
    return dict(os.environ if _caller_environment is None else _caller_environment)


def sanitized_environment(
    runtime: Path, env: dict[str, str], *, fonts: str
) -> dict[str, str]:
    """Fix everything that changes app output, keeping lookup paths and app settings.

    Creates private app directories under the owned runtime, so call this before
    starting any children.
    """
    root = runtime / "profile"
    for name in ("home", "config", "cache", "data", "state", "tmp"):
        (root / name).mkdir(parents=True, mode=0o700)
    result = {
        key: value
        for key, value in env.items()
        if key.startswith(_UNSANITIZED_PREFIXES)
        or not (key.startswith(_SANITIZED_PREFIXES) or key in _SANITIZED_NAMES)
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
        TZ="UTC",
        FONTCONFIG_FILE=fonts,
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
    for key in _TOOLKIT_LOOKUP_PATHS:
        if key in env:
            result[key] = env[key]
    for version in ("3.0", "4.0"):
        directory = root / "config" / f"gtk-{version}"
        directory.mkdir(mode=0o700)
        (directory / "settings.ini").write_text(_GTK_SETTINGS)
    return result
