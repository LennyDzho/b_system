"""Keep user/session SQL in the framework's repository layer."""

from sqlalchemy import func, select, update

from src.database.models import User, UserPropertyAccess, UserSession
from src.database.repositories.base import BaseRepo


class AuthRepo(BaseRepo):
    """Read current identities and mutate session records within caller transactions."""

    async def find_user(self, username: str) -> User | None:
        """Return the normalized local login or an explicit absence."""
        return await self.session.scalar(select(User).where(User.username == username))

    async def session_user(self, token_hash: str) -> User | None:
        """Recheck activation, expiry, and revocation on every authenticated request."""
        return await self.session.scalar(
            select(User)
            .join(UserSession)
            .where(
                UserSession.token_hash == token_hash,
                UserSession.expires_at > func.now(),
                UserSession.revoked_at.is_(None),
                User.is_active.is_(True),
            )
        )

    async def property_ids(self, user_id: int) -> frozenset[str]:
        """Read current assignments; an empty set always means no viewer access."""
        result = await self.session.scalars(
            select(UserPropertyAccess.property_id).where(UserPropertyAccess.user_id == user_id)
        )
        return frozenset(result)

    async def revoke(self, token_hash: str) -> None:
        """Revoke the server session without storing the browser's raw token."""
        await self.session.execute(
            update(UserSession)
            .where(UserSession.token_hash == token_hash)
            .values(revoked_at=func.now())
        )
