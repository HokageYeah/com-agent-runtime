#!/usr/bin/env python

import logging

import uvicorn

from app.core.config import settings


def main() -> None:
    logging.info("启动应用服务器...")
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.RELOAD,
    )


if __name__ == "__main__":
    main()
