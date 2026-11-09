"""Only newly submitted receipts get the current accounting marker."""

import tempfile
import unittest
from pathlib import Path

from src.services.vids.gateway import VidsGateway


class VidsReceiptTrackingTests(unittest.TestCase):
    def test_old_download_is_not_marked_after_another_new_generation(self):
        with tempfile.TemporaryDirectory() as root:
            gateway = VidsGateway(None, root)
            fresh = "vids_" + "a" * 64
            historical = "vids_" + "b" * 64
            gateway._vids_outcome_tracking = {fresh}
            gateway._save({"id": fresh, "kind": "video", "status": "generating"})
            self.assertEqual(gateway.get(fresh)["outcome_tracking_version"], 1)
            gateway._vids_outcome_tracking.clear()
            gateway._save({"id": historical, "kind": "video", "status": "completed"})
            self.assertNotIn("outcome_tracking_version", gateway.get(historical))
            resumed = gateway.get(fresh)
            resumed["status"] = "completed"
            gateway._save(resumed)
            self.assertEqual(gateway.get(fresh)["outcome_tracking_version"], 1)
            self.assertEqual(len(list(Path(root).glob("vids_*.json"))), 2)


if __name__ == "__main__":
    unittest.main()
