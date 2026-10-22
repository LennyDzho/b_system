"""Implement local credentials, opaque sessions, and explicit property scopes."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from pwdlib import PasswordHash
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from src.api.v1.auth.schemas import AccessScope, CreateUserData, Identity
from src.core.infra.exceptions import AuthError
from src.core.settings import AuthConfig
from src.database.models import User, UserPropertyAccess, UserSession
from src.database.repositories.auth import AuthRepo

password_hasher = PasswordHash.recommended()
_DUMMY_HASH = password_hasher.hash(secrets.token_urlsafe(32))


def token_digest(token: str) -> str:
    """Hash a high-entropy browser token; never persist its raw representation."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def scope_for(user: Identity, property_id: str | None = None) -> AccessScope:
    """Intersect an optional requested ID with current rights without widening them."""
    if user.role not in ("admin", "viewer"):
        raise AuthError(403, "Роль пользователя не поддерживается")
    if property_id is not None:
        if user.role != "admin" and property_id not in user.property_ids:
            raise AuthError(403, "Нет доступа к объекту")
        return AccessScope(user, (property_id,))
    return AccessScope(user, None if user.role == "admin" else tuple(sorted(user.property_ids)))


class AuthService:
    """Own credential checks and transaction boundaries for local sessions."""

    def __init__(self, repo: AuthRepo, config: AuthConfig) -> None:
        """Use the request/CLI session and the common cookie lifetime policy."""
        self.repo = repo
        self.config = config

    async def create_user(self, data: CreateUserData) -> User:
        """Hash the password off-loop and atomically create the user and assignments."""
        if data.role == "admin" and data.property_ids:
            raise AuthError(422, "Администратор уже имеет доступ ко всем объектам")
        hashed = await run_in_threadpool(password_hasher.hash, data.password.get_secret_value())
        user = User(username=data.username.lower(), password_hash=hashed, role=data.role)
        try:
            self.repo.add(user)
            await self.repo.session.flush()
            for property_id in sorted(set(data.property_ids)):
                self.repo.add(UserPropertyAccess(user_id=user.id, property_id=property_id))
            await self.repo.commit()
        except IntegrityError:
            await self.repo.rollback()
            raise AuthError(409, "Пользователь с таким логином уже существует") from None
        return user

    async def login(self, username: str, password: str, previous_token: str | None) -> str:
        """Verify credentials and issue a fresh eight-hour session by default."""
        user = await self.repo.find_user(username.strip().lower())
        valid = await run_in_threadpool(
            password_hasher.verify, password, user.password_hash if user else _DUMMY_HASH
        )
        if not user or not valid or not user.is_active:
            raise AuthError(401, "Неверный логин или пароль")
        if previous_token:
            await self.repo.revoke(token_digest(previous_token))
        token = secrets.token_urlsafe(32)
        self.repo.add(
            UserSession(
                token_hash=token_digest(token),
                user_id=user.id,
                expires_at=datetime.now(UTC) + timedelta(seconds=self.config.session_seconds),
            )
        )
        await self.repo.commit()
        return token

    async def identity(self, token: str | None) -> Identity:
        """Resolve a live session and current assignments without trusting cookie claims."""
        if not token or len(token) > 128:
            raise AuthError(401, "Требуется вход")
        user = await self.repo.session_user(token_digest(token))
        if user is None:
            raise AuthError(401, "Сессия отсутствует или завершена")
        return Identity(user.id, user.username, user.role, await self.repo.property_ids(user.id))

    async def logout(self, token: str) -> None:
        """Commit server-side revocation before the HTTP adapter clears the cookie."""
        await self.repo.revoke(token_digest(token))
        await self.repo.commit()
