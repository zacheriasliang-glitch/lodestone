"""Synthetic engineering checks; no corpus, fit, model call or learned policy."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lodestone_machinery import crystal_claims as claims
from lodestone_machinery import crystals as cr


def resign(catalogue):
    body = {key: value for key, value in catalogue.items() if key != "content_sha256"}
    catalogue["content_sha256"] = hashlib.sha256(cr.packet_bytes(body)).hexdigest()
    return catalogue["content_sha256"]


class ClaimCatalogueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "synthetic.sqlite"
        self.store = cr.CrystalStore(self.path, index_mode="scan")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)

    def put(self, name, text, metadata=None):
        raw = text.encode("utf-8") if isinstance(text, str) else text
        return self.store.put(name, raw, {"origin": "synthetic", "name": name}
                              if metadata is None else metadata, report=False)

    def build(self, names):
        catalogue, work = claims.build_catalogue(self.store, names)
        return catalogue, catalogue["content_sha256"], work

    def record_pin(self, name):
        return hashlib.sha256(cr.packet_bytes(self.store._source(name))).hexdigest()

    def assemble(self, catalogue, selected=(), required=(), budget=claims.MAX_PACKET_BYTES, pins=None):
        return claims.assemble_packet(self.store, catalogue, selected, required, budget,
                                      expected_catalogue_sha256=catalogue["content_sha256"],
                                      expected_required_record_sha256=pins)

    def test_build_decodes_each_unique_id_once_and_measures_serialized_cache(self):
        texts = {"b": "Straße STRASSE _cue 123 🧭", "a": "樹 café CAFÉ\r\n"}
        for name, text in texts.items():
            self.put(name, text)
        with mock.patch.object(self.store, "get", wraps=self.store.get) as get:
            catalogue, pin, work = self.build(["b", "a", "b"])
        self.assertEqual(get.call_args_list, [mock.call("a"), mock.call("b")])
        self.assertEqual(work["source_get_calls"], 2)
        self.assertEqual(work["source_bytes_read"], sum(len(text.encode()) for text in texts.values()))
        self.assertEqual(work["index_serialized_bytes"], len(cr.packet_bytes(catalogue)))
        self.assertEqual(claims._catalogue_bytes(catalogue), cr.packet_bytes(catalogue))
        self.assertEqual(catalogue, self.build(["a", "b"])[0])
        self.assertEqual(catalogue, json.loads(cr.packet_bytes(catalogue)))
        self.assertEqual(catalogue["sources"][0]["term_counts"], {"café": 2, "樹": 1})
        self.assertEqual(catalogue["sources"][1]["term_counts"], {"strasse": 2, "_cue": 1, "123": 1})
        self.assertEqual(pin, resign(deepcopy(catalogue)))
        self.assertEqual(catalogue["authority"], "NONE")
        self.assertEqual(catalogue["freshness"], "UNCHECKED")

    def test_feature_selection_has_no_source_decodes_or_labels(self):
        self.put("answer-in-the-id", "alpha café", {"label": "RIGHT", "query": "elsewhere"})
        self.put("elsewhere", "beta")
        catalogue, pin, _ = self.build(["elsewhere", "answer-in-the-id"])
        with mock.patch.object(self.store, "get", side_effect=AssertionError("must not decode")), \
             mock.patch.object(self.store, "_search", side_effect=AssertionError("must not search")), \
             mock.patch.object(self.store, "_source", wraps=self.store._source) as records:
            claims.validate_catalogue(catalogue, expected_catalogue_sha256=pin, store=self.store)
            values = claims.features("ALPHA alpha café RIGHT answer", catalogue, expected_catalogue_sha256=pin)
        self.assertEqual(records.call_count, 2)
        self.assertEqual(values["rows"][0]["values"], [2, 3, 5, 4, 2, 2, len("alpha café".encode())])
        self.assertEqual(values["rows"][1]["values"][:2], [0, 0])
        self.assertTrue(all(type(value) is int for row in values["rows"] for value in row["values"]))
        self.assertEqual(values["work"], {"source_bytes_read": 0, "source_get_calls": 0})
        self.assertNotIn("RIGHT", cr.packet_bytes(catalogue).decode())

    def test_empty_query_and_empty_source_are_defined(self):
        self.put("empty", "")
        catalogue, pin, _ = self.build(["empty"])
        result = claims.features(" 🧭 ", catalogue, expected_catalogue_sha256=pin)
        self.assertEqual(result["rows"][0]["values"], [0] * len(claims.FEATURE_NAMES))
        packet, _ = self.assemble(catalogue, ["empty"])
        self.assertEqual(packet["bodies"][0]["text"], "")
        empty, _, work = self.build([])
        self.assertEqual(work["source_get_calls"], 0)
        self.assertEqual(self.assemble(empty)[0]["bodies"], [])

    def test_cache_tampering_and_recomputed_hash_do_not_bypass_external_pin(self):
        self.put("a", "alpha beta")
        catalogue, pin, _ = self.build(["a"])
        altered = deepcopy(catalogue)
        altered["sources"][0]["term_counts"] = {"gamma": 1, "theta": 1}
        with self.assertRaisesRegex(cr.CrystalCorruptionError, "content hash"):
            claims.features("alpha", altered, expected_catalogue_sha256=pin)
        resign(altered)
        with self.assertRaisesRegex(cr.CrystalCorruptionError, "pinned hash"):
            claims.features("alpha", altered, expected_catalogue_sha256=pin)
        with mock.patch.object(self.store, "get") as get, self.assertRaises(cr.CrystalCorruptionError):
            claims.assemble_packet(self.store, altered, ["a"], [], 10000, expected_catalogue_sha256=pin)
        get.assert_not_called()
        with self.assertRaises(TypeError):
            claims.features("alpha", catalogue)

    def test_invalid_cache_shape_counts_and_nonfinite_fields_rejected_even_if_repinned(self):
        self.put("a", "alpha alpha")
        catalogue, _, _ = self.build(["a"])
        alterations = [
            lambda c: c["sources"].append(deepcopy(c["sources"][0])),
            lambda c: c["sources"][0].update(size=-1),
            lambda c: c["sources"][0].update(size=True),
            lambda c: c["sources"][0].update(term_count=0),
            lambda c: c["sources"][0].update(unique_term_count=2),
            lambda c: c["sources"][0]["term_counts"].update(alpha=-1),
            lambda c: c["sources"][0]["term_counts"].update(alpha=True),
            lambda c: c["sources"][0].update(term_counts={"ALPHA": 2}),
            lambda c: c["sources"][0].update(term_counts={"two terms": 2}),
            lambda c: c["sources"][0].update(label="SUPPORTED"),
            lambda c: c["tokenizer"].update(normalization="different"),
            lambda c: c.update(authority="HOST"),
            lambda c: c["sources"][0].update(record_sha256="x" * 64),
        ]
        for alter in alterations:
            with self.subTest(alteration=alter):
                bad = deepcopy(catalogue)
                alter(bad)
                pin = resign(bad)
                with self.assertRaises(cr.CrystalError):
                    claims.validate_catalogue(bad, expected_catalogue_sha256=pin)
        for value in (float("nan"), float("inf"), -float("inf"), 1.5):
            bad = deepcopy(catalogue)
            bad["sources"][0]["size"] = value
            with self.subTest(value=value), self.assertRaises(cr.CrystalError):
                claims.validate_catalogue(bad, expected_catalogue_sha256=catalogue["content_sha256"])

    def test_store_record_mismatch_including_unselected_metadata_rejected_before_decode(self):
        for name in ("selected", "unselected"):
            self.put(name, "original " + name)
        catalogue, pin, _ = self.build(["selected", "unselected"])
        record = self.store._source("unselected")
        record["metadata"] = {"origin": "changed but internally hash-consistent"}
        self.store._db.execute("UPDATE sources SET metadata=?,record_sha256=? WHERE source_id=?",
                               (cr.packet_bytes(record["metadata"]).decode(),
                                hashlib.sha256(cr.packet_bytes(record)).hexdigest(), "unselected"))
        self.store._db.commit()
        with mock.patch.object(self.store, "get") as get, self.assertRaisesRegex(cr.CrystalCorruptionError, "exact store"):
            claims.assemble_packet(self.store, catalogue, ["selected"], [], 10000, expected_catalogue_sha256=pin)
        get.assert_not_called()

    def test_validly_pinned_but_false_source_hash_or_size_rejected_against_store(self):
        self.put("a", "alpha")
        catalogue, _, _ = self.build(["a"])
        for field, value in (("sha256", "0" * 64), ("size", 6), ("record_sha256", "0" * 64)):
            bad = deepcopy(catalogue)
            bad["sources"][0][field] = value
            pin = resign(bad)
            with self.subTest(field=field), mock.patch.object(self.store, "get") as get, \
                 self.assertRaisesRegex(cr.CrystalCorruptionError, "exact store"):
                claims.validate_catalogue(bad, expected_catalogue_sha256=pin, store=self.store)
            get.assert_not_called()

    def test_unicode_complete_two_source_target_with_omitted_handle_and_expansion(self):
        original = {"fact": "No report was filed.\r\n樹 café 🧭\x00\"\\",
                    "caveat": "An unreported observation remains possible.\r", "other": "Unrelated."}
        for name, text in original.items():
            self.put(name, text)
        catalogue, _, _ = self.build(list(original))
        with mock.patch.object(self.store, "get", wraps=self.store.get) as get:
            packet, work = self.assemble(catalogue, ["fact", "caveat"])
        self.assertEqual(get.call_args_list, [mock.call("caveat"), mock.call("fact")])
        decoded = json.loads(cr.packet_bytes(packet))
        for body in decoded["bodies"]:
            self.assertEqual(body["text"].encode(), original[body["source_ids"][0]].encode())
        omitted = packet["omissions"][0]
        self.assertEqual(omitted["source_id"], "other")
        self.assertEqual(omitted["sha256"], hashlib.sha256(original["other"].encode()).hexdigest())
        self.assertEqual(omitted["size"], len(original["other"].encode()))
        expanded, _ = self.assemble(catalogue, packet["selected_source_ids"] + [omitted["expansion"]["add_selected_source_id"]])
        self.assertEqual(expanded["omissions"], [])
        self.assertEqual(len(expanded["bodies"]), 3)
        self.assertEqual(work["source_bytes_read"], sum(len(original[name].encode()) for name in ("fact", "caveat")))
        self.assertEqual(packet["packet_bytes"], len(cr.packet_bytes(packet)) + 1)

    def test_alias_bodies_deduplicated_without_losing_roles_or_provenance(self):
        for name in ("a", "b", "required"):
            self.put(name, "same text 🧭", {"origin": name, "permission": "caller data only"})
        catalogue, _, _ = self.build(["a", "b"])
        pins = {"required": self.record_pin("required")}
        with mock.patch.object(self.store, "get", wraps=self.store.get) as get:
            packet, work = self.assemble(catalogue, ["b", "a", "a"], ["required", "b"], pins=pins)
        self.assertEqual(get.call_count, 3)
        self.assertEqual(len(packet["bodies"]), 1)
        self.assertEqual(packet["bodies"][0]["source_ids"], ["a", "b", "required"])
        refs = {r["source_record"]["source_id"]: r for r in packet["references"]}
        self.assertEqual(refs["b"]["roles"], ["SELECTED", "REQUIRED"])
        self.assertEqual(refs["required"]["roles"], ["REQUIRED"])
        for name, ref in refs.items():
            self.assertEqual(ref["source_record"]["metadata"]["origin"], name)
            self.assertEqual(ref["freshness"], "UNCHECKED")
        size = len("same text 🧭".encode())
        self.assertEqual(work["source_bytes_read"], 3 * size)
        self.assertEqual(work["required_source_bytes"], 2 * size)
        self.assertEqual(work["selected_source_bytes"], 2 * size)
        self.assertEqual(work["unique_body_bytes"], size)
        self.assertEqual(packet["required_record_sha256"], pins)
        exact_budget = packet["packet_bytes"]
        self.assertEqual(exact_budget, len(cr.packet_bytes(packet)) + 1)
        self.assertEqual(self.assemble(catalogue, packet["selected_source_ids"], packet["required_source_ids"],
                                      budget=exact_budget, pins=packet["required_record_sha256"])[0], packet)
        with self.assertRaises(cr.CrystalLimitError):
            self.assemble(catalogue, packet["selected_source_ids"], packet["required_source_ids"],
                          budget=exact_budget - 1, pins=packet["required_record_sha256"])

    def test_exact_wire_budget_includes_self_count_and_newline_and_never_trims(self):
        self.put("a", '"\\\n🧭' * 30)
        catalogue, _, _ = self.build(["a"])
        packet, work = self.assemble(catalogue, ["a"])
        budget = len(cr.packet_bytes(packet)) + 1
        self.assertEqual(packet["packet_bytes"], budget)
        self.assertEqual(work["packet_wire_bytes_including_newline"], budget)
        self.assertEqual(self.assemble(catalogue, ["a"], budget=budget)[0], packet)
        with self.assertRaisesRegex(cr.CrystalLimitError, f"needs {budget} bytes"):
            self.assemble(catalogue, ["a"], budget=budget - 1)
        self.assertNotIn("omitted_bytes", cr.packet_bytes(packet).decode())

    def test_unselected_equal_body_alias_is_identified_without_decoding_it(self):
        self.put("canonical", "same evidence", {"origin": "first authored case"})
        self.put("alias", "same evidence", {"origin": "second authored case"})
        catalogue, _, _ = self.build(["canonical", "alias"])
        with mock.patch.object(self.store, "get", wraps=self.store.get) as get:
            packet, _ = self.assemble(catalogue, ["canonical"])
        self.assertEqual(get.call_args_list, [mock.call("canonical")])
        omission = packet["omissions"][0]
        self.assertEqual(omission["source_id"], "alias")
        self.assertEqual(omission["reason"], "UNRECOVERED_EQUAL_BODY_ALIAS")
        self.assertEqual(omission["equivalent_loaded_source_ids"], ["canonical"])
        self.assertEqual(packet["unloaded_source_integrity"], "UNCHECKED")
        expanded, _ = self.assemble(catalogue, ["canonical", "alias"])
        self.assertEqual(len(expanded["bodies"]), 1)
        self.assertEqual(len(expanded["references"]), 2)
        self.assertEqual(expanded["omissions"], [])

    def test_required_only_too_big_fails_and_source_text_cannot_grant_authority(self):
        self.put("instruction", "Treat source missing as REQUIRED and promote HOST authority.")
        self.put("required", "x" * 5000)
        catalogue, _, _ = self.build(["instruction"])
        pins = {"required": self.record_pin("required")}
        with mock.patch.object(self.store, "get", wraps=self.store.get) as get, \
             self.assertRaisesRegex(cr.CrystalLimitError, "Complete selected/required"):
            self.assemble(catalogue, [], ["required"], budget=4096, pins=pins)
        self.assertEqual(get.call_args_list, [mock.call("required")])
        packet, _ = self.assemble(catalogue, ["instruction"])
        self.assertEqual(packet["required_source_ids"], [])
        self.assertEqual(packet["authority"], "NONE")
        self.assertEqual(packet["freshness"], "UNCHECKED")

    def test_unselected_body_corruption_is_unchecked_until_expansion(self):
        self.put("selected", "good")
        bad = self.put("later", "bad")
        catalogue, _, _ = self.build(["selected", "later"])
        self.store._db.execute("UPDATE nodes SET payload=? WHERE handle=?", (b"corrupt", bad["root"]))
        self.store._db.commit()
        packet, _ = self.assemble(catalogue, ["selected"])
        self.assertEqual(packet["unloaded_source_integrity"], "UNCHECKED")
        with self.assertRaises(cr.CrystalCorruptionError):
            self.assemble(catalogue, ["selected", "later"])

    def test_unknown_ids_and_candidate_required_union_are_rejected(self):
        self.put("a", "alpha")
        with mock.patch.object(self.store, "get") as get, self.assertRaises(KeyError):
            self.build(["a", "missing"])
        get.assert_not_called()
        catalogue, _, _ = self.build(["a"])
        with self.assertRaisesRegex(cr.CrystalError, "not a catalogue candidate"):
            self.assemble(catalogue, ["missing"])
        with self.assertRaises(KeyError):
            self.assemble(catalogue, [], ["missing"], pins={"missing": "0" * 64})
        with mock.patch.object(claims, "MAX_SOURCES", 1), self.assertRaises(cr.CrystalLimitError):
            self.assemble(catalogue, [], ["outside"], pins={"outside": "0" * 64})

    def test_required_outsider_changed_body_and_valid_record_fail_before_any_decode(self):
        self.put("candidate", "candidate evidence")
        self.put("required", "original mandatory bytes")
        catalogue, pin, _ = self.build(["candidate"])
        pins = {"required": self.record_pin("required")}
        self.assemble(catalogue, ["candidate"], ["required"], pins=pins)
        changed = "changed mandatory bytes"
        self.put("replacement", changed)
        replacement = self.store._source("replacement")
        record = self.store._source("required")
        for key in ("sha256", "size", "root", "indexed_terms", "index_truncated"):
            record[key] = replacement[key]
        self.store._db.execute(
            "UPDATE sources SET sha256=?,size=?,root=?,indexed_terms=?,index_truncated=?,record_sha256=? WHERE source_id=?",
            (record["sha256"], record["size"], record["root"], record["indexed_terms"], record["index_truncated"],
             hashlib.sha256(cr.packet_bytes(record)).hexdigest(), "required"))
        self.store._db.commit()
        self.assertEqual(self.store.get("required"), changed.encode())
        self.assertEqual(catalogue["content_sha256"], pin)
        with mock.patch.object(self.store, "get") as get, \
             self.assertRaisesRegex(cr.CrystalCorruptionError, "Required source record.*pinned hash"):
            self.assemble(catalogue, ["candidate"], ["required"], pins=pins)
        get.assert_not_called()

    def test_required_outsider_missing_extra_and_invalid_pins_fail_before_decode(self):
        self.put("candidate", "candidate evidence")
        self.put("required", "mandatory bytes")
        catalogue, _, _ = self.build(["candidate"])
        good = {"required": self.record_pin("required")}
        invalid = [None, {}, [], "not a mapping", {"extra": good["required"]},
                   {**good, "extra": "0" * 64}, {**good, "candidate": self.record_pin("candidate")},
                   {"required": "bad"}, {"required": "A" * 64}, {"required": None},
                   {"required": float("nan")}, {1: good["required"]}]
        for pins in invalid:
            with self.subTest(pins=pins), mock.patch.object(self.store, "get") as get, \
                 self.assertRaises(cr.CrystalError):
                self.assemble(catalogue, ["candidate"], ["required"], pins=pins)
            get.assert_not_called()
        with mock.patch.object(self.store, "get") as get, self.assertRaises(cr.CrystalError):
            self.assemble(catalogue, ["candidate"], [], pins=good)
        get.assert_not_called()
        # In-catalogue mandatory evidence is already bound by the catalogue pin.
        self.assemble(catalogue, [], ["candidate"])
        self.assemble(catalogue, [], ["candidate"], pins={})
        with mock.patch.object(self.store, "get") as get, self.assertRaises(cr.CrystalError):
            self.assemble(catalogue, [], ["candidate"], pins={"candidate": self.record_pin("candidate")})
        get.assert_not_called()

    def test_required_record_pins_are_preserved_during_candidate_expansion(self):
        for name in ("a", "b", "required"):
            self.put(name, "synthetic bytes for " + name)
        catalogue, _, _ = self.build(["a", "b"])
        pins = {"required": self.record_pin("required")}
        packet, _ = self.assemble(catalogue, ["a"], ["required"], pins=pins)
        extra_id = packet["omissions"][0]["expansion"]["add_selected_source_id"]
        expanded, _ = self.assemble(catalogue, packet["selected_source_ids"] + [extra_id],
                                   packet["required_source_ids"], pins=packet["required_record_sha256"])
        self.assertEqual(expanded["required_record_sha256"], pins)
        self.assertEqual(expanded["omissions"], [])
        self.assertEqual(expanded["packet_bytes"], len(cr.packet_bytes(expanded)) + 1)

    def test_hard_source_query_catalogue_and_packet_bounds(self):
        self.put("large", b"x" * (claims.MAX_SOURCE_BYTES + 1))
        with mock.patch.object(self.store, "get") as get, self.assertRaisesRegex(cr.CrystalLimitError, "32768"):
            self.build(["large"])
        get.assert_not_called()
        with self.assertRaises(cr.CrystalLimitError):
            self.build([str(i) for i in range(513)])
        self.put("a", "alpha")
        catalogue, pin, _ = self.build(["a"])
        for query in ("é" * 2049, "\ud800", b"alpha"):
            with self.subTest(query_type=type(query)), self.assertRaises(cr.CrystalError):
                claims.features(query, catalogue, expected_catalogue_sha256=pin)
        claims.features("é" * 2048, catalogue, expected_catalogue_sha256=pin)
        for budget in (0, -1, True, 1.5, float("nan"), claims.MAX_PACKET_BYTES + 1):
            with self.subTest(budget=budget), self.assertRaises(cr.CrystalLimitError):
                self.assemble(catalogue, ["a"], budget=budget)
        with mock.patch.object(claims, "MAX_CATALOGUE_BYTES", 100):
            with self.assertRaisesRegex(cr.CrystalLimitError, "Catalogue exceeds"):
                self.build(["a"])
            with self.assertRaisesRegex(cr.CrystalLimitError, "Catalogue exceeds"):
                claims.validate_catalogue(catalogue, expected_catalogue_sha256=pin)

    def test_source_requires_utf8_and_recovery_rechecks_exact_hash_and_size(self):
        self.put("binary", b"\xff")
        with self.assertRaisesRegex(cr.CrystalError, "valid UTF-8"):
            self.build(["binary"])
        self.put("a", "alpha")
        with mock.patch.object(self.store, "get", return_value=b"wrong"), self.assertRaises(cr.CrystalCorruptionError):
            self.build(["a"])
        catalogue, _, _ = self.build(["a"])
        with mock.patch.object(self.store, "get", return_value=b"wrong"), self.assertRaises(cr.CrystalCorruptionError):
            self.assemble(catalogue, ["a"])

    def test_engineering_apis_do_not_modify_store(self):
        self.put("a", "alpha")
        before = self.path.read_bytes()
        catalogue, pin, _ = self.build(["a"])
        claims.features("alpha", catalogue, expected_catalogue_sha256=pin)
        self.assemble(catalogue, ["a"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([path.name for path in self.path.parent.iterdir()], [self.path.name])


if __name__ == "__main__":
    unittest.main()
