"""Typed handler output wrappers used by the Runtime executor.

Python lists are valid Table payloads, so cardinality cannot be inferred from
the value's container type.  Handlers producing multiple Artifacts must opt
into ``ArtifactBatch`` explicitly.
"""

from __future__ import annotations

from typing import Iterable


class ArtifactPayload:
    """Explicit wrapper for one Artifact payload (including a row list)."""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class ArtifactBatch(list):
    """Explicit wrapper for a many-cardinality Artifact output.

    Subclassing ``list`` preserves compatibility for direct handler callers,
    while the executor checks this concrete type before accepting a batch.
    """

    pass


__all__ = ["ArtifactBatch", "ArtifactPayload"]
