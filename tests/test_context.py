import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_review.context import (
    ContextBudgetError,
    ContextExpansionError,
    SourceRef,
    build_packet,
    expand_packet,
    extract_section,
)


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, contents):
        path = self.root / name
        path.write_text(contents, encoding="utf-8")
        return path

    def test_extracts_only_requested_markdown_section(self):
        source = self.write("brain.md", "# A\nkeep\n## B\nnested\n# C\nskip\n")

        packet = build_packet([SourceRef(source, "A", "architecture")], max_tokens=1000)

        self.assertIn("keep", packet.markdown)
        self.assertIn("nested", packet.markdown)
        self.assertNotIn("skip", packet.markdown)

    def test_rejects_missing_or_duplicate_exact_headings(self):
        missing = self.write("missing.md", "# Other\nx\n")
        duplicate = self.write("duplicate.md", "# A\nx\n# A\ny\n")

        with self.assertRaises(ContextBudgetError):
            extract_section(missing, "A")
        with self.assertRaises(ContextBudgetError):
            extract_section(duplicate, "A")

    def test_rejects_more_than_three_initial_sources(self):
        refs = [
            SourceRef(self.write("%s.md" % index, "# A\nx\n"), "A", "r")
            for index in range(4)
        ]

        with self.assertRaises(ContextBudgetError):
            build_packet(refs, max_tokens=8000, max_sources=3)

        with self.assertRaises(ContextBudgetError):
            build_packet(refs, max_tokens=8000, max_sources=4)

    def test_budget_uses_four_character_fallback(self):
        source = self.write("brain.md", "# A\n" + ("字" * 41))

        with self.assertRaises(ContextBudgetError):
            build_packet([SourceRef(source, "A", "r")], max_tokens=10)

    def test_budget_includes_rendered_packet_provenance(self):
        source = self.write("brain.md", "# A\n")

        with self.assertRaises(ContextBudgetError):
            build_packet([SourceRef(source, "A", "r")], max_tokens=1)

    def test_unchanged_packet_has_same_checksum(self):
        source = self.write("brain.md", "# A\nstable\n")

        first = build_packet([SourceRef(source, "A", "r")], max_tokens=1000)
        second = build_packet([SourceRef(source, "A", "r")], max_tokens=1000)

        self.assertEqual(first.checksum, second.checksum)

    def test_writes_manifest_with_checked_but_not_selected_paths(self):
        source = self.write("selected.md", "# A\nkeep\n")
        checked = self.write("checked.md", "# B\nnot selected\n")
        output = self.root / "packet"

        packet = build_packet(
            [SourceRef(source, "A", "architecture")],
            max_tokens=1000,
            checked_paths=[checked],
            output_dir=output,
        )

        manifest = json.loads((output / "context-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["selected"][0]["path"], str(source.resolve()))
        self.assertEqual(manifest["checked_not_selected"], [str(checked.resolve())])
        self.assertTrue(manifest["advisory_only"])
        self.assertTrue((output / "knowledge-packet-r1.md").exists())
        self.assertEqual(packet.revision, 1)

    def test_expansion_replaces_lowest_priority_source_within_budget(self):
        low = self.write("low.md", "# A\nlow\n")
        high = self.write("high.md", "# A\nhigh\n")
        replacement = self.write("replacement.md", "# A\nreplacement\n")
        output = self.root / "packet"
        packet = build_packet(
            [SourceRef(low, "A", "low", priority=1), SourceRef(high, "A", "high", priority=5)],
            max_tokens=1000,
            output_dir=output,
        )

        expanded = expand_packet(
            packet,
            [SourceRef(replacement, "A", "new evidence", priority=3)],
            output_dir=output,
        )

        self.assertEqual(expanded.revision, 2)
        self.assertEqual([item.path for item in expanded.sources], [str(replacement.resolve()), str(high.resolve())])
        self.assertIn("replacement", expanded.markdown)
        self.assertNotIn("low\n", expanded.markdown)
        self.assertTrue((output / "knowledge-packet-r1.md").exists())
        self.assertTrue((output / "knowledge-packet-r2.md").exists())

    def test_expansion_rejects_candidate_when_rendered_provenance_exceeds_budget(self):
        source = self.write("source.md", "# A\nx\n")
        replacement = self.write("replacement.md", "# A\nx\n")
        baseline = build_packet([SourceRef(source, "A", "r")], max_tokens=1000)
        packet = build_packet(
            [SourceRef(source, "A", "r")], max_tokens=baseline.estimated_tokens
        )

        with self.assertRaises(ContextBudgetError):
            expand_packet(
                packet,
                [SourceRef(replacement, "A", "reason that grows the rendered packet" * 4)],
            )

    def test_failed_divergent_expansion_leaves_packet_files_and_manifest_unchanged(self):
        source = self.write("source.md", "# A\nsource\n")
        first_candidate = self.write("first.md", "# A\nfirst\n")
        second_candidate = self.write("second.md", "# A\nsecond\n")
        output = self.root / "packet"
        packet = build_packet([SourceRef(source, "A", "r")], max_tokens=1000, output_dir=output)
        first_expansion = expand_packet(packet, [SourceRef(first_candidate, "A", "first")])
        before = {path.name: path.read_bytes() for path in output.iterdir()}

        with self.assertRaises(ContextExpansionError):
            expand_packet(packet, [SourceRef(second_candidate, "A", "second")])

        after = {path.name: path.read_bytes() for path in output.iterdir()}
        self.assertEqual(after, before)
        self.assertEqual(first_expansion.revision, 2)

    def test_source_checksum_and_excerpt_use_one_source_read(self):
        source = self.write("source.md", "# A\ncontent\n")
        original_read_bytes = Path.read_bytes

        with patch.object(Path, "read_bytes", autospec=True, side_effect=original_read_bytes) as read_bytes:
            build_packet([SourceRef(source, "A", "r")], max_tokens=1000)

        self.assertEqual(read_bytes.call_count, 1)

    def test_expansion_is_limited_to_two_and_does_not_change_run_state(self):
        source = self.write("source.md", "# A\nsource\n")
        candidate = self.write("candidate.md", "# A\ncandidate\n")
        packet = build_packet([SourceRef(source, "A", "r")], max_tokens=1000)

        packet = expand_packet(packet, [SourceRef(candidate, "A", "first")])
        packet = expand_packet(packet, [SourceRef(source, "A", "second")])
        with self.assertRaises(ContextExpansionError):
            expand_packet(packet, [SourceRef(candidate, "A", "third")])

        self.assertEqual(packet.expansion_count, 2)
