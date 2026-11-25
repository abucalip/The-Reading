"""Delivery targets for generated EPUB editions."""

from the_reading.delivery.gdrive import (
    DeliveryResult,
    GDriveDeliverer,
    UploadResult,
    build_drive_service,
    get_credentials,
)

__all__ = [
    "DeliveryResult",
    "GDriveDeliverer",
    "UploadResult",
    "build_drive_service",
    "get_credentials",
]
