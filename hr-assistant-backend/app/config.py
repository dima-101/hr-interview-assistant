from functools import lru_cache
from pathlib import Path
import os

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

load_dotenv(dotenv_path=str(ENV_FILE), override=True)


class Settings:
    def __init__(self):
        self.hr_username = os.getenv("HR_USERNAME", "").replace("\ufeff", "").strip()
        self.hr_password = os.getenv("HR_PASSWORD", "").strip()
        self.session_secret_key = os.getenv("SESSION_SECRET_KEY", "").strip()

        if not self.hr_username:
            raise ValueError("Не найден HR_USERNAME в .env")

        if not self.hr_password:
            raise ValueError("Не найден HR_PASSWORD в .env")

        if not self.session_secret_key:
            raise ValueError("Не найден SESSION_SECRET_KEY в .env")


@lru_cache
def get_settings():
    return Settings()