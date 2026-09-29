import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, MaritimeSARService  # noqa: E402


def deadline(hours: float = 6.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")


class ReliefHandoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.service = MaritimeSARService(self.db_path)
        self.incident = self.service.create_incident(
            "coord1", "coordinator", "SAR-101", "海燕号", 31.0, 122.0, 10.0, 3, "东海中心"
        )
        self.original = self.service.add_asset(
            "coord1", "coordinator", "海巡01", "vessel", ["surface", "night"], 31.0, 122.0, 20, 120, 5
        )
        self.relief = self.service.add_asset(
            "coord1", "coordinator", "海巡02", "vessel", ["surface"], 30.9, 121.9, 18, 150, 5
        )
        self.area = self.service.create_search_area(
            "coord1", "coordinator", self.incident["id"], "A-101", "surface", 31.05, 122.05, 8, 1
        )
        self.service.assign_area("coord1", "coordinator", self.area["id"], self.original["id"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_reserve_keeps_original_responsible_and_switch_transfers(self):
        handover = self.service.reserve_relief(
            "coord1", "coordinator", self.area["id"], self.relief["id"], deadline(6)
        )
        self.assertEqual("reserved", handover["status"])
        self.assertEqual(self.original["id"], handover["original_asset_id"])
        state = self.service.state()
        area_view = next(a for a in state["search_areas"] if a["id"] == self.area["id"])
        # 确认前：接替船已预留，原船仍是当前责任人，区域不空
        self.assertEqual("reserved", next(a for a in state["assets"] if a["id"] == self.relief["id"])["status"])
        self.assertEqual("assigned", next(a for a in state["assets"] if a["id"] == self.original["id"])["status"])
        self.assertEqual(self.original["id"], area_view["current_asset_id"])
        self.assertEqual(self.relief["id"], area_view["relief_asset_id"])
        self.assertIsNone(area_view["switched_at"])
        switched = self.service.confirm_relief("coord1", "coordinator", handover["id"])
        self.assertEqual("switched", switched["status"])
        self.assertIsNotNone(switched["switched_at"])
        state = self.service.state()
        area_view = next(a for a in state["search_areas"] if a["id"] == self.area["id"])
        self.assertEqual(self.relief["id"], area_view["current_asset_id"])
        self.assertEqual(self.original["id"], area_view["original_asset_id"])
        self.assertEqual(self.relief["id"], area_view["assigned_asset_id"])
        self.assertEqual("available", next(a for a in state["assets"] if a["id"] == self.original["id"])["status"])
        self.assertEqual("assigned", next(a for a in state["assets"] if a["id"] == self.relief["id"])["status"])

    def test_reserve_validates_sea_state_capability_range_and_eta(self):
        weak = self.service.add_asset(
            "coord1", "coordinator", "小艇", "vessel", ["surface"], 30.9, 121.9, 18, 150, 2
        )
        with self.assertRaisesRegex(DomainError, "海况"):
            self.service.reserve_relief("coord1", "coordinator", self.area["id"], weak["id"], deadline(6))
        air = self.service.add_asset(
            "coord1", "coordinator", "救助直升机", "aircraft", ["air"], 30.9, 121.9, 180, 300, 5
        )
        with self.assertRaisesRegex(DomainError, "能力"):
            self.service.reserve_relief("coord1", "coordinator", self.area["id"], air["id"], deadline(6))
        short = self.service.add_asset(
            "coord1", "coordinator", "近岸艇", "vessel", ["surface"], 30.9, 121.9, 18, 2, 5
        )
        with self.assertRaisesRegex(DomainError, "航程"):
            self.service.reserve_relief("coord1", "coordinator", self.area["id"], short["id"], deadline(6))
        slow = self.service.add_asset(
            "coord1", "coordinator", "慢船", "vessel", ["surface"], 30.9, 121.9, 1, 200, 5
        )
        with self.assertRaisesRegex(DomainError, "晚于"):
            self.service.reserve_relief("coord1", "coordinator", self.area["id"], slow["id"], deadline(0.02))

    def test_two_coordinators_cannot_grab_same_relief(self):
        self.service.reserve_relief("coord1", "coordinator", self.area["id"], self.relief["id"], deadline(6))
        area2 = self.service.create_search_area(
            "coord2", "coordinator", self.incident["id"], "A-102", "surface", 31.06, 122.06, 8, 2
        )
        with self.assertRaises(DomainError) as ctx:
            self.service.reserve_relief("coord2", "coordinator", area2["id"], self.relief["id"], deadline(6))
        self.assertEqual(409, ctx.exception.status)
        # 已有预留的区域不能再次预留
        with self.assertRaises(DomainError):
            self.service.reserve_relief("coord1", "coordinator", self.area["id"], self.relief["id"], deadline(6))

    def test_fail_relief_releases_relief_and_keeps_reason_original_stays(self):
        handover = self.service.reserve_relief(
            "coord1", "coordinator", self.area["id"], self.relief["id"], deadline(6)
        )
        failed = self.service.fail_relief(
            "coord1", "coordinator", "接替船主机故障", handover_id=handover["id"]
        )
        self.assertEqual("failed", failed["status"])
        self.assertEqual("接替船主机故障", failed["failure_reason"])
        state = self.service.state()
        self.assertEqual("available", next(a for a in state["assets"] if a["id"] == self.relief["id"])["status"])
        area_view = next(a for a in state["search_areas"] if a["id"] == self.area["id"])
        self.assertEqual(self.original["id"], area_view["current_asset_id"])
        self.assertEqual("接替船主机故障", area_view["handover_failure_reason"])
        # 失败后可以重新预留另一艘船
        other = self.service.add_asset(
            "coord1", "coordinator", "海巡03", "vessel", ["surface"], 30.95, 121.95, 18, 150, 5
        )
        again = self.service.reserve_relief("coord1", "coordinator", self.area["id"], other["id"], deadline(6))
        self.assertEqual("reserved", again["status"])

    def test_withdraw_relief_or_original_cancels_reservation(self):
        handover = self.service.reserve_relief(
            "coord1", "coordinator", self.area["id"], self.relief["id"], deadline(6)
        )
        relief = next(a for a in self.service.list_assets() if a["id"] == self.relief["id"])
        self.service.withdraw_asset("coord1", "coordinator", self.relief["id"], "接替船取消出动", relief["version"])
        state = self.service.state()
        record = next(h for h in state["area_handovers"] if h["id"] == handover["id"])
        self.assertEqual("failed", record["status"])
        self.assertIn("接替船取消出动", record["failure_reason"])
        self.assertEqual(self.original["id"], next(a for a in state["search_areas"] if a["id"] == self.area["id"])["current_asset_id"])
        # 再走一遍：撤回原船也会作废预留
        other = self.service.add_asset(
            "coord1", "coordinator", "海巡03", "vessel", ["surface"], 30.95, 121.95, 18, 150, 5
        )
        handover2 = self.service.reserve_relief("coord1", "coordinator", self.area["id"], other["id"], deadline(6))
        original = next(a for a in self.service.list_assets() if a["id"] == self.original["id"])
        self.service.withdraw_asset("coord1", "coordinator", self.original["id"], "燃油不足返航", original["version"])
        state = self.service.state()
        self.assertEqual("failed", next(h for h in state["area_handovers"] if h["id"] == handover2["id"])["status"])
        self.assertEqual("available", next(a for a in state["assets"] if a["id"] == other["id"])["status"])

    def test_restart_returns_reserved_handover_to_original(self):
        handover = self.service.reserve_relief(
            "coord1", "coordinator", self.area["id"], self.relief["id"], deadline(6)
        )
        MaritimeSARService(self.db_path)  # 模拟服务重启
        state = self.service.state()
        record = next(h for h in state["area_handovers"] if h["id"] == handover["id"])
        self.assertEqual("failed", record["status"])
        self.assertIn("服务重启", record["failure_reason"])
        self.assertEqual("available", next(a for a in state["assets"] if a["id"] == self.relief["id"])["status"])
        self.assertEqual(self.original["id"], next(a for a in state["search_areas"] if a["id"] == self.area["id"])["current_asset_id"])

    def test_legacy_assigned_area_without_handover_stays_compatible(self):
        area2 = self.service.create_search_area(
            "coord1", "coordinator", self.incident["id"], "A-103", "surface", 31.07, 122.07, 8, 3
        )
        other = self.service.add_asset(
            "coord1", "coordinator", "海巡03", "vessel", ["surface"], 30.95, 121.95, 18, 150, 5
        )
        self.service.assign_area("coord1", "coordinator", area2["id"], other["id"])
        area_view = next(a for a in self.service.state()["search_areas"] if a["id"] == area2["id"])
        self.assertIsNone(area_view["handover_status"])
        self.assertEqual(other["id"], area_view["current_asset_id"])
        self.assertEqual(other["id"], area_view["original_asset_id"])
        self.assertIsNone(area_view["relief_asset_id"])
        self.assertIsNone(area_view["switched_at"])

    def test_complete_area_and_false_alarm_cancel_reservation(self):
        handover = self.service.reserve_relief(
            "coord1", "coordinator", self.area["id"], self.relief["id"], deadline(6)
        )
        area_view = next(a for a in self.service.state()["search_areas"] if a["id"] == self.area["id"])
        self.service.complete_area("coord1", "coordinator", self.area["id"], "completed", area_view["version"])
        record = next(h for h in self.service.state()["area_handovers"] if h["id"] == handover["id"])
        self.assertEqual("failed", record["status"])
        self.assertEqual("available", next(a for a in self.service.state()["assets"] if a["id"] == self.relief["id"])["status"])

    def test_reserve_requires_coordinator_role(self):
        with self.assertRaises(DomainError) as ctx:
            self.service.reserve_relief("view1", "viewer", self.area["id"], self.relief["id"], deadline(6))
        self.assertEqual(403, ctx.exception.status)


if __name__ == "__main__":
    unittest.main()
