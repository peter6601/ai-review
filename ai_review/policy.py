"""Strict loading for cross-project consensus limits."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Tuple

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
    "doc_max_initial_sources",
    "doc_max_context_tokens",
    "codex_model",
}

# The run kinds a context budget exists for.  ``context_limits`` refuses
# anything else rather than handing back the shared pair, because a silently
# wrong budget is the failure this mapping exists to prevent.
_CONTEXT_KINDS = frozenset({"plan", "code", "review", "doc"})

_MAXIMUMS = {
    "max_rounds": 6,
    "max_context_tokens": 8000,
    "max_initial_sources": 3,
    "max_context_expansions": 2,
    "production_line_limit": 100,
    "production_growth_percent": 30,
    "doc_max_initial_sources": 5,
    "doc_max_context_tokens": 24000,
}

# ``codex_model`` is a requirement, not a numeric limit, so it stays out of
# ``_MAXIMUMS`` and is checked by allowlist instead.  The value becomes one
# argv element handed to a subprocess; it never reaches a shell, but a policy
# file is the wrong place to accept arbitrary text, and refusing an
# unvalidated value is this module's whole job.
_MODEL_NAME = re.compile(r"[A-Za-z0-9._-]+")


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
    doc_max_initial_sources: int
    doc_max_context_tokens: int
    # No default, deliberately: a Policy that can be built without a model is
    # a Policy whose model can be silently substituted, which is the shape of
    # the defect this field exists to close.  The pinned value has exactly one
    # home, config/defaults.yaml.
    codex_model: str

    def context_limits(self, kind: str) -> Tuple[int, int]:
        """(max_sources, max_tokens) for the run kind."""
        if kind not in _CONTEXT_KINDS:
            # PolicyError is a ValueError, and PlanWorkflow._expand_context
            # catches ValueError: raised from there this surfaces as a
            # CONTEXT_EXPANSION_FAILED pause, not as a propagating error.
            raise PolicyError("no context limits for run kind: %r" % (kind,))
        if kind == "doc":
            return self.doc_max_initial_sources, self.doc_max_context_tokens
        return self.max_initial_sources, self.max_context_tokens


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
    model = contents["codex_model"]
    if type(model) is not str or not _MODEL_NAME.fullmatch(model):
        raise PolicyError(
            "codex_model must be a non-empty name of letters, digits, '.', '-', or '_'"
        )
    return Policy(**contents)


def load_policy(path: Path) -> Policy:
    """Load a complete central policy without accepting implicit defaults."""
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            contents = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as error:
        raise PolicyError("could not load policy: %s" % error) from error
    return _validate(contents)
