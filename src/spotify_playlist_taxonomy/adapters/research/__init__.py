"""Read-only independent research provider adapters."""

from .musicbrainz import MusicBrainzResearchProvider
from .wikidata import WikidataResearchProvider

__all__ = ["MusicBrainzResearchProvider", "WikidataResearchProvider"]
