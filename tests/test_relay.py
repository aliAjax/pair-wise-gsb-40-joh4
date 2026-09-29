import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, MaritimeSARService  # noqa: E402


def deadline(hours: float = 6) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")


class RelayHandoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.service = MaritimeSARService(self.db_path)
        self.incident = self.service.create_incident(
            "coord1", "coordinator", "SAR-101", "海燕号", 31.0, 122.0, 10.0, 3, "东海中心"
        )
        self.outgoing = self.service.add_asset(
            "coord1", "coordinator", "海巡01", "vessel", ["surface", "night"], 31.0, 122.0, 20, 100, 5
        )
        self.incoming = self.service.add_asset(
            "coord1", "coordinator", "海巡02", "vessel", ["surface"], 31.0, 122.1, 20, 100, 5
        )
        self.area = self.service.create_search_area(
            "coord1", "coordinator", self.incident["id"], "A-101", "surface", 31.1, 122.1, 8, 1
        )
        self.service.assign_area("coord1", "coordinator", self.area["id"], self.outgoing["id"], self.outgoing["version"])

    def tearDown(self):
        self.tmp.cleanup()

    def _state_area(self):
        return next(a for a in self.service.state()["search_areas"] if a["id"] == self.area["id"])

    def test_reserve_then_confirm_switches_responsibility(self):
        reserve = self.service.reserve_relay(
            "coord1", "coordinator", self.area["id"], self.incoming["id"], deadline()
        )
        self.assertEqual("reserved", reserve["status"])
        # 确认前原船继续担责
        self.assertEqual(self.outgoing["id"], reserve["current_asset_id"])
        assets = {a["id"]: a["status"] for a in self.service.state()["assets"]}
        self.assertEqual("assigned", assets[self.outgoing["id"]])
        self.assertEqual("reserved", assets[self.incoming["id"]])
        area_view = self._state_area()
        self.assertEqual("海巡01", area_view["outgoing_asset_name"])
        self.assertEqual("海巡02", area_view["incoming_asset_name"])
        self.assertIsNone(area_view["switched_at"])
        self.assertEqual(self.outgoing["id"], area_view["current_asset_id"])

        confirmed = self.service.confirm_relay(
            "coord1", "coordinator", reserve["id"], expected_incoming_version=self.incoming["version"] + 1
        )
        self.assertEqual("switched", confirmed["status"])
        self.assertIsNotNone(confirmed["switched_at"])
        self.assertEqual(self.incoming["id"], confirmed["current_asset_id"])
        assets = {a["id"]: a["status"] for a in self.service.state()["assets"]}
        self.assertEqual("available", assets[self.outgoing["id"]])
        self.assertEqual("assigned", assets[self.incoming["id"]])
        area_view = self._state_area()
        self.assertEqual(self.incoming["id"], area_view["assigned_asset_id"])
        self.assertEqual(self.incoming["id"], area_view["current_asset_id"])
        self.assertIsNotNone(area_view["switched_at"])

    def test_failed_relay_releases_incoming_and_keeps_reason(self):
        reserve = self.service.reserve_relay(
            "coord1", "coordinator", self.area["id"], self.incoming["id"], deadline()
        )
        failed = self.service.fail_relay("coord1", "coordinator", reserve["id"], "接替船主机故障")
        self.assertEqual("failed", failed["status"])
        self.assertEqual("接替船主机故障", failed["failure_reason"])
        # 预留退回原船：原船仍是当前责任人，接替船恢复可用
        self.assertEqual(self.outgoing["id"], failed["current_asset_id"])
        assets = {a["id"]: a["status"] for a in self.service.state()["assets"]}
        self.assertEqual("assigned", assets[self.outgoing["id"]])
        self.assertEqual("available", assets[self.incoming["id"]])
        area_view = self._state_area()
        self.assertEqual("接替船主机故障", area_view["failure_reason"])
        with self.assertRaises(DomainError):
            self.service.confirm_relay("coord1", "coordinator", reserve["id"])
        # 失败后允许再次预留
        again = self.service.reserve_relay(
            "coord1", "coordinator", self.area["id"], self.incoming["id"], deadline()
        )
        self.assertEqual("reserved", again["status"])

    def test_validation_blocks_capability_sea_state_range_and_eta(self):
        # 能力不匹配
        air = self.service.add_asset(
            "coord1", "coordinator", "救助直升机", "aircraft", ["air"], 31.0, 122.0, 180, 300, 5
        )
        with self.assertRaises(DomainError) as ctx:
            self.service.reserve_relay("coord1", "coordinator", self.area["id"], air["id"], deadline())
        self.assertEqual(409, ctx.exception.status)
        # 到场时间过早（航程不够时间赶到）
        with self.assertRaises(DomainError) as ctx2:
            self.service.reserve_relay(
                "coord1", "coordinator", self.area["id"], self.incoming["id"], deadline(0.0001)
            )
        self.assertEqual(409, ctx2.exception.status)
        # 海况超限
        weak = self.service.add_asset(
            "coord1", "coordinator", "小艇", "vessel", ["surface"], 31.0, 122.0, 20, 100, 1
        )
        with self.assertRaises(DomainError):
            self.service.reserve_relay("coord1", "coordinator", self.area["id"], weak["id"], deadline())
        # 航程超限
        far = self.service.add_asset(
            "coord1", "coordinator", "远船", "vessel", ["surface"], 35.0, 128.0, 20, 50, 9
        )
        with self.assertRaises(DomainError):
            self.service.reserve_relay("coord1", "coordinator", self.area["id"], far["id"], deadline())
        # 非协调员无权操作
        with self.assertRaises(DomainError) as ctx3:
            self.service.reserve_relay("field1", "field", self.area["id"], self.incoming["id"], deadline())
        self.assertEqual(403, ctx3.exception.status)

    def test_two_coordinators_cannot_grab_same_relay_ship(self):
        self.service.reserve_relay(
            "coord1", "coordinator", self.area["id"], self.incoming["id"], deadline()
        )
        area2 = self.service.create_search_area(
            "coord1", "coordinator", self.incident["id"], "A-102", "surface", 31.2, 122.1, 8, 2
        )
        other_out = self.service.add_asset(
            "coord2", "coordinator", "海巡03", "vessel", ["surface"], 31.2, 122.1, 20, 100, 5
        )
        self.service.assign_area("coord2", "coordinator", area2["id"], other_out["id"], other_out["version"])
        with self.assertRaises(DomainError) as ctx:
            self.service.reserve_relay("coord2", "coordinator", area2["id"], self.incoming["id"], deadline())
        self.assertEqual(409, ctx.exception.status)
        # 同一区域也不能重复预留
        third = self.service.add_asset(
            "coord1", "coordinator", "海巡04", "vessel", ["surface"], 31.0, 122.0, 20, 100, 5
        )
        with self.assertRaises(DomainError):
            self.service.reserve_relay("coord1", "coordinator", self.area["id"], third["id"], deadline())

    def test_outgoing_ship_cannot_withdraw_before_handover_confirmed(self):
        reserve = self.service.reserve_relay(
            "coord1", "coordinator", self.area["id"], self.incoming["id"], deadline()
        )
        outgoing = next(a for a in self.service.state()["assets"] if a["id"] == self.outgoing["id"])
        with self.assertRaises(DomainError) as ctx:
            self.service.withdraw_asset("coord1", "coordinator", self.outgoing["id"], "燃油不足", outgoing["version"])
        self.assertEqual(409, ctx.exception.status)
        incoming = next(a for a in self.service.state()["assets"] if a["id"] == self.incoming["id"])
        with self.assertRaises(DomainError):
            self.service.withdraw_asset("coord1", "coordinator", self.incoming["id"], "改派", incoming["version"])
        # 确认交接后原船已自动释放，无需也不能再撤回
        self.service.confirm_relay("coord1", "coordinator", reserve["id"])
        outgoing = next(a for a in self.service.state()["assets"] if a["id"] == self.outgoing["id"])
        self.assertEqual("available", outgoing["status"])
        with self.assertRaises(DomainError):
            self.service.withdraw_asset("coord1", "coordinator", self.outgoing["id"], "燃油不足", outgoing["version"])

    def test_restart_recovers_pending_reservation_back_to_outgoing(self):
        reserve = self.service.reserve_relay(
            "coord1", "coordinator", self.area["id"], self.incoming["id"], deadline()
        )
        # 模拟服务重启：重新实例化服务会回收未确认预留
        restarted = MaritimeSARService(self.db_path)
        handover = next(h for h in restarted.state()["area_handovers"] if h["id"] == reserve["id"])
        self.assertEqual("failed", handover["status"])
        self.assertIn("服务重启", handover["failure_reason"])
        assets = {a["id"]: a["status"] for a in restarted.state()["assets"]}
        self.assertEqual("assigned", assets[self.outgoing["id"]])
        self.assertEqual("available", assets[self.incoming["id"]])
        area_view = next(a for a in restarted.state()["search_areas"] if a["id"] == self.area["id"])
        self.assertEqual(self.outgoing["id"], area_view["current_asset_id"])

    def test_existing_assignment_without_handover_stays_compatible(self):
        # 从未发起交接的存量区域：relay 字段为空，责任人仍是原船
        area_view = self._state_area()
        self.assertIsNone(area_view["relay_status"])
        self.assertEqual(self.outgoing["id"], area_view["current_asset_id"])
        self.assertEqual("海巡01", area_view["current_asset_name"])

    def test_completing_area_cancels_pending_reservation(self):
        reserve = self.service.reserve_relay(
            "coord1", "coordinator", self.area["id"], self.incoming["id"], deadline()
        )
        area_view = self._state_area()
        self.service.complete_area("coord1", "coordinator", self.area["id"], "completed", area_view["version"])
        handover = next(h for h in self.service.state()["area_handovers"] if h["id"] == reserve["id"])
        self.assertEqual("failed", handover["status"])
        self.assertIn("自动取消", handover["failure_reason"])
        assets = {a["id"]: a["status"] for a in self.service.state()["assets"]}
        self.assertEqual("available", assets[self.incoming["id"]])
        self.assertEqual("available", assets[self.outgoing["id"]])


if __name__ == "__main__":
    unittest.main()
