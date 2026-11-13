/* oma-vclick: move the pointer to x,y (layout pixels) and left-click, through the
 * wlr virtual-pointer protocol (Hyprland supports it). No root, no uinput.
 * usage: oma-vclick X Y [--move-only] */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>
#include "wlr-virtual-pointer-unstable-v1-client-protocol.h"
#define BTN_LEFT 0x110
static struct zwlr_virtual_pointer_manager_v1 *mgr; static struct wl_seat *seat;
static struct wl_output *outs[16]; static int nouts;
static int ox[16], oy[16], ow[16], oh[16];
static void geo(void *d, struct wl_output *o, int32_t x, int32_t y, int32_t pw, int32_t ph, int32_t sp, const char *mk, const char *md, int32_t t) { int i=(int)(long)d; ox[i]=x; oy[i]=y; }
static void mode(void *d, struct wl_output *o, uint32_t f, int32_t w, int32_t h, int32_t r) { int i=(int)(long)d; if (f & WL_OUTPUT_MODE_CURRENT) { ow[i]=w; oh[i]=h; } }
static void done(void *d, struct wl_output *o) {} static void scale(void *d, struct wl_output *o, int32_t s) {}
static void oname(void *d, struct wl_output *o, const char *n) {} static void odesc(void *d, struct wl_output *o, const char *n) {}
static const struct wl_output_listener ol = { geo, mode, done, scale, oname, odesc };
static void reg(void *d, struct wl_registry *r, uint32_t name, const char *iface, uint32_t v) {
  if (!strcmp(iface, zwlr_virtual_pointer_manager_v1_interface.name)) mgr = wl_registry_bind(r, name, &zwlr_virtual_pointer_manager_v1_interface, 1);
  else if (!strcmp(iface, wl_seat_interface.name) && !seat) seat = wl_registry_bind(r, name, &wl_seat_interface, 1);
  else if (!strcmp(iface, wl_output_interface.name) && nouts < 16) { outs[nouts] = wl_registry_bind(r, name, &wl_output_interface, v < 4 ? v : 4); wl_output_add_listener(outs[nouts], &ol, (void*)(long)nouts); nouts++; }
}
static void unreg(void *d, struct wl_registry *r, uint32_t n) {}
static const struct wl_registry_listener rl = { reg, unreg };
static uint32_t ms(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec * 1000 + t.tv_nsec / 1000000; }
int main(int argc, char **argv) {
  if (argc < 3) { fprintf(stderr, "usage: oma-vclick X Y [--move-only]\n"); return 2; }
  int x = atoi(argv[1]), y = atoi(argv[2]); int move_only = argc > 3 && !strcmp(argv[3], "--move-only");
  struct wl_display *dpy = wl_display_connect(NULL); if (!dpy) { fprintf(stderr, "no wayland display\n"); return 1; }
  struct wl_registry *r = wl_display_get_registry(dpy); wl_registry_add_listener(r, &rl, NULL);
  wl_display_roundtrip(dpy); wl_display_roundtrip(dpy);
  if (!mgr) { fprintf(stderr, "compositor lacks zwlr_virtual_pointer_manager_v1\n"); return 1; }
  /* absolute motion spans the bounding box of all outputs */
  int minx = 1<<30, miny = 1<<30, maxx = -(1<<30), maxy = -(1<<30);
  for (int i = 0; i < nouts; i++) { if (ox[i] < minx) minx = ox[i]; if (oy[i] < miny) miny = oy[i]; if (ox[i]+ow[i] > maxx) maxx = ox[i]+ow[i]; if (oy[i]+oh[i] > maxy) maxy = oy[i]+oh[i]; }
  if (nouts == 0) { minx = miny = 0; maxx = 1920; maxy = 1080; }
  struct zwlr_virtual_pointer_v1 *p = zwlr_virtual_pointer_manager_v1_create_virtual_pointer(mgr, seat);
  zwlr_virtual_pointer_v1_motion_absolute(p, ms(), x - minx, y - miny, maxx - minx, maxy - miny);
  zwlr_virtual_pointer_v1_frame(p); wl_display_roundtrip(dpy); usleep(60000);
  if (!move_only) {
    zwlr_virtual_pointer_v1_button(p, ms(), BTN_LEFT, WL_POINTER_BUTTON_STATE_PRESSED); zwlr_virtual_pointer_v1_frame(p); wl_display_roundtrip(dpy); usleep(40000);
    zwlr_virtual_pointer_v1_button(p, ms(), BTN_LEFT, WL_POINTER_BUTTON_STATE_RELEASED); zwlr_virtual_pointer_v1_frame(p); wl_display_roundtrip(dpy);
  }
  usleep(30000); zwlr_virtual_pointer_v1_destroy(p); wl_display_roundtrip(dpy); wl_display_disconnect(dpy); return 0;
}
