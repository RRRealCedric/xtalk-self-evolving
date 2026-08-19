"""Core SCID schemas, product boundaries, and static templates."""

from .product_contract import (
    DEPLOYMENT_SCOPE,
    PRODUCT_CONTRACT_VERSION,
    USER_DISCLOSURE_ZH,
    render_session_opening,
)

__all__ = [
    "DEPLOYMENT_SCOPE",
    "PRODUCT_CONTRACT_VERSION",
    "USER_DISCLOSURE_ZH",
    "render_session_opening",
]
