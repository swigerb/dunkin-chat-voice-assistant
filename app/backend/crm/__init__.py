"""CRM data models and repository helpers."""

from .models import CustomerFavoriteItem, CustomerProfile, CustomerSuggestion
from .repository import CRMRepository

__all__ = [
    "CRMRepository",
    "CustomerFavoriteItem",
    "CustomerProfile",
    "CustomerSuggestion",
]
