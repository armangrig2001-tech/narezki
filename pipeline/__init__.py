"""Конвейер нарезок Twitch → вертикальные MP4 (GitHub Actions)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)  # narezchik.py, cam_detect.py, title_card.py в корне репозитория
