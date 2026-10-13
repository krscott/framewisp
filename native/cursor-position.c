/* Read the cursor hotspot on Framewisp's sole private output. */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <wayland-client.h>

#include "ext-image-copy-capture-v1-client-protocol.h"
#include "ext-image-capture-source-v1-client-protocol.h"

struct state {
    struct wl_output *output;
    struct wl_seat *seat;
    struct ext_image_copy_capture_manager_v1 *capture;
    struct ext_output_image_capture_source_manager_v1 *sources;
    unsigned outputs;
    unsigned seats;
    bool known;
    int32_t x;
    int32_t y;
};

static void global(void *data, struct wl_registry *registry, uint32_t name,
        char const *interface, uint32_t version) {
    struct state *s = data;
    (void)version;
    if (strcmp(interface, wl_output_interface.name) == 0) {
        if (s->outputs++ == 0) {
            s->output = wl_registry_bind(registry, name, &wl_output_interface, 1);
        }
    } else if (strcmp(interface, wl_seat_interface.name) == 0) {
        if (s->seats++ == 0) {
            s->seat = wl_registry_bind(registry, name, &wl_seat_interface, 1);
        }
    } else if (strcmp(interface, ext_image_copy_capture_manager_v1_interface.name) == 0) {
        s->capture = wl_registry_bind(registry, name,
                &ext_image_copy_capture_manager_v1_interface, 1);
    } else if (strcmp(interface, ext_output_image_capture_source_manager_v1_interface.name) == 0) {
        s->sources = wl_registry_bind(registry, name,
                &ext_output_image_capture_source_manager_v1_interface, 1);
    }
}

static void global_remove(void *data, struct wl_registry *registry, uint32_t name) {
    (void)data;
    (void)registry;
    (void)name;
}

static struct wl_registry_listener const registry_listener = {global, global_remove};

static void enter(void *data, struct ext_image_copy_capture_cursor_session_v1 *cursor) {
    (void)data;
    (void)cursor;
}

static void leave(void *data, struct ext_image_copy_capture_cursor_session_v1 *cursor) {
    struct state *s = data;
    (void)cursor;
    s->known = false;
}

static void position(void *data, struct ext_image_copy_capture_cursor_session_v1 *cursor,
        int32_t x, int32_t y) {
    struct state *s = data;
    (void)cursor;
    s->x = x;
    s->y = y;
    s->known = true;
}

static void hotspot(void *data, struct ext_image_copy_capture_cursor_session_v1 *cursor,
        int32_t x, int32_t y) {
    (void)data;
    (void)cursor;
    (void)x;
    (void)y;
}

static struct ext_image_copy_capture_cursor_session_v1_listener const cursor_listener = {
    enter, leave, position, hotspot
};

int main(int argc, char **argv) {
    if (argc != 2 || argv[1][0] != '/') {
        fprintf(stderr, "Expected the absolute private Wayland socket path.\n");
        return 1;
    }
    struct wl_display *display = wl_display_connect(argv[1]);
    if (display == NULL) {
        fprintf(stderr, "Cannot connect to the private Wayland display.\n");
        return 1;
    }

    struct state s = {0};
    struct wl_registry *registry = wl_display_get_registry(display);
    int rc = wl_registry_add_listener(registry, &registry_listener, &s);
    if (rc == 0 && wl_display_roundtrip(display) < 0) {
        rc = 1;
    }
    if (rc == 0 && (s.outputs != 1 || s.seats != 1 ||
            s.capture == NULL || s.sources == NULL)) {
        fprintf(stderr, "Expected one output and seat with cursor capture support.\n");
        rc = 1;
    }
    if (rc == 0) {
        struct wl_pointer *pointer = wl_seat_get_pointer(s.seat);
        struct ext_image_capture_source_v1 *source =
            ext_output_image_capture_source_manager_v1_create_source(s.sources, s.output);
        struct ext_image_copy_capture_cursor_session_v1 *cursor =
            ext_image_copy_capture_manager_v1_create_pointer_cursor_session(s.capture, source, pointer);
        rc = ext_image_copy_capture_cursor_session_v1_add_listener(cursor, &cursor_listener, &s);
        if (rc == 0 && wl_display_roundtrip(display) < 0) {
            rc = 1;
        }
        if (rc == 0 && s.known) {
            printf("%d %d\n", s.x, s.y);
        } else if (rc == 0) {
            /* Hidden cursors may not intersect the capture source. */
            printf("unavailable\n");
        }
    }
    if (rc != 0) {
        fprintf(stderr, "Failed to read the private compositor's cursor position.\n");
    }
    /* Disconnect destroys every proxy and compositor resource owned by this client. */
    wl_display_disconnect(display);
    return rc == 0 ? 0 : 1;
}
