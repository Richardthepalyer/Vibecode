"""
Aim Practice - Basic (Part 1)
Click the rectangle to score. R = reset, ESC = quit.
Install:  pip install pygame
Run:      python aim_practice_basic.py
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

HUD_HEIGHT = 60            # top bar where the score is shown
BUTTON_WIDTH = 110         # Reset button size
BUTTON_HEIGHT = 36

COLOR_BACKGROUND = (30, 30, 36)
COLOR_HUD = (22, 22, 28)
COLOR_TARGET = (230, 70, 70)
COLOR_TEXT = (240, 240, 240)
COLOR_BUTTON = (70, 70, 90)
COLOR_BUTTON_HOVER = (95, 95, 125)
# ==================================================================


def random_target():
    """Return a rectangle fully inside the play area (below the HUD bar)."""
    x = random.randint(0, WINDOW_WIDTH - TARGET_WIDTH)
    y = random.randint(HUD_HEIGHT, WINDOW_HEIGHT - TARGET_HEIGHT)
    return pygame.Rect(x, y, TARGET_WIDTH, TARGET_HEIGHT)


def main():
    pygame.init()
    screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT))
    pygame.display.set_caption("Aim Practice - Basic")
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("arial", 28)
    small_font = pygame.font.SysFont("arial", 20)

    reset_button = pygame.Rect(WINDOW_WIDTH - BUTTON_WIDTH - 15,
                               (HUD_HEIGHT - BUTTON_HEIGHT) // 2,
                               BUTTON_WIDTH, BUTTON_HEIGHT)

    score = 0
    target = random_target()

    running = True
    while running:
        # ------------------------- Input -------------------------
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_r:
                    score = 0
                    target = random_target()
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                if reset_button.collidepoint(event.pos):
                    score = 0
                    target = random_target()
                elif target.collidepoint(event.pos):
                    score += 1               # hit: +1 and a new target
                    target = random_target()
                # a miss does nothing

        # ------------------------- Drawing -------------------------
        screen.fill(COLOR_BACKGROUND)
        pygame.draw.rect(screen, COLOR_TARGET, target)

        # HUD bar with score and reset button
        pygame.draw.rect(screen, COLOR_HUD, (0, 0, WINDOW_WIDTH, HUD_HEIGHT))
        score_text = font.render(f"Score: {score}", True, COLOR_TEXT)
        screen.blit(score_text, (20, (HUD_HEIGHT - score_text.get_height()) // 2))

        hovered = reset_button.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(screen, COLOR_BUTTON_HOVER if hovered else COLOR_BUTTON,
                         reset_button, border_radius=6)
        label = small_font.render("Reset (R)", True, COLOR_TEXT)
        screen.blit(label, label.get_rect(center=reset_button.center))

        pygame.display.flip()
        clock.tick(FPS)

    pygame.quit()
    sys.exit()


if __name__ == "__main__":
    main()
