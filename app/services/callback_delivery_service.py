"""兼容旧导入路径；可靠投递实现归入 callback_service。"""

from app.services.callback_service import CallbackDeliveryService, CallbackSender

__all__ = ["CallbackDeliveryService", "CallbackSender"]
