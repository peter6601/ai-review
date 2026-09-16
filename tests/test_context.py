import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_review.context import (
    MAX_SOURCES_CEILING,
    ContextBudgetError,
    ContextExpansionError,
    SourceRef,
    build_packet,
    expand_packet,
    extract_section,
)
from ai_review.policy import load_policy


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

    def test_rejects_more_initial_sources_than_the_cap_allows(self):
        refs = [
            SourceRef(self.write("%s.md" % index, "# A\nx\n"), "A", "r")
            for index in range(4)
        ]

        with self.assertRaises(ContextBudgetError):
            build_packet(refs, max_tokens=8000, max_sources=3)

        with self.assertRaises(ContextBudgetError):
            build_packet(refs, max_tokens=8000, max_sources=MAX_SOURCES_CEILING + 1)

    def test_accepts_sources_up_to_the_ceiling(self):
        refs = [
            SourceRef(self.write("%s.md" % index, "# A\nx\n"), "A", "r")
            for index in range(MAX_SOURCES_CEILING)
        ]

        packet = build_packet(refs, max_tokens=8000, max_sources=MAX_SOURCES_CEILING)

        self.assertEqual(len(packet.sources), MAX_SOURCES_CEILING)

    def test_doc_source_budget_is_reachable_from_the_shipped_policy(self):
        policy = load_policy(Path(__file__).parents[1] / "config" / "defaults.yaml")
        max_sources, max_tokens = policy.context_limits("doc")
        refs = [
            SourceRef(self.write("%s.md" % index, "# A\nx\n"), "A", "r")
            for index in range(max_sources)
        ]

        packet = build_packet(refs, max_tokens=max_tokens, max_sources=max_sources)

        self.assertEqual(len(packet.sources), policy.doc_max_initial_sources)

    def test_budget_uses_four_character_fallback(self):
        source = self.write("brain.md", "# A\n" + ("字" * 41))

        with self.assertRaises(ContextBudgetError):
            build_packet([SourceRef(source, "A", "r")], max_tokens=10)

    def test_budget_includes_rendered_packet_provenance(self):
        source = self.write("brain.md", "# A\n")

        with self.assertRaises(ContextBudgetError):
            build_packet([SourceRef(source, "A", "r")], max_tokens=1)

    def test_checksum_depends_on_the_token_budget(self):
        source = self.write("brain.md", "# A\nstable\n")
        refs = [SourceRef(source, "A", "r")]

        self.assertNotEqual(
            build_packet(refs, max_tokens=8000).checksum,
            build_packet(refs, max_tokens=16000).checksum,
        )

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

    def test_cjk_budget_method_counts_han_characters_individually(self):
        from ai_review.context import BUDGET_METHOD_DOC, estimate_tokens

        ascii_only = "a" * 400
        self.assertEqual(estimate_tokens(ascii_only, BUDGET_METHOD_DOC), 100)
        # 100 Han characters count as 25 tokens under four_chars_per_token; the real
        # cost is closer to 100.
        han = "審" * 100
        self.assertEqual(estimate_tokens(han, BUDGET_METHOD_DOC), 100)

    def test_default_budget_method_is_unchanged(self):
        from ai_review.context import BUDGET_METHOD, estimate_tokens

        self.assertEqual(estimate_tokens("審" * 100, BUDGET_METHOD), 25)

    def test_default_packet_artifacts_are_byte_identical_to_the_pre_refactor_output(self):
        """Golden checksum, markdown and manifest captured before this refactor."""
        from ai_review.context import (
            BUDGET_METHOD,
            KnowledgePacket,
            SourceExcerpt,
            _packet_checksum,
            _render_with_token_estimate,
        )

        excerpt = SourceExcerpt(
            path="/fixed/brain.md",
            section="A",
            reason="r",
            priority=0,
            source_checksum="0" * 64,
            markdown="# A\nkeep\n",
            estimated_tokens=2,
        )
        checksum = _packet_checksum((excerpt,), (), 1000, 1, BUDGET_METHOD)
        estimated, markdown = _render_with_token_estimate((excerpt,), checksum, BUDGET_METHOD)
        packet = KnowledgePacket(
            sources=(excerpt,),
            checked_not_selected=(),
            max_context_tokens=1000,
            estimated_tokens=estimated,
            checksum=checksum,
            markdown=markdown,
        )

        self.assertEqual(
            checksum, "eaa30231efcf096ac813e555a5cc20b01f8e6eebc4def04a1cc79da0161722bc"
        )
        self.assertEqual(estimated, 96)
        self.assertEqual(
            markdown,
            "# Knowledge Packet\n"
            "\n"
            "- budget_method: four_chars_per_token\n"
            "- estimated_tokens: 96\n"
            "- checksum: eaa30231efcf096ac813e555a5cc20b01f8e6eebc4def04a1cc79da0161722bc\n"
            "- evidence_status: advisory_only (code and tests remain authoritative)\n"
            "\n"
            "## Source\n"
            "- path: /fixed/brain.md\n"
            "- section: A\n"
            "- reason: r\n"
            "- source_checksum: %s\n"
            "\n"
            "# A\nkeep\n" % ("0" * 64),
        )
        self.assertEqual(
            packet.manifest_dict(),
            {
                "advisory_only": True,
                "budget_method": "four_chars_per_token",
                "checked_not_selected": [],
                "checksum": "eaa30231efcf096ac813e555a5cc20b01f8e6eebc4def04a1cc79da0161722bc",
                "estimated_tokens": 96,
                "expansion_count": 0,
                "max_context_tokens": 1000,
                "revision": 1,
                "selected": [
                    {
                        "estimated_tokens": 2,
                        "path": "/fixed/brain.md",
                        "priority": 0,
                        "reason": "r",
                        "section": "A",
                        "source_checksum": "0" * 64,
                    }
                ],
            },
        )

    def test_default_build_packet_matches_an_explicit_legacy_budget_method(self):
        from ai_review.context import BUDGET_METHOD

        source = self.write("brain.md", "# A\n審查重點\n")

        default_packet = build_packet([SourceRef(source, "A", "r")], max_tokens=1000)
        legacy_packet = build_packet(
            [SourceRef(source, "A", "r")], max_tokens=1000, budget_method=BUDGET_METHOD
        )

        self.assertEqual(default_packet.checksum, legacy_packet.checksum)
        self.assertEqual(default_packet.manifest_dict()["budget_method"], BUDGET_METHOD)
        self.assertEqual(default_packet.sources[0].estimated_tokens, 3)

    def test_doc_packet_budget_check_uses_the_non_ascii_estimate(self):
        from ai_review.context import (
            BUDGET_METHOD,
            BUDGET_METHOD_DOC,
            SourceExcerpt,
            _render_with_token_estimate,
        )

        section = "# A\n" + ("審查重點" * 40) + "\n"
        # A fixed path keeps the rendered packet, and therefore the number
        # _validate_budget sees, identical on every machine.
        excerpt = SourceExcerpt(
            path="/fixed/brain.md",
            section="A",
            reason="r",
            priority=0,
            source_checksum="0" * 64,
            markdown=section,
            estimated_tokens=42,
        )

        default_estimate, _ = _render_with_token_estimate((excerpt,), "0" * 64, BUDGET_METHOD)
        doc_estimate, _ = _render_with_token_estimate((excerpt,), "0" * 64, BUDGET_METHOD_DOC)

        self.assertEqual(default_estimate, 135)
        self.assertEqual(doc_estimate, 256)

        source = self.write("brain.md", section)
        self.assertEqual(
            build_packet([SourceRef(source, "A", "r")], max_tokens=1000)
            .sources[0].estimated_tokens,
            42,
        )
        self.assertEqual(
            build_packet(
                [SourceRef(source, "A", "r")], max_tokens=1000, budget_method=BUDGET_METHOD_DOC
            ).sources[0].estimated_tokens,
            162,
        )

    def test_cjk_content_within_the_ascii_budget_is_rejected_by_the_doc_method(self):
        from ai_review.context import BUDGET_METHOD_DOC

        source = self.write("brain.md", "# A\n" + ("審查重點" * 40) + "\n")
        budget = build_packet([SourceRef(source, "A", "r")], max_tokens=1000).estimated_tokens

        self.assertEqual(
            build_packet([SourceRef(source, "A", "r")], max_tokens=budget).estimated_tokens,
            budget,
        )
        with self.assertRaises(ContextBudgetError):
            build_packet(
                [SourceRef(source, "A", "r")], max_tokens=budget, budget_method=BUDGET_METHOD_DOC
            )

    def test_doc_packet_validates_and_keeps_its_budget_method(self):
        from ai_review.context import BUDGET_METHOD_DOC, validate_knowledge_packet

        source = self.write("brain.md", "# A\n審查重點說明\n")
        packet = build_packet(
            [SourceRef(source, "A", "r")], max_tokens=1000, budget_method=BUDGET_METHOD_DOC
        )

        validated = validate_knowledge_packet(packet)

        self.assertEqual(validated.budget_method, BUDGET_METHOD_DOC)
        self.assertEqual(validated.checksum, packet.checksum)
        self.assertEqual(validated.estimated_tokens, packet.estimated_tokens)
        self.assertEqual(validated.markdown, packet.markdown)

    def test_expansion_keeps_the_doc_budget_method(self):
        from ai_review.context import BUDGET_METHOD_DOC

        source = self.write("source.md", "# A\n原始來源說明\n")
        candidate = self.write("candidate.md", "# A\n候補來源說明\n")
        packet = build_packet(
            [SourceRef(source, "A", "r")], max_tokens=1000, budget_method=BUDGET_METHOD_DOC
        )

        expanded = expand_packet(packet, [SourceRef(candidate, "A", "x")])

        self.assertEqual(expanded.budget_method, BUDGET_METHOD_DOC)
        # "# A\n候補來源說明\n": five ASCII characters round up to two tokens,
        # plus one token for each of the six Han characters.
        self.assertEqual(expanded.sources[0].estimated_tokens, 8)

    def test_rendered_packet_header_reports_its_own_budget_method(self):
        from ai_review.context import BUDGET_METHOD, BUDGET_METHOD_DOC

        source = self.write("brain.md", "# A\n審查重點說明\n")

        default_packet = build_packet([SourceRef(source, "A", "r")], max_tokens=1000)
        doc_packet = build_packet(
            [SourceRef(source, "A", "r")], max_tokens=1000, budget_method=BUDGET_METHOD_DOC
        )

        self.assertIn("- budget_method: %s\n" % BUDGET_METHOD, default_packet.markdown)
        self.assertIn("- budget_method: %s\n" % BUDGET_METHOD_DOC, doc_packet.markdown)
        self.assertNotIn(BUDGET_METHOD_DOC, default_packet.markdown)

    def test_unknown_budget_method_is_rejected_at_both_entry_points(self):
        from ai_review.context import estimate_tokens

        source = self.write("brain.md", "# A\nkeep\n")

        with self.assertRaises(ContextBudgetError):
            estimate_tokens("keep", "made_up_method")
        with self.assertRaises(ContextBudgetError):
            build_packet(
                [SourceRef(source, "A", "r")], max_tokens=1000, budget_method="made_up_method"
            )

    def test_doc_estimate_rounds_ascii_once_for_the_whole_document(self):
        from ai_review.context import BUDGET_METHOD_DOC, estimate_tokens

        # Three ASCII characters split by Han characters: rounding each ASCII run
        # on its own would charge three tokens for them instead of one.
        self.assertEqual(estimate_tokens("a審b審c", BUDGET_METHOD_DOC), 3)
        # Eight ASCII characters in four runs: two tokens, not four.
        self.assertEqual(estimate_tokens("ab審cd審ef審gh", BUDGET_METHOD_DOC), 5)

    def test_doc_estimate_charges_every_non_ascii_codepoint_not_only_han(self):
        from ai_review.context import BUDGET_METHOD_DOC, estimate_tokens

        self.assertEqual(estimate_tokens("é" * 12, BUDGET_METHOD_DOC), 12)
        self.assertEqual(estimate_tokens("привет", BUDGET_METHOD_DOC), 6)
        self.assertEqual(estimate_tokens("  ", BUDGET_METHOD_DOC), 2)
