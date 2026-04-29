import os

from dotenv import load_dotenv


load_dotenv()


def resolve_database_url():
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return database_url

    app_env = os.getenv("APP_ENV", "").strip().lower()
    if app_env in {"local", "development", "test"}:
        return "sqlite:///instance/app.db"

    raise RuntimeError(
        f"DATABASE_URL must be set when APP_ENV is '{app_env or 'unset'}'"
    )


class Config:
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    @staticmethod
    def init_app(app):
        if not app.config.get("SQLALCHEMY_DATABASE_URI"):
            app.config["SQLALCHEMY_DATABASE_URI"] = resolve_database_url()
