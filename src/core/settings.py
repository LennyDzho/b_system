"""
Application configuration settings.
"""

from pathlib import Path
from typing import Optional

from environs import Env
from pydantic import AmqpDsn, BaseModel, PostgresDsn, Field

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class AppConfig(BaseModel):
    """
    Application settings.
    """
    debug: bool
    api_key: str


class DatabaseConfig(BaseModel):
    """
    Database connection settings.
    """
    database: str
    user: str
    password: str
    host: str
    port: int

    def dsn(self) -> str:
        """
        Constructs the PostgreSQL DSN for asyncpg.
        """

        return str(
            PostgresDsn.build(
                scheme="postgresql+asyncpg",
                username=self.user,
                password=self.password,
                host=self.host,
                port=self.port,
                path=self.database,
            )
        )


class RedisConfig(BaseModel):
    """
    Redis connection settings.
    """
    host: Optional[str]
    port: Optional[int]
    password: Optional[str]
    db: int = Field(default=0)


class RabbitMQConfig(BaseModel):
    """
    RabbitMQ connection settings.
    """
    user: str
    password: str
    host: str
    port: int
    vhost: str

    def dsn(self, vhost: str | None = None) -> str:
        """
        Constructs the AMQP DSN for RabbitMQ.
        """
        vhost = vhost or self.vhost
        return str(
            AmqpDsn.build(
                scheme="amqp",
                username=self.user,
                password=self.password,
                host=self.host,
                port=self.port,
                path=vhost,
            )
        )


class Settings(BaseModel):
    """
    Application settings container.
    """
    app: AppConfig
    db: DatabaseConfig
    redis: RedisConfig
    rabbitmq: RabbitMQConfig


def load_settings() -> Settings:
    """
    Load settings from the .env file.
    """
    env_path = BASE_DIR / ".env"
    env = Env()

    env.read_env(env_path)

    return Settings(
        app=AppConfig(
            debug=env.bool("DEBUG"),
            api_key=env.str("API_KEY"),
        ),
        db=DatabaseConfig(
            database=env.str("DB_NAME"),
            user=env.str("DB_USER"),
            password=env.str("DB_PASSWORD"),
            host=env.str("DB_HOST"),
            port=env.int("DB_PORT"),
        ),
        redis=RedisConfig(
            host=env.str("REDIS_HOST", "localhost"),
            port=env.int("REDIS_PORT", 6379),
            password=env.str("REDIS_PASSWORD", None),
            db=env.int("REDIS_DB", 0),
        ),
        rabbitmq=RabbitMQConfig(
            user=env.str("AMQP_USER"),
            password=env.str("AMQP_PASSWORD"),
            host=env.str("AMQP_HOST"),
            port=env.int("AMQP_PORT"),
            vhost=env.str("AMQP_VHOST"),
        ),
    )

"""Global settings instance."""
settings: Settings = load_settings()
