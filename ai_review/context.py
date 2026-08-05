"""Bounded, advisory second-brain context packets for consensus reviews.

This module deliberately has no dependency on review state or storage authority.
Knowledge packets are evidence for a reviewer, never a replacement for code or
test results.
"""

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple, Union


DEFAULT_MAX_TOKENS = 8000
DEFAULT_MAX_SOURCES = 3
MAX_EXPANSIONS = 2
BUDGET_METHOD = "four_chars_per_token"
_ATX_HEADING = re.compile(r"^[ ]{0,3}(#{1,6})(?:[ \t]+|$)(.*)$")


class ContextBudgetError(ValueError):
    """A context source cannot be selected within the configured budget."""


class ContextExpansionError(ContextBudgetError):
    """A requested context expansion violates its bounded revision policy."""


@dataclass(frozen=True)
class SourceRef:
    """A requested, exact Markdown section and its review relevance."""

    path: Union[str, Path]
    section: str
    reason: str
    priority: int = 0

    def __post_init__(self) -> None:
        resolved = Path(self.path).expanduser().resolve()
        if not self.section or not isinstance(self.section, str):
            raise ContextBudgetError("source section must be a non-empty string")
        if not self.reason or not isinstance(self.reason, str):
            raise ContextBudgetError("source reason must be a non-empty string")
        if type(self.priority) is not int:
            raise ContextBudgetError("source priority must be an integer")
        object.__setattr__(self, "path", str(resolved))


@dataclass(frozen=True)
class SourceExcerpt:
    """Immutable provenance and selected text for one packet source."""

    path: str
    section: str
    reason: str
    priority: int
    source_checksum: str
    markdown: str
    estimated_tokens: int

    def manifest_dict(self) -> dict:
        return {
            "path": self.path,
            "section": self.section,
            "reason": self.reason,
            "priority": self.priority,
            "source_checksum": self.source_checksum,
            "estimated_tokens": self.estimated_tokens,
        }


@dataclass(frozen=True)
class KnowledgePacket:
    """A deterministic revision of a bounded, advisory knowledge packet."""

    sources: Tuple[SourceExcerpt, ...]
    checked_not_selected: Tuple[str, ...]
    max_context_tokens: int
    estimated_tokens: int
    checksum: str
    markdown: str
    revision: int = 1
    expansion_count: int = 0
    output_dir: Optional[str] = None

    @property
    def advisory_only(self) -> bool:
        """Second-brain evidence cannot override code or test evidence."""
        return True

    def manifest_dict(self) -> dict:
        return {
            "revision": self.revision,
            "expansion_count": self.expansion_count,
            "max_context_tokens": self.max_context_tokens,
            "budget_method": BUDGET_METHOD,
            "estimated_tokens": self.estimated_tokens,
            "checksum": self.checksum,
            "advisory_only": True,
            "selected": [source.manifest_dict() for source in self.sources],
            "checked_not_selected": list(self.checked_not_selected),
        }


def _read_source(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ContextBudgetError("unable to read context source: %s" % path) from error


def _heading_title(line: str) -> Optional[str]:
    match = _ATX_HEADING.match(line.rstrip("\r\n"))
    if not match:
        return None
    title = match.group(2)
    return re.sub(r"[ \t]+#+[ \t]*$", "", title).rstrip(" \t")


def _heading_level(line: str) -> Optional[int]:
    match = _ATX_HEADING.match(line.rstrip("\r\n"))
    return len(match.group(1)) if match else None


def _extract_section_from_text(source_path: Path, heading: str, source: str) -> str:
    """Return one exact ATX heading, including nested Markdown subsections.

    A requested heading must occur exactly once.  Extraction ends at the next
    ATX heading at the same or a higher level, preserving nested subsections.
    """
    if not isinstance(heading, str) or not heading:
        raise ContextBudgetError("requested heading must be a non-empty string")
    lines = source.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if _heading_title(line) == heading]
    if not matches:
        raise ContextBudgetError("requested Markdown heading is missing: %s" % heading)
    if len(matches) > 1:
        raise ContextBudgetError("requested Markdown heading is duplicated: %s" % heading)
    start = matches[0]
    requested_level = _heading_level(lines[start])
    end = len(lines)
    for index in range(start + 1, len(lines)):
        level = _heading_level(lines[index])
        if level is not None and level <= requested_level:
            end = index
            break
    return "".join(lines[start:end])


def extract_section(path: Union[str, Path], heading: str) -> str:
    """Read and extract one exact ATX Markdown section from *path*."""
    source_path = Path(path).expanduser().resolve()
    return _extract_section_from_text(source_path, heading, _read_source(source_path))


def _estimate_tokens(markdown: str) -> int:
    """Conservative fallback estimate retained in every packet manifest."""
    return int(math.ceil(len(markdown) / 4.0))


def _source_excerpt(reference: SourceRef) -> SourceExcerpt:
    path = Path(reference.path)
    try:
        source_bytes = path.read_bytes()
    except OSError as error:
        raise ContextBudgetError("unable to read context source: %s" % path) from error
    try:
        source = source_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContextBudgetError("unable to read context source: %s" % path) from error
    excerpt = _extract_section_from_text(path, reference.section, source)
    return SourceExcerpt(
        path=str(path),
        section=reference.section,
        reason=reference.reason,
        priority=reference.priority,
        source_checksum=hashlib.sha256(source_bytes).hexdigest(),
        markdown=excerpt,
        estimated_tokens=_estimate_tokens(excerpt),
    )


def _canonical_checked_paths(paths: Iterable[Union[str, Path]]) -> Tuple[str, ...]:
    return tuple(sorted({str(Path(path).expanduser().resolve()) for path in paths}))


def _checked_not_selected(
    checked_paths: Iterable[Union[str, Path]], sources: Sequence[SourceExcerpt]
) -> Tuple[str, ...]:
    selected = {source.path for source in sources}
    return tuple(path for path in _canonical_checked_paths(checked_paths) if path not in selected)


def _validate_budget(max_tokens: int, estimated_tokens: int) -> int:
    if type(max_tokens) is not int or max_tokens <= 0:
        raise ContextBudgetError("max_tokens must be a positive integer")
    if estimated_tokens > max_tokens:
        raise ContextBudgetError(
            "context estimate %s exceeds token budget %s" % (estimated_tokens, max_tokens)
        )
    return estimated_tokens


def _packet_checksum(sources: Sequence[SourceExcerpt], checked: Sequence[str], max_tokens: int, revision: int) -> str:
    payload = {
        "advisory_only": True,
        "budget_method": BUDGET_METHOD,
        "checked_not_selected": list(checked),
        "max_context_tokens": max_tokens,
        "revision": revision,
        "sources": [
            {
                "path": source.path,
                "section": source.section,
                "reason": source.reason,
                "priority": source.priority,
                "source_checksum": source.source_checksum,
                "markdown": source.markdown,
            }
            for source in sources
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _render_packet(sources: Sequence[SourceExcerpt], estimated_tokens: int, checksum: str) -> str:
    lines = [
        "# Knowledge Packet",
        "",
        "- budget_method: %s" % BUDGET_METHOD,
        "- estimated_tokens: %s" % estimated_tokens,
        "- checksum: %s" % checksum,
        "- evidence_status: advisory_only (code and tests remain authoritative)",
    ]
    for source in sources:
        lines.extend(
            [
                "",
                "## Source",
                "- path: %s" % source.path,
                "- section: %s" % source.section,
                "- reason: %s" % source.reason,
                "- source_checksum: %s" % source.source_checksum,
                "",
                source.markdown.rstrip("\n"),
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _render_with_token_estimate(
    sources: Sequence[SourceExcerpt], checksum: str
) -> Tuple[int, str]:
    """Render until the displayed estimate matches the actual packet estimate."""
    estimate = 0
    for _ in range(16):
        markdown = _render_packet(sources, estimate, checksum)
        actual = _estimate_tokens(markdown)
        if actual == estimate:
            return actual, markdown
        estimate = actual
    raise ContextBudgetError("packet token estimate did not converge")


def _make_packet(
    sources: Sequence[SourceExcerpt],
    checked_paths: Iterable[Union[str, Path]],
    max_tokens: int,
    revision: int,
    expansion_count: int,
    output_dir: Optional[Union[str, Path]],
) -> KnowledgePacket:
    checked = _checked_not_selected(checked_paths, sources)
    checksum = _packet_checksum(sources, checked, max_tokens, revision)
    estimated, markdown = _render_with_token_estimate(sources, checksum)
    _validate_budget(max_tokens, estimated)
    normalized_output = str(Path(output_dir).expanduser().resolve()) if output_dir else None
    return KnowledgePacket(
        sources=tuple(sources),
        checked_not_selected=checked,
        max_context_tokens=max_tokens,
        estimated_tokens=estimated,
        checksum=checksum,
        markdown=markdown,
        revision=revision,
        expansion_count=expansion_count,
        output_dir=normalized_output,
    )


def validate_knowledge_packet(packet: KnowledgePacket) -> KnowledgePacket:
    """Recompute every derived field and return one canonical bounded packet."""
    if not isinstance(packet, KnowledgePacket):
        raise ContextBudgetError("knowledge packet has an invalid type")
    if (
        type(packet.revision) is not int or packet.revision < 1
        or type(packet.expansion_count) is not int
        or packet.expansion_count < 0 or packet.expansion_count > MAX_EXPANSIONS
        or not isinstance(packet.checked_not_selected, tuple)
    ):
        raise ContextBudgetError("knowledge packet counters are invalid")
    for source in packet.sources:
        if (
            not isinstance(source, SourceExcerpt)
            or not re.fullmatch(r"[0-9a-f]{64}", source.source_checksum)
            or source.estimated_tokens != _estimate_tokens(source.markdown)
        ):
            raise ContextBudgetError("knowledge packet source is invalid")
    checksum = _packet_checksum(
        packet.sources, packet.checked_not_selected,
        packet.max_context_tokens, packet.revision,
    )
    estimated, markdown = _render_with_token_estimate(packet.sources, checksum)
    _validate_budget(packet.max_context_tokens, estimated)
    if (
        not isinstance(packet.checksum, str)
        or not hmac.compare_digest(packet.checksum, checksum)
        or packet.estimated_tokens != estimated
        or packet.markdown != markdown
    ):
        raise ContextBudgetError("knowledge packet derived fields do not match canonical content")
    return KnowledgePacket(
        sources=packet.sources,
        checked_not_selected=packet.checked_not_selected,
        max_context_tokens=packet.max_context_tokens,
        estimated_tokens=estimated,
        checksum=checksum,
        markdown=markdown,
        revision=packet.revision,
        expansion_count=packet.expansion_count,
        output_dir=None,
    )


def _preflight_packet_revision(packet: KnowledgePacket, root: Path) -> bool:
    """Return whether a revision needs writing, without mutating packet artifacts."""
    revision_path = root / ("knowledge-packet-r%s.md" % packet.revision)
    contents = packet.markdown.encode("utf-8")
    if revision_path.exists():
        try:
            existing = revision_path.read_bytes()
        except OSError as error:
            raise ContextExpansionError("unable to verify packet revision: %s" % revision_path) from error
        if existing != contents:
            raise ContextExpansionError("packet revision is immutable: %s" % revision_path)
        return False
    return True


def _write_manifest(packet: KnowledgePacket, root: Path) -> None:
    (root / "context-manifest.json").write_text(
        json.dumps(packet.manifest_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_packet(
    packet: KnowledgePacket, output_dir: Union[str, Path], write_manifest: bool = True
) -> None:
    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if _preflight_packet_revision(packet, root):
        revision_path = root / ("knowledge-packet-r%s.md" % packet.revision)
        contents = packet.markdown.encode("utf-8")
        revision_path.write_bytes(contents)
    if write_manifest:
        _write_manifest(packet, root)


def build_packet(
    sources: Iterable[SourceRef],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_sources: int = DEFAULT_MAX_SOURCES,
    checked_paths: Iterable[Union[str, Path]] = (),
    output_dir: Optional[Union[str, Path]] = None,
) -> KnowledgePacket:
    """Build the initial bounded packet from no more than three sources."""
    references = tuple(sources)
    if type(max_sources) is not int or max_sources <= 0 or max_sources > DEFAULT_MAX_SOURCES:
        raise ContextBudgetError("max_sources must be between 1 and %s" % DEFAULT_MAX_SOURCES)
    if len(references) > max_sources:
        raise ContextBudgetError("initial context may select no more than %s sources" % max_sources)
    if not references:
        raise ContextBudgetError("initial context requires at least one source")
    excerpts = tuple(_source_excerpt(reference) for reference in references)
    packet = _make_packet(excerpts, checked_paths, max_tokens, 1, 0, output_dir)
    if output_dir is not None:
        _write_packet(packet, output_dir)
    return packet


def expand_packet(
    packet: KnowledgePacket,
    candidates: Iterable[SourceRef],
    output_dir: Optional[Union[str, Path]] = None,
) -> KnowledgePacket:
    """Replace one lowest-priority excerpt without changing run state or budget."""
    if not isinstance(packet, KnowledgePacket):
        raise ContextExpansionError("expand_packet requires a knowledge packet")
    if packet.expansion_count >= MAX_EXPANSIONS:
        raise ContextExpansionError("context packets permit no more than two expansions")
    references = tuple(candidates)
    if not references:
        raise ContextExpansionError("context expansion requires a replacement candidate")
    excerpts = tuple(_source_excerpt(reference) for reference in references)
    evicted_index = min(range(len(packet.sources)), key=lambda index: (packet.sources[index].priority, index))
    remaining = list(packet.sources)
    checked_paths = set(packet.checked_not_selected)
    checked_paths.update(source.path for source in packet.sources)
    checked_paths.update(source.path for source in excerpts)
    chosen = None
    for candidate in sorted(excerpts, key=lambda item: (-item.priority, item.path, item.section, item.reason)):
        proposed = list(remaining)
        proposed[evicted_index] = candidate
        try:
            _make_packet(
                proposed,
                checked_paths,
                packet.max_context_tokens,
                packet.revision + 1,
                packet.expansion_count + 1,
                None,
            )
        except ContextBudgetError:
            continue
        else:
            chosen = candidate
            break
    if chosen is None:
        raise ContextBudgetError("no replacement candidate fits the existing context budget")
    remaining[evicted_index] = chosen
    target_dir = output_dir if output_dir is not None else packet.output_dir
    expanded = _make_packet(
        remaining,
        checked_paths,
        packet.max_context_tokens,
        packet.revision + 1,
        packet.expansion_count + 1,
        target_dir,
    )
    if target_dir is not None:
        root = Path(target_dir).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        _preflight_packet_revision(packet, root)
        _preflight_packet_revision(expanded, root)
        _write_packet(packet, root, write_manifest=False)
        _write_packet(expanded, root, write_manifest=False)
        _write_manifest(expanded, root)
    return expanded
