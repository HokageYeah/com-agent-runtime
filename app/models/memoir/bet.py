"""赌局素材的最小 ORM 映射；只供回忆录冻结器按关系段读取。"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, String

from app.db.sqlalchemy_db import Base


class Bet(Base):
    """业务赌局记录；回忆录不修改赌局，仅生成加密的冻结摘要。"""

    __tablename__ = "bets"

    id = Column(Integer, primary_key=True)
    space_id = Column(Integer, nullable=False, index=True)
    relationship_id = Column(Integer, nullable=False, index=True)
    relationship_segment_no = Column(Integer, nullable=False)
    creator_user_id = Column(Integer, nullable=False)
    receiver_user_id = Column(Integer, nullable=False)
    title = Column(String(120), nullable=False)
    reward = Column(String(120), nullable=False)
    status = Column(String(24), nullable=False)
    winner_user_id = Column(Integer, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False)
