"""
Aim Practice - 3D (Part 3)
First-person practice room using a simple 3D projection in pygame.
Mouse = look, WASD = move, left click = shoot, R = reset score,
F11 (or F) = fullscreen on/off, ESC = release mouse and quit.
The window is resizable: drag its edges or use the maximize button.
Install:  pip install pygame-ce
Run:      python aim_practice_3d.py
"""
import math
import random
import sys
import pygame

# ============================ SETTINGS ============================
WINDOW_WIDTH = 1100          # starting (windowed) size
WINDOW_HEIGHT = 700
START_FULLSCREEN = False     # True = open in fullscreen
FPS = 60

MOUSE_SENSITIVITY = 0.18     # degrees of turn per pixel of mouse movement
FIELD_OF_VIEW = 90           # horizontal field of view in degrees
EYE_HEIGHT = 1.7             # camera height above the floor
MOVE_SPEED = 6.0             # WASD speed in units per second

NUM_TARGETS = 1              # how many rectangles are in the room at once
TARGET_WIDTH = 1.4           # rectangle size in 3D units
TARGET_HEIGHT = 1.0
TARGET_MIN_DIST = 6          # random distance range from you (when spawning)
TARGET_MAX_DIST = 22
TARGET_SPREAD_ANGLE = 55     # spawns within +/- this many degrees of where you look
TARGET_MIN_Y = 0.8           # random height range
TARGET_MAX_Y = 6.0

# Room size (you start at x=0, z=0)
ROOM_X = (-14, 14)
ROOM_Y = (0, 8)
ROOM_Z = (-10, 26)
GRID_STEP = 2
WALL_MARGIN = 0.6            # how close you can get to a wall

COLOR_BACKGROUND = (14, 14, 20)
COLOR_FLOOR = (60, 75, 100)
COLOR_WALL = (40, 46, 62)
COLOR_EDGE = (110, 120, 150)
COLOR_TARGET = (230, 70, 70)
COLOR_TARGET_EDGE = (255, 200, 200)
COLOR_CROSSHAIR = (80, 255, 120)
COLOR_TEXT = (240, 240, 240)
CROSSHAIR_SIZE = 10
# ==================================================================

NEAR = 0.05                  # near clipping plane


class Camera:
    """Player position, view angles and screen size."""

    def __init__(self):
        self.x = 0.0
        self.z = 0.0
        self.yaw = 0.0       # radians; 0 looks along +z
        self.pitch = 0.0
        self.set_size(WINDOW_WIDTH, WINDOW_HEIGHT)

    def set_size(self, w, h):
        self.w, self.h = w, h
        self.cx, self.cy = w / 2, h / 2
        self.focal = self.cx / math.tan(math.radians(FIELD_OF_VIEW) / 2)

    def to_camera(self, p):
        """World point -> camera space (x right, y up, z forward)."""
        x = p[0] - self.x
        y = p[1] - EYE_HEIGHT
        z = p[2] - self.z
        cy, sy = math.cos(self.yaw), math.sin(self.yaw)
        x1 = x * cy - z * sy
        z1 = x * sy + z * cy
        cp, sp = math.cos(self.pitch), math.sin(self.pitch)
        return (x1, y * cp - z1 * sp, y * sp + z1 * cp)

    def project(self, c):
        """Camera-space point (z > 0) -> screen coordinates."""
        return (self.cx + c[0] / c[2] * self.focal,
                self.cy - c[1] / c[2] * self.focal)


# --------------------------- Room geometry ---------------------------
def build_room():
    """Return a list of (point_a, point_b, color) line segments for the room."""
    x0, x1 = ROOM_X
    y0, y1 = ROOM_Y
    z0, z1 = ROOM_Z
    lines = []

    def frange(a, b, step):
        v = a
        while v <= b + 1e-9:
            yield v
            v += step

    for z in frange(z0, z1, GRID_STEP):                       # floor
        lines.append(((x0, y0, z), (x1, y0, z), COLOR_FLOOR))
    for x in frange(x0, x1, GRID_STEP):
        lines.append(((x, y0, z0), (x, y0, z1), COLOR_FLOOR))
    for z in (z0, z1):                                        # front/back walls
        for x in frange(x0, x1, GRID_STEP):
            lines.append(((x, y0, z), (x, y1, z), COLOR_WALL))
        for y in frange(y0, y1, GRID_STEP):
            lines.append(((x0, y, z), (x1, y, z), COLOR_WALL))
    for x in (x0, x1):                                        # side walls
        for z in frange(z0, z1, GRID_STEP):
            lines.append(((x, y0, z), (x, y1, z), COLOR_WALL))
        for y in frange(y0, y1, GRID_STEP):
            lines.append(((x, y, z0), (x, y, z1), COLOR_WALL))
    for a, b in [((x0, y1, z0), (x1, y1, z0)), ((x0, y1, z1), (x1, y1, z1)),
                 ((x0, y1, z0), (x0, y1, z1)), ((x1, y1, z0), (x1, y1, z1))]:
        lines.append((a, b, COLOR_EDGE))                      # ceiling edges
    for x in (x0, x1):                                        # corner edges
        for z in (z0, z1):
            lines.append(((x, y0, z), (x, y1, z), COLOR_EDGE))
    return lines


def draw_line_3d(surface, cam, a, b, color):
    """Draw a 3D line with near-plane clipping."""
    ca, cb = cam.to_camera(a), cam.to_camera(b)
    if ca[2] < NEAR and cb[2] < NEAR:
        return
    if ca[2] < NEAR:
        t = (NEAR - ca[2]) / (cb[2] - ca[2])
        ca = tuple(ca[i] + (cb[i] - ca[i]) * t for i in range(3))
    elif cb[2] < NEAR:
        t = (NEAR - cb[2]) / (ca[2] - cb[2])
        cb = tuple(cb[i] + (ca[i] - cb[i]) * t for i in range(3))
    pygame.draw.line(surface, color, cam.project(ca), cam.project(cb), 1)


# --------------------------- Targets ---------------------------
def new_target(cam):
    """A rectangle at a random spot in front of the player, facing them."""
    x0, x1 = ROOM_X
    z0, z1 = ROOM_Z
    for _ in range(60):
        ang = cam.yaw + math.radians(random.uniform(-TARGET_SPREAD_ANGLE, TARGET_SPREAD_ANGLE))
        dist = random.uniform(TARGET_MIN_DIST, TARGET_MAX_DIST)
        x = cam.x + math.sin(ang) * dist
        z = cam.z + math.cos(ang) * dist
        if x0 + 1 <= x <= x1 - 1 and z0 + 1 <= z <= z1 - 1:
            break
    else:                                    # fallback: any spot in the room
        x = random.uniform(x0 + 1, x1 - 1)
        z = random.uniform(z0 + 1, z1 - 1)
    # Unit vector pointing to the target's right, as seen from the player
    fx, fz = x - cam.x, z - cam.z
    length = math.hypot(fx, fz) or 1.0
    return {"x": x, "z": z, "y": random.uniform(TARGET_MIN_Y, TARGET_MAX_Y),
            "rx": fz / length, "rz": -fx / length}


def target_screen_quad(t, cam):
    """4 projected corners of a target, or None if not fully in front of you."""
    hw, hh = TARGET_WIDTH / 2, TARGET_HEIGHT / 2
    ox, oz = t["rx"] * hw, t["rz"] * hw
    corners = [(t["x"] - ox, t["y"] + hh, t["z"] - oz), (t["x"] + ox, t["y"] + hh, t["z"] + oz),
               (t["x"] + ox, t["y"] - hh, t["z"] + oz), (t["x"] - ox, t["y"] - hh, t["z"] - oz)]
    cams = [cam.to_camera(c) for c in corners]
    if any(c[2] < NEAR for c in cams):
        return None
    return [cam.project(c) for c in cams]


def point_in_quad(px, py, quad):
    """True if the point is inside a convex quad (same-sign cross products)."""
    sign = 0
    for i in range(4):
        x1, y1 = quad[i]
        x2, y2 = quad[(i + 1) % 4]
        cross = (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)
        if cross != 0:
            s = 1 if cross > 0 else -1
            if sign == 0:
                sign = s
            elif s != sign:
                return False
    return True


# --------------------------- Window helpers ---------------------------
def make_window(fullscreen):
    """Create the window (fullscreen or resizable) and lock the mouse to it."""
    if fullscreen:
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    else:
        screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT), pygame.RESIZABLE)
    pygame.display.set_caption("Aim Practice - 3D")
    pygame.mouse.set_visible(False)
    pygame.event.set_grab(True)
    pygame.mouse.get_rel()                   # clear queued mouse movement
    return screen


def main():
    pygame.init()
    fullscreen = START_FULLSCREEN
    screen = make_window(fullscreen)
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("arial", 28)
    small_font = pygame.font.SysFont("arial", 18)

    cam = Camera()
    cam.set_size(*screen.get_size())
    room = build_room()
    targets = [new_target(cam) for _ in range(NUM_TARGETS)]
    score = 0
    max_pitch = math.radians(89)

    running = True
    while running:
        dt = clock.tick(FPS) / 1000.0

        # ------------------------- Input -------------------------
        shoot = False
        toggle_fs = False
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_r:
                    score = 0
                    targets = [new_target(cam) for _ in range(NUM_TARGETS)]
                elif event.key in (pygame.K_F11, pygame.K_f):
                    toggle_fs = True
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                shoot = True

        if toggle_fs:
            fullscreen = not fullscreen
            screen = make_window(fullscreen)
            shoot = False

        # Keep the camera matched to the current window size (resize / fullscreen)
        if screen.get_size() != (cam.w, cam.h):
            cam.set_size(*screen.get_size())

        # Mouse look
        dx, dy = pygame.mouse.get_rel()
        cam.yaw += math.radians(dx * MOUSE_SENSITIVITY)
        cam.pitch -= math.radians(dy * MOUSE_SENSITIVITY)
        cam.pitch = max(-max_pitch, min(max_pitch, cam.pitch))

        # WASD movement on the floor, relative to where you look
        keys = pygame.key.get_pressed()
        fwd = (keys[pygame.K_w] or keys[pygame.K_UP]) - (keys[pygame.K_s] or keys[pygame.K_DOWN])
        side = (keys[pygame.K_d] or keys[pygame.K_RIGHT]) - (keys[pygame.K_a] or keys[pygame.K_LEFT])
        if fwd or side:
            length = math.hypot(fwd, side)           # no faster on diagonals
            fwd, side = fwd / length, side / length
            sy, cy = math.sin(cam.yaw), math.cos(cam.yaw)
            cam.x += (sy * fwd + cy * side) * MOVE_SPEED * dt
            cam.z += (cy * fwd - sy * side) * MOVE_SPEED * dt
            cam.x = max(ROOM_X[0] + WALL_MARGIN, min(ROOM_X[1] - WALL_MARGIN, cam.x))
            cam.z = max(ROOM_Z[0] + WALL_MARGIN, min(ROOM_Z[1] - WALL_MARGIN, cam.z))

        # Shooting: is the screen center inside a target? Pick the closest one.
        quads = [target_screen_quad(t, cam) for t in targets]
        if shoot:
            best, best_dist = None, 0
            for i, quad in enumerate(quads):
                if quad and point_in_quad(cam.cx, cam.cy, quad):
                    d = math.hypot(targets[i]["x"] - cam.x, targets[i]["z"] - cam.z)
                    if best is None or d < best_dist:
                        best, best_dist = i, d
            if best is not None:
                score += 1
                targets[best] = new_target(cam)
                quads[best] = target_screen_quad(targets[best], cam)

        # ------------------------- Drawing -------------------------
        screen.fill(COLOR_BACKGROUND)
        for a, b, color in room:
            draw_line_3d(screen, cam, a, b, color)
        for quad in quads:
            if quad:
                pygame.draw.polygon(screen, COLOR_TARGET, quad)
                pygame.draw.polygon(screen, COLOR_TARGET_EDGE, quad, 2)

        cx, cy = int(cam.cx), int(cam.cy)             # crosshair fixed in the center
        pygame.draw.line(screen, COLOR_CROSSHAIR, (cx - CROSSHAIR_SIZE, cy), (cx + CROSSHAIR_SIZE, cy), 2)
        pygame.draw.line(screen, COLOR_CROSSHAIR, (cx, cy - CROSSHAIR_SIZE), (cx, cy + CROSSHAIR_SIZE), 2)

        screen.blit(font.render(f"Score: {score}", True, COLOR_TEXT), (20, 15))
        hint = small_font.render(
            f"Sensitivity: {MOUSE_SENSITIVITY}   WASD = move   F11 = fullscreen   R = reset   ESC = quit",
            True, COLOR_TEXT)
        screen.blit(hint, (20, cam.h - 30))

        pygame.display.flip()

    pygame.event.set_grab(False)
    pygame.mouse.set_visible(True)
    pygame.quit()
    sys.exit()


if __name__ == "__main__":
    main()