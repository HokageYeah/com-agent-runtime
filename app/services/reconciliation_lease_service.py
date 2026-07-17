"""数据库对账扫描租约，保证多实例不会同时执行同一轮扫描。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import RuntimeReconciliationLease


class ReconciliationLeaseService:
    """用单行、带过期时间的租约串行化对账器。"""

    LEASE_KEY = "runtime-reconciler"

    def __init__(self, session: Session, *, ttl_seconds: int = 300) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds 必须大于零")
        self._session = session
        self._ttl = timedelta(seconds=ttl_seconds)
        self._fencing_token: int | None = None

    @property
    def fencing_token(self) -> int | None:
        """最近一次成功 acquire 的 fencing token。"""
        return self._fencing_token

    def acquire(self, owner_id: str, *, now: datetime | None = None) -> bool:
        """原子取得已到期租约；未持有者不做任何扫描。"""
        current = now or datetime.now(UTC)
        expires_at = current + self._ttl
        claimed = self._session.execute(
            update(RuntimeReconciliationLease)
            .where(
                RuntimeReconciliationLease.lease_key == self.LEASE_KEY,
                RuntimeReconciliationLease.expires_at <= current,
            )
            .values(
                owner_id=owner_id,
                expires_at=expires_at,
                fencing_token=RuntimeReconciliationLease.fencing_token + 1,
            )
        )
        if claimed.rowcount == 1:  # type: ignore[attr-defined]
            lease = self._session.scalar(
                select(RuntimeReconciliationLease.fencing_token).where(
                    RuntimeReconciliationLease.lease_key == self.LEASE_KEY
                )
            )
            assert lease is not None
            self._fencing_token = lease
            self._session.commit()
            return True

        try:
            with self._session.begin_nested():
                self._session.add(
                    RuntimeReconciliationLease(
                        lease_key=self.LEASE_KEY,
                        owner_id=owner_id,
                        expires_at=expires_at,
                        fencing_token=1,
                    )
                )
                self._session.flush()
        except IntegrityError:
            # 并发创建者已赢得租约；后续条件 UPDATE 只会在其已过期时接管。
            claimed = self._session.execute(
                update(RuntimeReconciliationLease)
                .where(
                    RuntimeReconciliationLease.lease_key == self.LEASE_KEY,
                    RuntimeReconciliationLease.expires_at <= current,
                )
                .values(
                    owner_id=owner_id,
                    expires_at=expires_at,
                    fencing_token=RuntimeReconciliationLease.fencing_token + 1,
                )
            )
            if claimed.rowcount != 1:  # type: ignore[attr-defined]
                self._session.rollback()
                return False
        self._session.commit()
        lease = self._session.scalar(
            select(RuntimeReconciliationLease.fencing_token).where(
                RuntimeReconciliationLease.lease_key == self.LEASE_KEY
            )
        )
        assert lease is not None
        self._fencing_token = lease
        return True

    def renew(
        self, owner_id: str, fencing_token: int, *, now: datetime | None = None
    ) -> bool:
        """仅当前 owner/token 可续租；到期或被接管的旧实例立即失去扫描权。"""
        current = now or datetime.now(UTC)
        renewed = self._session.execute(
            update(RuntimeReconciliationLease)
            .where(
                RuntimeReconciliationLease.lease_key == self.LEASE_KEY,
                RuntimeReconciliationLease.owner_id == owner_id,
                RuntimeReconciliationLease.fencing_token == fencing_token,
                RuntimeReconciliationLease.expires_at > current,
            )
            .values(expires_at=current + self._ttl)
        )
        self._session.commit()
        return renewed.rowcount == 1  # type: ignore[attr-defined]

    def release(
        self, owner_id: str, fencing_token: int, *, now: datetime | None = None
    ) -> bool:
        """仅持有者可提前释放；fencing 防止误释放新 owner 的租约。"""
        current = now or datetime.now(UTC)
        released = self._session.execute(
            update(RuntimeReconciliationLease)
            .where(
                RuntimeReconciliationLease.lease_key == self.LEASE_KEY,
                RuntimeReconciliationLease.owner_id == owner_id,
                RuntimeReconciliationLease.fencing_token == fencing_token,
            )
            .values(expires_at=current)
        )
        self._session.commit()
        return released.rowcount == 1  # type: ignore[attr-defined]
