"""
Aim Practice - Simple (Part 2)
Pick Easy / Normal / Hard, then click the rectangles before they vanish.
Keys: 1/2/3 or click = choose mode, M = back to menu, R = reset, ESC = quit.
Install:  pip install pygame-ce
Run:      python aim_practice_simple.py
"""
import random
import sys
import pygame

# ============================ SETTINGS ============================
WINDOW_WIDTH = 1000
WINDOW_HEIGHT = 700
FPS = 60

TARGET_WIDTH = 80          # rectangle size (pixels)
TARGET_HEIGHT = 80

# How long each rectangle stays on screen (seconds)
EASY_TIME = 2.0
NORMAL_TIME = 1.2
HARD_TIME = 0.6

HUD_HEIGHT = 60            # top bar for score / misses / accuracy
TIMER_BAR_HEIGHT = 8       # thin timer bar under each rectangle's top edge

COLOR_BACKGROUND = (30, 30, 36)
COLOR_HUD = (22, 22, 28)
COLOR_TARGET = (230, 70, 70)
COLOR_TIMER = (250, 220, 90)
COLOR_TIMER_BG = (90, 40, 40)
COLOR_TEXT = (240, 240, 240)
COLOR_BUTTON = (70, 70, 90)
COLOR_BUTTON_HOVER = (95, 95, 125)
# ==================================================================

MODES = [("Easy", EASY_TIME), ("Normal", NORMAL_TIME), ("Hard", HARD_TIME)]


def random_target():
    """Return a rectangle fully inside the play area (below the HUD bar)."""
    x = random.randint(0, WINDOW_WIDTH - TARGET_WIDTH)
    y = random.randint(HUD_HEIGHT, WINDOW_HEIGHT - TARGET_HEIGHT)
    return pygame.Rect(x, y, TARGET_WIDTH, TARGET_HEIGHT)


def main():
    pygame.init()
    screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT))
    clock = pygame.time.Clock()
    title_font = pygame.font.SysFont("arial", 56, bold=True)
    font = pygame.font.SysFont("arial", 28)
    small_font = pygame.font.SysFont("arial", 20)

    # Menu buttons, stacked in the middle of the window
    buttons = []
    for i, (name, secs) in enumerate(MODES):
        rect = pygame.Rect(0, 0, 260, 60)
        rect.center = (WINDOW_WIDTH // 2, 280 + i * 90)
        buttons.append((rect, name, secs))

    state = "menu"          # "menu" or "play"
    mode_name, lifetime = "", 1.0
    score = misses = 0
    target = None
    spawn_time = 0

    def reset_stats():
        nonlocal score, misses
        score = misses = 0

    def spawn():
        nonlocal target, spawn_time
        target = random_target()
        spawn_time = pygame.time.get_ticks()

    def start_mode(name, secs):
        nonlocal state, mode_name, lifetime
        mode_name, lifetime = name, secs
        reset_stats()
        spawn()
        state = "play"
        pygame.display.set_caption(f"Aim Practice - Simple ({name})")

    pygame.display.set_caption("Aim Practice - Simple")

    running = True
    while running:
        # ------------------------- Input -------------------------
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_m:
                    state = "menu"
                    pygame.display.set_caption("Aim Practice - Simple")
                elif state == "menu" and event.key in (pygame.K_1, pygame.K_2, pygame.K_3):
                    _, name, secs = buttons[event.key - pygame.K_1]
                    start_mode(name, secs)
                elif state == "play" and event.key == pygame.K_r:
                    reset_stats()
                    spawn()
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                if state == "menu":
                    for rect, name, secs in buttons:
                        if rect.collidepoint(event.pos):
                            start_mode(name, secs)
                elif target.collidepoint(event.pos):
                    score += 1
                    spawn()
                # clicking and missing does not change anything

        # ------------------------- Update -------------------------
        if state == "play":
            elapsed = (pygame.time.get_ticks() - spawn_time) / 1000
            if elapsed >= lifetime:       # rectangle vanished on its own
                misses += 1
                spawn()
                elapsed = 0

        # ------------------------- Drawing -------------------------
        screen.fill(COLOR_BACKGROUND)

        if state == "menu":
            title = title_font.render("Aim Practice", True, COLOR_TEXT)
            screen.blit(title, title.get_rect(center=(WINDOW_WIDTH // 2, 150)))
            sub = font.render("Choose a mode", True, COLOR_TEXT)
            screen.blit(sub, sub.get_rect(center=(WINDOW_WIDTH // 2, 210)))
            mouse = pygame.mouse.get_pos()
            for i, (rect, name, secs) in enumerate(buttons):
                color = COLOR_BUTTON_HOVER if rect.collidepoint(mouse) else COLOR_BUTTON
                pygame.draw.rect(screen, color, rect, border_radius=8)
                label = font.render(f"{i + 1}. {name}  ({secs}s)", True, COLOR_TEXT)
                screen.blit(label, label.get_rect(center=rect.center))
            hint = small_font.render("Press 1 / 2 / 3 or click  |  ESC to quit", True, COLOR_TEXT)
            screen.blit(hint, hint.get_rect(center=(WINDOW_WIDTH // 2, WINDOW_HEIGHT - 40)))
        else:
            # Target with a thin timer bar across its top that shrinks over time
            pygame.draw.rect(screen, COLOR_TARGET, target)
            frac = max(0.0, 1 - elapsed / lifetime)
            bar_bg = pygame.Rect(target.x, target.y, target.width, TIMER_BAR_HEIGHT)
            pygame.draw.rect(screen, COLOR_TIMER_BG, bar_bg)
            pygame.draw.rect(screen, COLOR_TIMER,
                             (target.x, target.y, int(target.width * frac), TIMER_BAR_HEIGHT))

            # HUD
            pygame.draw.rect(screen, COLOR_HUD, (0, 0, WINDOW_WIDTH, HUD_HEIGHT))
            total = score + misses
            accuracy = (score / total * 100) if total else 0
            text = f"{mode_name}   Score: {score}   Misses: {misses}   Accuracy: {accuracy:.0f}%"
            surf = font.render(text, True, COLOR_TEXT)
            screen.blit(surf, (20, (HUD_HEIGHT - surf.get_height()) // 2))
            hint = small_font.render("M = menu   R = reset   ESC = quit", True, COLOR_TEXT)
            screen.blit(hint, (WINDOW_WIDTH - hint.get_width() - 15,
                               (HUD_HEIGHT - hint.get_height()) // 2))

        pygame.display.flip()
        clock.tick(FPS)

    pygame.quit()
    sys.exit()


if __name__ == "__main__":
    main()
