from __future__ import annotations

from typing import TypeVar


Item = TypeVar("Item")


def next_sequence_item(items: list[Item], current: Item) -> Item | None:
    """Return the next displayed item without wrapping at the end."""
    try:
        index = items.index(current)
    except ValueError:
        return None
    return items[index + 1] if index + 1 < len(items) else None
