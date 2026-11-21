"""Build station labels for configuration and board search."""

from collections.abc import Iterable

from .models import Stop


def station_labels(stops: Iterable[Stop]) -> dict[str, str]:
    """Return searchable Great Britain passenger stations in name order."""
    labels = {}
    for stop in stops:
        if stop.namespace != "gb-nr" or not stop.short_code:
            continue
        code = stop.unique_identity or f"gb-nr:{stop.short_code}"
        labels[code] = (
            f"{stop.description} ({stop.short_code})"
            if stop.description
            else stop.short_code
        )
    return dict(sorted(labels.items(), key=lambda option: option[1].casefold()))
