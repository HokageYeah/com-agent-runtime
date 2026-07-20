"""情侣关系表的 ORM 映射；归档只读取已经解除的确定关系段。"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, String

from app.db.sqlalchemy_db import Base


class CoupleRelationship(Base):
    """真实关系段；space 与 segment 是回忆录素材筛选的业务边界。"""

    __tablename__ = "couple_relationships"

    id = Column(Integer, primary_key=True)
    user_a_id = Column(Integer, nullable=False)
    user_b_id = Column(Integer, nullable=False)
    status = Column(String(32), nullable=False)
    bound_at = Column(DateTime, nullable=True)
    unbound_at = Column(DateTime, nullable=True)
    unbound_by_user_id = Column(Integer, nullable=True)
    unbound_reason = Column(String(32), nullable=True)
    space_id = Column(Integer, nullable=False, index=True)
    relationship_segment_no = Column(Integer, nullable=False)
