"""Strict loading for cross-project consensus limits."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class PolicyError(ValueError):
    """Raised when the central policy is incomplete or outside approved limits."""


_REQUIRED_KEYS = {
    "version",
    "max_rounds",
    "max_context_tokens",
    "max_initial_sources",
    "max_context_expansions",
    "production_line_limit",
    "production_growth_percent",
    "production_excludes",
}

_MAXIMUMS = {
    "max_rounds": 6,
    "max_context_tokens": 8000,
    "max_initial_sources": 3,
    "max_context_expansions": 2,
    "production_line_limit": 100,
    "production_growth_percent": 30,
}


@dataclass(frozen=True)
class Policy:
    version: int
    max_rounds: int
    max_context_tokens: int
    max_initial_sources: int
    max_context_expansions: int
    production_line_limit: int
    production_growth_percent: int
    production_excludes: list


def _validate(contents: Any) -> Policy:
    if not isinstance(contents, dict):
        raise PolicyError("policy must be a YAML mapping")
    keys = set(contents)
    missing = _REQUIRED_KEYS - keys
    unknown = keys - _REQUIRED_KEYS
    if missing:
        raise PolicyError("policy is missing required keys: %s" % sorted(missing))
    if unknown:
        raise PolicyError("policy has unknown keys: %s" % sorted(unknown))
    if contents["version"] != 1 or type(contents["version"]) is not int:
        raise PolicyError("policy version must be 1")
    for key, limit in _MAXIMUMS.items():
        value = contents[key]
        if type(value) is not int or value < 1 or value > limit:
            raise PolicyError("%s must be an integer from 1 to %d" % (key, limit))
    excludes = contents["production_excludes"]
    if not isinstance(excludes, list) or not all(
        isinstance(item, str) and item for item in excludes
    ):
        raise PolicyError("production_excludes must be a list of non-empty strings")
    return Policy(**contents)


def load_policy(path: Path) -> Policy:
    """Load a complete central policy without accepting implicit defaults."""
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            contents = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as error:
        raise PolicyError("could not load policy: %s" % error) from error
    return _validate(contents)
