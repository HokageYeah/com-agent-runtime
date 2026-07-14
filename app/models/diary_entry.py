from __future__ import annotations

from sqlalchemy import Column, Date, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from app.db.sqlalchemy_db import Base


class DiaryEntry(Base):
    """日记业务域的最小持久化模型。

    这里故意不做过多字段设计，只保留模板工程最需要的几类信息：
    1. 标题与正文，方便演示文本型业务表应该怎么建模
    2. 心情字段，告诉后续开发者可以如何继续扩展业务属性
    3. entry_date，明确这是一条“日记记录”而不是旧模板里的文章抓取记录
    """

    __tablename__ = "diary_entries"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), index=True, nullable=False)
    content = Column(Text, nullable=True)
    mood = Column(String(50), nullable=True)
    entry_date = Column(Date, nullable=True, index=True)
    created_at = Column(DateTime, default=func.now(), nullable=True)
    updated_at = Column(
        DateTime,
        default=func.now(),
        onupdate=func.now(),
        nullable=True,
    )
