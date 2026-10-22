"""Load typed configuration for the application and its optional services."""

from pathlib import Path
from typing import Literal

from environs import Env
from pydantic import AmqpDsn, AnyHttpUrl, BaseModel, Field, field_validator
from sqlalchemy import URL

BASE_DIR = Path(__file__).resolve().parents[2]


class AppConfig(BaseModel):
    """Application flags and the location of unchanged input files."""

    debug: bool = False
    api_key: str = Field(default="", repr=False)
    data_dir: Path = BASE_DIR / "data"
    readiness_timeout: float = Field(default=5, gt=0)


class AuthConfig(BaseModel):
    """Cookie lifetime and the trusted browser origin for state-changing requests."""

    origin: str = "http://localhost:8000"
    session_seconds: int = Field(default=28800, ge=60, le=2592000)
    cookie_name: Literal["villa_session"] = Field(default="villa_session", frozen=True)

    @field_validator("origin")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        """Accept only a complete HTTP origin without credentials or a path."""
        url = AnyHttpUrl(value)
        if url.username or url.password or url.query or url.fragment or url.path not in (None, "/"):
            raise ValueError("AUTH_ORIGIN must contain only scheme, host, and port")
        return str(url).rstrip("/")

    @property
    def secure_cookie(self) -> bool:
        """Require Secure cookies whenever the trusted origin uses HTTPS."""
        return self.origin.startswith("https://")


class DatabaseConfig(BaseModel):
    """PostgreSQL credentials and bounded connection timeouts."""

    database: str
    user: str
    password: str = Field(repr=False)
    host: str
    port: int = Field(ge=1, le=65535)
    timeout: float = Field(default=3, gt=0)

    def dsn(self) -> URL:
        """Build an asyncpg URL without parsing special password characters."""
        return URL.create(
            "postgresql+asyncpg",
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.database,
        )


class ClickHouseConfig(BaseModel):
    """HTTP connection settings for the independent analytics client."""

    database: str
    user: str
    password: str = Field(repr=False)
    host: str
    port: int = Field(ge=1, le=65535)
    secure: bool = False
    timeout: int = Field(default=3, gt=0)


class RedisConfig(BaseModel):
    """Retain optional framework settings without requiring a Redis service."""

    host: str | None = None
    port: int | None = None
    password: str | None = Field(default=None, repr=False)
    db: int = 0


class RabbitMQConfig(BaseModel):
    """Explicit opt-in configuration for the existing Taskiq transport."""

    enabled: bool = False
    user: str = "guest"
    password: str = Field(default="guest", repr=False)
    host: str = "localhost"
    port: int = Field(default=5672, ge=1, le=65535)
    vhost: str = "/"

    def dsn(self, vhost: str | None = None) -> str:
        """Build the AMQP URL only when the optional broker is enabled."""
        return str(
            AmqpDsn.build(
                scheme="amqp",
                username=self.user,
                password=self.password,
                host=self.host,
                port=self.port,
                path=vhost if vhost is not None else self.vhost,
            )
        )


class Settings(BaseModel):
    """Keep service configuration in the framework's typed container."""

    app: AppConfig
    db: DatabaseConfig
    clickhouse: ClickHouseConfig
    redis: RedisConfig
    rabbitmq: RabbitMQConfig
    auth: AuthConfig = Field(default_factory=AuthConfig)


def load_settings(env_file: Path | None = BASE_DIR / ".env") -> Settings:
    """Read environment overrides and resolve relative DATA_DIR from the root."""
    env = Env()
    if env_file is not None:
        env.read_env(env_file, recurse=False)
    data_dir = Path(env.str("DATA_DIR", "data"))
    if not data_dir.is_absolute():
        data_dir = BASE_DIR / data_dir
    return Settings(
        auth=AuthConfig(
            origin=env.str("AUTH_ORIGIN", "http://localhost:8000"),
            session_seconds=env.int("SESSION_SECONDS", 28800),
        ),
        app=AppConfig(
            debug=env.bool("APP_DEBUG", False),
            api_key=env.str("API_KEY", ""),
            data_dir=data_dir.resolve(),
            readiness_timeout=env.float("READINESS_TIMEOUT", 5),
        ),
        db=DatabaseConfig(
            database=env.str("DB_NAME", "finance"),
            user=env.str("DB_USER", "finance"),
            password=env.str("DB_PASSWORD", "change-me-local-postgres"),
            host=env.str("DB_HOST", "localhost"),
            port=env.int("DB_PORT", 55432),
            timeout=env.float("DB_TIMEOUT", 3),
        ),
        clickhouse=ClickHouseConfig(
            database=env.str("CH_DATABASE", "finance"),
            user=env.str("CH_USER", "finance"),
            password=env.str("CH_PASSWORD", "change-me-local-clickhouse"),
            host=env.str("CH_HOST", "localhost"),
            port=env.int("CH_PORT", 58123),
            secure=env.bool("CH_SECURE", False),
            timeout=env.int("CH_TIMEOUT", 3),
        ),
        redis=RedisConfig(
            host=env.str("REDIS_HOST", None),
            port=env.int("REDIS_PORT", None),
            password=env.str("REDIS_PASSWORD", None),
            db=env.int("REDIS_DB", 0),
        ),
        rabbitmq=RabbitMQConfig(
            enabled=env.bool("BROKER_ENABLED", False),
            user=env.str("AMQP_USER", "guest"),
            password=env.str("AMQP_PASSWORD", "guest"),
            host=env.str("AMQP_HOST", "localhost"),
            port=env.int("AMQP_PORT", 5672),
            vhost=env.str("AMQP_VHOST", "/"),
        ),
    )


settings: Settings = load_settings()
