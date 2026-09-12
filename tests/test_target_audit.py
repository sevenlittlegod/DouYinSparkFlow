import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("target_audit", Path(__file__).resolve().parents[1] / "docker" / "audit-targets.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def identity(number, nickname="Same", remark="Shared"):
    return {"short_id": str(number), "unique_id": f"id_{number}", "sec_uid": f"sec_{number}",
            "nickname": nickname, "remark_name": remark}


class TargetAuditTests(unittest.TestCase):
    def test_same_names_remain_separate_and_are_ambiguous(self):
        store = audit.IdentityStore()
        store.add(identity(1))
        store.add(identity(2))
        store.add(identity(1))
        self.assertEqual(len(store.records), 2)
        rows = audit.match_targets(["Shared", "id_1"], list(store.records.values()))
        self.assertEqual(rows[0]["status"], "ambiguous")
        self.assertEqual(len(rows[0]["identities"]), 2)
        self.assertEqual(rows[1]["status"], "matched")
        self.assertEqual(rows[1]["identities"][0]["unique_id"], "id_1")
        self.assertTrue(rows[1]["sender_title_collision"])
        self.assertEqual(len(rows[1]["identities"]), 1)

    def test_exact_id_takes_priority_over_someone_elses_nickname(self):
        rows = audit.match_targets(["id_1", "ID_1", "999"], [identity(1), identity(2, nickname="id_1")])
        self.assertEqual(rows[0]["status"], "matched")
        self.assertEqual(rows[0]["matched_by"], "id")
        self.assertEqual(rows[0]["identities"][0]["short_id"], "1")
        self.assertEqual(rows[1]["status"], "missing")
        self.assertEqual(rows[2]["status"], "missing")

    def test_output_keeps_original_names_and_excludes_unrelated_profiles(self):
        records = [identity(1, nickname="  阿甲\u3000", remark=""), identity(2, nickname="Unrelated")]
        rows = audit.match_targets(["阿甲"], records)
        self.assertEqual(rows[0]["identities"][0]["nickname"], "  阿甲\u3000")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.json"
            audit.save_report({"accounts": [{"targets": rows}]}, path)
            saved = path.read_text(encoding="utf-8")
            self.assertNotIn("Unrelated", saved)
            self.assertEqual(json.loads(saved)["accounts"][0]["targets"][0]["status"], "matched")


if __name__ == "__main__":
    unittest.main()
