"""Define credential, identity and access contracts shared by CLI, API and UI."""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, StringConstraints

Role = Literal["admin", "viewer"]


PropertyId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


Username = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9_.-]{3,64}$")
]


class CreateUserData(BaseModel):
    """Validate local account creation independently of HTTP or CLI input."""

    username: Username
    password: SecretStr = Field(min_length=12, max_length=1024)
    role: Role
    property_ids: list[PropertyId] = Field(default_factory=list)


@dataclass(frozen=True)
class Identity:
    """A current user identity with a freshly read set of external property IDs."""

    id: int
    username: str
    role: str
    property_ids: frozenset[str]


@dataclass(frozen=True)
class AccessScope:
    """Represent all objects as None, and explicit viewer denial as an empty tuple."""

    user: Identity
    property_ids: tuple[str, ...] | None


class LoginInput(BaseModel):
    """Receive local credentials without including the password in model repr."""

    username: Username = Field(description="Local account login")
    password: SecretStr = Field(min_length=1, max_length=1024, description="Local account password")


class MessageResponse(BaseModel):
    """A public result that contains no session token or internal account hash."""

    message: str


class ScopeResponse(BaseModel):
    """Make unrestricted admin and empty viewer access distinguishable."""

    all_properties: bool = Field(description="True only for an unfiltered administrator")
    property_ids: list[str] = Field(description="Allowed IDs; empty viewer list means no access")


class CurrentUserResponse(BaseModel):
    """Expose only the current identity and the server-authorized selection."""

    id: int
    username: str
    role: str
    access: ScopeResponse
