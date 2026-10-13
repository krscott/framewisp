{
  stdenv,
  pkg-config,
  wayland,
  wayland-scanner,
  wayland-protocols,
}:
stdenv.mkDerivation {
  name = "framewisp-cursor-position";
  src = ./native;
  nativeBuildInputs = [
    pkg-config
    wayland-scanner
  ];
  buildInputs = [ wayland ];
  buildPhase = ''
    for protocol in ext-image-copy-capture ext-image-capture-source ext-foreign-toplevel-list; do
      xml="${wayland-protocols}/share/wayland-protocols/staging/$protocol/$protocol-v1.xml"
      wayland-scanner client-header "$xml" "$protocol-v1-client-protocol.h"
      wayland-scanner private-code "$xml" "$protocol-v1-protocol.c"
    done
    $CC -std=c11 -Wall -Wextra -Werror -Werror=unused-result \
      cursor-position.c *-protocol.c $(pkg-config --cflags --libs wayland-client) \
      -o framewisp-cursor-position
  '';
  installPhase = ''
    install -Dm755 framewisp-cursor-position "$out/bin/framewisp-cursor-position"
  '';
}
