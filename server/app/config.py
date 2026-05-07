from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    api_token: str
    storage_roots: str = ""  # comma-separated paths
    db_path: str = "/data/flashpoint.db"
    host: str = "0.0.0.0"
    port: int = 8000

    @property
    def storage_root_list(self) -> list[str]:
        return [p.strip() for p in self.storage_roots.split(",") if p.strip()]


settings = Settings()
