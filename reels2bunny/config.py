import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "", *aliases: str) -> str:
    """Read an env var, falling back to alias names (e.g. INSTAGRAM_TOKEN)."""
    for key in (name, *aliases):
        val = os.getenv(key, "").strip()
        if val:
            return val
    return default


@dataclass
class Config:
    ig_access_token: str
    ig_user_id: str
    ig_api_base: str
    ig_api_version: str
    bunny_zone: str
    bunny_password: str
    bunny_region: str
    bunny_base_path: str
    bunny_api_key: str
    bunny_cdn_hostname: str

    @classmethod
    def from_env(cls) -> "Config":
        cdn = _env("BUNNY_CDN_HOSTNAME")
        cdn = cdn.replace("https://", "").replace("http://", "").strip("/")
        return cls(
            ig_access_token=_env("IG_ACCESS_TOKEN", "", "INSTAGRAM_TOKEN"),
            ig_user_id=_env("IG_USER_ID", "me", "INSTAGRAM_USER_ID"),
            ig_api_base=_env("IG_API_BASE", "https://graph.instagram.com").rstrip("/"),
            ig_api_version=_env("IG_API_VERSION", "", "INSTAGRAM_API_VERSION").strip("/"),
            bunny_cdn_hostname=cdn,
            bunny_zone=_env("BUNNY_STORAGE_ZONE"),
            bunny_password=_env("BUNNY_STORAGE_PASSWORD"),
            bunny_region=_env("BUNNY_STORAGE_REGION").lower(),
            bunny_base_path=_env("BUNNY_BASE_PATH", "").strip("/"),
            bunny_api_key=_env("BUNNY_API_KEY"),
        )

    def require(self, *fields: str) -> None:
        missing = [f.upper() for f in fields if not getattr(self, f)]
        if missing:
            from .errors import FatalError
            raise FatalError(f"Missing required config: {', '.join(missing)} (see .env.example)")
