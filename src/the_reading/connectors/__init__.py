"""Personal signal source connectors."""

from the_reading.connectors.base import InterestConnector
from the_reading.connectors.jellyfin import JellyfinConnector
from the_reading.connectors.letterboxd import LetterboxdConnector
from the_reading.connectors.navidrome import NavidromeConnector
from the_reading.connectors.seerr import SeerrConnector

__all__ = [
    "InterestConnector",
    "JellyfinConnector",
    "LetterboxdConnector",
    "NavidromeConnector",
    "SeerrConnector",
]
