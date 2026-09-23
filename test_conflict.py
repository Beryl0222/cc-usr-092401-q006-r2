"""磅单幂等可审计规则测试。

覆盖：
* 业务摘要稳定、重放与冲突分流；冲突不改树群已采重量/预占/既有检验；
* 复核保留原值 / 交货前更正磅单（带关联、统计平移、配额重核）；
* 交货后、结算后只能形成重量差异调整（每公斤只进一条结算链）；
* 并发补传与交货/结算交错、重启式重放、失败请求不占流水号；
* 果农冲突摘要与企业/护树队越权边界。
"""

import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from domain import (
    Conflict,
    HeritageCitrusService,
    PermissionDenied,
    ROLE_COOP,
    ROLE_FARMER,
    ROLE_GUARD,
    ROLE_REVIEWER,
    ROLE_ENTERPRISE,
    ValidationFailed,
)
from test_domain import World, weigh_inspect_settle


def delivered(world: World, bid: str) -> str:
    d = world.svc.deliver(world.coop, bid, "广兴饮料厂",
                          world.contract_id, "2026-09-02T08:00:00+00:00")
    return d["交货单编号"]


class DigestAndReplayTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.bid = self.w.svc.open_batch(
            self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")["批次编号"]

    def test_digest_is_stable_and_field_order_independent(self):
        d1 = HeritageCitrusService._ticket_digest("树群-1", __import__("decimal").Decimal("100"), "地磅-02")
        d2 = HeritageCitrusService._ticket_digest("树群-1", __import__("decimal").Decimal("100.0"), "地磅-02")
        self.assertEqual(d1, d2)  # 100 与 100.0 同净重同摘要
        self.assertEqual(len(d1), 64)
        d3 = HeritageCitrusService._ticket_digest("树群-1", __import__("decimal").Decimal("101"), "地磅-02")
        self.assertNotEqual(d1, d3)

    def test_identical_resend_is_replay_not_conflict(self):
        svc = self.w.svc
        first = svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "S-1",
                          gross_kg="210", tare_kg="10", offline=True, device="地磅-02")
        second = svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "S-1",
                           gross_kg="210", tare_kg="10", offline=False, device="地磅-02")
        self.assertTrue(second["幂等命中"])
        self.assertEqual(second["磅单编号"], first["磅单编号"])
        self.assertEqual(svc.list_conflicts(self.w.coop), [])
        # 毛/皮重表示不同但净重一致，仍是同一张磅单的重放
        third = svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "S-1",
                          weight_kg="200", device="地磅-02")
        self.assertTrue(third["幂等命中"])

    def test_failed_request_does_not_consume_slip_no(self):
        svc = self.w.svc
        # 净重非法：拒绝，不占流水号
        with self.assertRaises(ValidationFailed):
            svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "S-2",
                      gross_kg="10", tare_kg="20", device="地磅-02")
        # 同流水号随后可正常首登
        ok = svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "S-2",
                       weight_kg="30", device="地磅-02")
        self.assertNotIn("幂等命中", ok)
        self.assertEqual(svc.list_conflicts(self.w.coop), [])

    def test_protection_over_quota_failure_does_not_consume_slip(self):
        svc = self.w.svc
        svc.reserve_trees(self.w.coop, self.bid, self.w.heritage_id, "50")
        with self.assertRaises(Conflict) as cm:
            svc.weigh(self.w.coop, self.bid, self.w.heritage_id, "H-9", weight_kg="60")
        self.assertEqual(cm.exception.code, "over_reservation")
        # 额度内重试同号成功
        ok = svc.weigh(self.w.coop, self.bid, self.w.heritage_id, "H-9", weight_kg="50")
        self.assertEqual(ok["重量kg"], "50")


class ConflictRegistrationTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.bid = self.w.svc.open_batch(
            self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")["批次编号"]
        self.first = self.w.svc.weigh(
            self.w.coop, self.bid, self.w.old_tree_id, "C-1",
            weight_kg="100", offline=True, device="地磅-02")
        self.tid = self.first["磅单编号"]

    def _conflict_resend(self, **over):
        params = dict(actor=self.w.coop, batch_id=self.bid, tree_id=self.w.old_tree_id,
                      slip_no="C-1", weight_kg="110", offline=False, device="地磅-02")
        params.update(over)
        return self.w.svc.weigh(**params)

    def test_changed_weight_registers_pending_conflict_without_side_effects(self):
        svc = self.w.svc
        with self.assertRaises(Conflict) as cm:
            self._conflict_resend()
        self.assertEqual(cm.exception.code, "ticket_conflict")
        c = cm.exception.extra["冲突"]
        self.assertEqual(c["状态"], "待复核")
        self.assertEqual(c["原磅单编号"], self.tid)
        self.assertEqual(c["字段差异"], [{"字段": "净重kg", "原值": "100", "补传值": "110"}])

        # 原磅单原样保留：重量/树群/设备不变
        tickets = svc.get_batch_detail(self.w.coop, self.bid)["磅单"]
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0]["重量kg"], "100")
        self.assertEqual(tickets[0]["冲突编号"], c["冲突编号"])
        # 树群已采重量没有因补传而增加
        self.assertEqual(svc.get_tree_group(self.w.coop, self.w.old_tree_id)["本季已采重量kg"], "100")

    def test_changed_tree_or_device_also_conflicts(self):
        with self.assertRaises(Conflict) as cm:
            self._conflict_resend(tree_id=self.w.heritage_id, weight_kg="100")
        fields = {d["字段"] for d in cm.exception.extra["冲突"]["字段差异"]}
        self.assertEqual(fields, {"树群编号"})
        with self.assertRaises(Conflict) as cm:
            self._conflict_resend(weight_kg="100", device="地磅-99")
        self.assertEqual(cm.exception.extra["冲突"]["字段差异"][0]["字段"], "设备号")

    def test_conflict_never_touches_inspection_or_quota(self):
        svc = self.w.svc
        insp = svc.inspect(self.w.reviewer, self.tid, "A", "初检达标")
        # 补传把净重改到 130：既有检验不动、树群统计不动
        with self.assertRaises(Conflict):
            self._conflict_resend(weight_kg="130")
        kept = svc.get_batch_detail(self.w.coop, self.bid)["磅单"][0]
        self.assertEqual(kept["检验编号"], insp["检验编号"])
        self.assertEqual(kept["重量kg"], "100")
        self.assertEqual(svc.get_tree_group(self.w.coop, self.w.old_tree_id)["本季已采次数"], 1)

    def test_repeated_conflicting_resends_merge_into_one_record(self):
        svc = self.w.svc
        for w in ("110", "112", "115"):
            with self.assertRaises(Conflict):
                self._conflict_resend(weight_kg=w)
        pending = svc.list_conflicts(self.w.coop, "待复核")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["补传次数"], 3)
        self.assertEqual(pending[0]["补传净重kg"], "115")  # 归并最新值
        self.assertEqual(len(svc.get_batch_detail(self.w.coop, self.bid)["磅单"]), 1)

    def test_pending_conflict_blocks_delivery(self):
        svc = self.w.svc
        svc.inspect(self.w.reviewer, self.tid, "A", "初检达标")
        with self.assertRaises(Conflict):
            self._conflict_resend()
        with self.assertRaises(Conflict) as cm:
            delivered(self.w, self.bid)
        self.assertEqual(cm.exception.code, "conflict_pending")

    def test_replay_after_conflict_resolved_keep_reopens_new_conflict(self):
        svc = self.w.svc
        with self.assertRaises(Conflict) as cm:
            self._conflict_resend()
        cid = cm.exception.extra["冲突"]["冲突编号"]
        svc.resolve_ticket_conflict(self.w.coop, cid, "保留原值", "纸单核对无误")
        # 又有人拿不同数据补同号：作为新的一轮冲突重新挂起，原记录留痕
        with self.assertRaises(Conflict) as cm2:
            self._conflict_resend(weight_kg="120")
        self.assertNotEqual(cm2.exception.extra["冲突"]["冲突编号"], cid)
        self.assertEqual(len(svc.list_conflicts(self.w.coop)), 2)


class ConflictResolutionTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.bid = self.w.svc.open_batch(
            self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")["批次编号"]

    def _open_conflict(self, original_weight="100", resent_weight="90",
                       tree=None, device="地磅-02", resent_device="地磅-02"):
        tree = tree or self.w.old_tree_id
        first = self.w.svc.weigh(self.w.coop, self.bid, tree, "K-1",
                                 weight_kg=original_weight, device=device)
        with self.assertRaises(Conflict) as cm:
            self.w.svc.weigh(self.w.coop, self.bid, tree, "K-1",
                             weight_kg=resent_weight, device=resent_device)
        return first, cm.exception.extra["冲突"]["冲突编号"]

    def test_keep_original_closes_conflict_with_audit(self):
        svc = self.w.svc
        first, cid = self._open_conflict()
        result = svc.resolve_ticket_conflict(self.w.reviewer, cid, "保留原值", "原纸单为准")
        self.assertEqual(result["处置"], "保留原值")
        self.assertEqual(result["冲突"]["状态"], "保留原值")
        self.assertEqual(result["冲突"]["复核人"], "严复核")
        self.assertEqual(result["现行磅单"]["磅单编号"], first["磅单编号"])
        self.assertEqual(svc.get_batch_detail(self.w.coop, self.bid)["磅单"][0]["重量kg"], "100")
        self.assertEqual(svc.list_conflicts(self.w.coop, "待复核"), [])
        # 不能重复复核
        with self.assertRaises(Conflict) as cm:
            svc.resolve_ticket_conflict(self.w.coop, cid, "保留原值")
        self.assertEqual(cm.exception.code, "conflict_resolved")

    def test_pre_delivery_correction_creates_linked_ticket_and_moves_stats(self):
        svc = self.w.svc
        first, insp = weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "K-9", "100", "A")
        with self.assertRaises(Conflict) as cm:
            svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "K-9",
                      weight_kg="90", device="地磅-02")
        cid = cm.exception.extra["冲突"]["冲突编号"]
        result = svc.resolve_ticket_conflict(self.w.coop, cid, "生成更正磅单", "人工补录净重90")

        self.assertEqual(result["处置"], "更正磅单")
        new_t = result["更正磅单"]
        old_t = result["原磅单"]
        self.assertEqual(new_t["重量kg"], "90")
        self.assertEqual(new_t["状态"], "更正单")
        self.assertEqual(new_t["更正自"], first["磅单编号"])
        self.assertEqual(new_t["过磅流水号"], "K-9")
        self.assertEqual(old_t["状态"], "已更正")
        self.assertEqual(old_t["更正为"], new_t["磅单编号"])
        # 既有检验保留在原单上，更正单必须重新检验
        self.assertEqual(old_t["检验编号"], insp["检验编号"])
        self.assertIsNone(new_t["检验编号"])

        # 树群已采重量/次数按新旧差平移（不是累加）
        tree = svc.get_tree_group(self.w.coop, self.w.old_tree_id)
        self.assertEqual(tree["本季已采重量kg"], "90")
        self.assertEqual(tree["本季已采次数"], 1)

        # 批次详情中原单与更正单都在，可审计
        tickets = svc.get_batch_detail(self.w.coop, self.bid)["磅单"]
        self.assertEqual({t["状态"] for t in tickets}, {"已更正", "更正单"})

        # 流水号现在解析到更正单：90 重放命中更正单；100 再来变成针对更正单的新冲突
        replay = svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "K-9",
                           weight_kg="90", device="地磅-02")
        self.assertTrue(replay["幂等命中"])
        self.assertEqual(replay["磅单编号"], new_t["磅单编号"])
        with self.assertRaises(Conflict):
            svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "K-9",
                      weight_kg="100", device="地磅-02")

    def test_correction_moving_tree_transfers_stats(self):
        svc = self.w.svc
        # 在第二块普通老树群建档，便于跨树群更正
        other_tree = svc.register_tree_group(
            self.w.coop, self.w.plot_id, "坡顶老树", "红橘", 55, "普通老树")["编号"]
        first = svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "K-2",
                          weight_kg="40", device="地磅-02")
        with self.assertRaises(Conflict) as cm:
            svc.weigh(self.w.coop, self.bid, other_tree, "K-2",
                      weight_kg="40", device="地磅-02")
        cid = cm.exception.extra["冲突"]["冲突编号"]
        svc.resolve_ticket_conflict(self.w.coop, cid, "生成更正磅单")
        self.assertEqual(svc.get_tree_group(self.w.coop, self.w.old_tree_id)["本季已采重量kg"], "0")
        self.assertEqual(svc.get_tree_group(self.w.coop, other_tree)["本季已采重量kg"], "40")

    def test_correction_into_over_reservation_is_rejected_and_conflict_stays_open(self):
        svc = self.w.svc
        svc.reserve_trees(self.w.coop, self.bid, self.w.heritage_id, "100")
        first = svc.weigh(self.w.coop, self.bid, self.w.heritage_id, "K-3",
                          weight_kg="90", device="地磅-02")
        with self.assertRaises(Conflict) as cm:
            svc.weigh(self.w.coop, self.bid, self.w.heritage_id, "K-3",
                      weight_kg="110", device="地磅-02")
        cid = cm.exception.extra["冲突"]["冲突编号"]
        # 110 > 预占 100：更正被拒，冲突保持待复核，统计与原单不动
        with self.assertRaises(Conflict) as rj:
            svc.resolve_ticket_conflict(self.w.coop, cid, "生成更正磅单")
        self.assertEqual(rj.exception.code, "over_reservation")
        self.assertEqual(svc.get_conflict(self.w.coop, cid)["状态"], "待复核")
        self.assertIsNone(svc.get_conflict(self.w.coop, cid)["复核时间"])
        self.assertEqual(svc.get_tree_group(self.w.coop, self.w.heritage_id)["本季已采重量kg"], "90")
        # 仍可改判保留原值
        svc.resolve_ticket_conflict(self.w.coop, cid, "保留原值")
        self.assertEqual(svc.get_tree_group(self.w.coop, self.w.heritage_id)["本季已采重量kg"], "90")

    def test_only_authorized_roles_resolve(self):
        svc = self.w.svc
        _first, cid = self._open_conflict()
        for actor in (self.w.farmer, self.w.guard, self.w.ent):
            with self.assertRaises(PermissionDenied):
                svc.resolve_ticket_conflict(actor, cid, "保留原值")
        # 合作社与质量复核人可以
        svc.resolve_ticket_conflict(self.w.coop, cid, "保留原值")


class PostDeliveryAdjustmentTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.bid = self.w.svc.open_batch(
            self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")["批次编号"]

    def _delivered_ticket(self, weight="100", grade="A", slip="D-1"):
        ticket, insp = weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id,
                                            slip, weight, grade)
        did = delivered(self.w, self.bid)
        return ticket, insp, did

    def _conflict(self, slip, weight, new_weight):
        with self.assertRaises(Conflict) as cm:
            self.w.svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, slip,
                             weight_kg=new_weight, device="地磅-02")
        return cm.exception.extra["冲突"]["冲突编号"]

    def test_after_delivery_correction_becomes_pending_weight_adjustment(self):
        svc = self.w.svc
        ticket, _insp, did = self._delivered_ticket("100", "A")
        cid = self._conflict("D-1", "100", "110")
        result = svc.resolve_ticket_conflict(self.w.coop, cid, "生成更正磅单")
        self.assertEqual(result["处置"], "差异调整")
        adj = result["差异调整"]
        self.assertEqual(adj["差异kg"], "10")
        self.assertEqual(adj["状态"], "待入账")
        self.assertIsNone(adj["结算编号"])
        # 原磅单不被替换
        original = svc.get_batch_detail(self.w.coop, self.bid)["磅单"][0]
        self.assertEqual(original["磅单编号"], ticket["磅单编号"])
        self.assertEqual(original["重量kg"], "100")
        self.assertEqual(original["状态"], "正常")

        # 结算时差异按交货锁定价规并入：100*4 + 10*4 = 440，只有一张磅单进结算链
        settlement = svc.settle(self.w.coop, did)
        rows = [r for r in settlement["明细行"] if "磅单编号" in r]
        adj_rows = [r for r in settlement["明细行"] if "重量调整编号" in r]
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(adj_rows), 1)
        self.assertEqual(adj_rows[0]["差异kg"], "10")
        self.assertEqual(adj_rows[0]["金额"], "40.00")
        self.assertEqual(settlement["明细行"][-1]["合计应收"], "440.00")
        self.assertEqual(settlement["状态"], "含调整")
        stored = svc.list_ledger(self.w.coop, "农户结算")["记录"]
        self.assertEqual(len(stored), 1)

    def test_after_settlement_correction_is_priced_claim_without_replacing_ticket(self):
        svc = self.w.svc
        ticket, insp, did = self._delivered_ticket("100", "B")
        settlement = svc.settle(self.w.coop, did)
        self.assertEqual(settlement["明细行"][-1]["合计应收"], "300.00")

        cid = self._conflict("D-1", "100", "90")  # 少 10kg
        result = svc.resolve_ticket_conflict(self.w.coop, cid, "生成更正磅单")
        self.assertEqual(result["处置"], "差异调整(已结算)")
        adj = result["差异调整"]
        self.assertEqual(adj["差异kg"], "-10")
        self.assertEqual(adj["方向"], "扣回")
        self.assertEqual(adj["状态"], "已入账")
        self.assertEqual(adj["结算单价"], "3.00")
        self.assertEqual(adj["金额"], "-30.00")
        self.assertEqual(adj["结算编号"], settlement["结算编号"])

        # 原结算行与合计分文不改，调整独立挂账
        again = svc.get_settlement(self.w.coop, settlement["结算编号"])
        self.assertEqual(again["明细行"][-1]["合计应收"], "300.00")
        self.assertEqual(len(again["重量调整"]), 1)
        self.assertEqual(again["重量调整"][0]["金额"], "-30.00")
        self.assertEqual(again["状态"], "含调整")
        # 原磅单依旧是结算引用的那一张
        self.assertEqual(svc.get_batch_detail(self.w.coop, self.bid)["磅单"][0]["结算编号"],
                         settlement["结算编号"])

    def test_upward_correction_after_settlement_is_supplement(self):
        svc = self.w.svc
        _t, _i, did = self._delivered_ticket("100", "A")
        sid = svc.settle(self.w.coop, did)["结算编号"]
        cid = self._conflict("D-1", "100", "108")
        result = svc.resolve_ticket_conflict(self.w.reviewer, cid, "生成更正磅单")
        adj = result["差异调整"]
        self.assertEqual(adj["方向"], "补付")
        self.assertEqual(adj["金额"], "32.00")  # 8kg * 4
        self.assertEqual(adj["结算编号"], sid)


class ConcurrentInterleaveTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.bid = self.w.svc.open_batch(
            self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")["批次编号"]

    def test_concurrent_conflicting_resends_one_ticket_one_conflict(self):
        svc = self.w.svc
        svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "X-1",
                  weight_kg="100", device="地磅-02")
        outcomes = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def resend():
            barrier.wait()
            try:
                svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "X-1",
                          weight_kg="105", device="地磅-02")
                with lock:
                    outcomes.append("ok")
            except Conflict as exc:
                with lock:
                    outcomes.append(exc.code)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: resend(), range(8)))
        self.assertEqual(set(outcomes), {"ticket_conflict"})
        self.assertEqual(len(svc.get_batch_detail(self.w.coop, self.bid)["磅单"]), 1)
        pending = svc.list_conflicts(self.w.coop, "待复核")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["补传次数"], 8)
        self.assertEqual(svc.get_tree_group(self.w.coop, self.w.old_tree_id)["本季已采重量kg"], "100")

    def test_identical_replays_concurrent_with_delivery_single_chain(self):
        svc = self.w.svc
        svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "X-2",
                  weight_kg="100", device="地磅-02")
        svc.inspect(self.w.reviewer,
                    svc.get_batch_detail(self.w.coop, self.bid)["磅单"][0]["磅单编号"],
                    "A", "达标")
        barrier = threading.Barrier(6)

        def replay():
            barrier.wait()
            v = svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "X-2",
                          weight_kg="100", device="地磅-02")
            return v.get("幂等命中", False)

        def deliver():
            barrier.wait()
            return svc.deliver(self.w.coop, self.bid, "广兴饮料厂",
                               self.w.contract_id, "2026-09-02T08:00:00+00:00")

        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = [pool.submit(replay) for _ in range(4)]
            dfut = pool.submit(deliver)
            futs.append(dfut)
            barrier.wait()  # 主线程占第 6 个槽，统一放行
            results = [f.result() for f in futs]
        self.assertTrue(all(r is True for r in results[:-1]))
        did = results[-1]["交货单编号"]
        settlement = svc.settle(self.w.coop, did)
        rows = [r for r in settlement["明细行"] if "磅单编号" in r]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["重量kg"], "100")
        self.assertEqual(settlement["明细行"][-1]["合计应收"], "400.00")

    def test_conflicting_resends_interleaved_with_delivery_never_double_count(self):
        """冲突补传与交货并发：无论谁先，磅单都只进一条结算链。"""
        svc = self.w.svc
        ticket = svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "X-3",
                           weight_kg="100", device="地磅-02")
        svc.inspect(self.w.reviewer, ticket["磅单编号"], "A", "达标")
        barrier = threading.Barrier(6)

        def resend():
            barrier.wait()
            try:
                svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "X-3",
                          weight_kg="110", device="地磅-02")
                return "ok"
            except Conflict as exc:
                return exc.code

        def deliver():
            barrier.wait()
            try:
                return ("delivered", svc.deliver(
                    self.w.coop, self.bid, "广兴饮料厂",
                    self.w.contract_id, "2026-09-02T08:00:00+00:00"))
            except Conflict as exc:
                return ("blocked", exc.code)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = [pool.submit(resend) for _ in range(4)]
            dfut = pool.submit(deliver)
            barrier.wait()
            resend_codes = [f.result() for f in futs]
            d_outcome = dfut.result()
        self.assertEqual(set(resend_codes), {"ticket_conflict"})
        self.assertEqual(len(svc.get_batch_detail(self.w.coop, self.bid)["磅单"]), 1)
        cid = svc.list_conflicts(self.w.coop, "待复核")[0]["冲突编号"]

        if d_outcome[0] == "blocked":
            # 冲突先登记：交货被挡；交货前更正成 110 后重走检验→交货→结算
            self.assertEqual(d_outcome[1], "conflict_pending")
            svc.resolve_ticket_conflict(self.w.coop, cid, "生成更正磅单")
            new_id = svc.get_conflict(self.w.coop, cid)["更正磅单编号"]
            svc.inspect(self.w.reviewer, new_id, "A", "更正单复检")
            did = delivered(self.w, self.bid)
            settlement = svc.settle(self.w.coop, did)
            self.assertEqual(settlement["明细行"][-1]["合计应收"], "440.00")
        else:
            # 交货先成：冲突只能形成 +10kg 差异调整，结算总额 440 且只有一张磅单行
            did = d_outcome[1]["交货单编号"]
            svc.resolve_ticket_conflict(self.w.coop, cid, "生成更正磅单")
            settlement = svc.settle(self.w.coop, did)
            rows = [r for r in settlement["明细行"] if "磅单编号" in r]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["磅单编号"], ticket["磅单编号"])
            self.assertEqual(settlement["明细行"][-1]["合计应收"], "440.00")

        # 任一交错结果下，结算账只有一张结算单
        self.assertEqual(len(svc.list_ledger(self.w.coop, "农户结算")["记录"]), 1)

    def test_conflicting_resends_interleaved_with_settlement(self):
        svc = self.w.svc
        ticket, _insp = weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "X-4", "100", "A")
        did = delivered(self.w, self.bid)
        barrier = threading.Barrier(5)

        def resend():
            barrier.wait()
            try:
                svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "X-4",
                          weight_kg="110", device="地磅-02")
            except Conflict:
                return "conflict"
            return "ok"

        def settle():
            barrier.wait()
            try:
                return "settled", svc.settle(self.w.coop, did)
            except Conflict as exc:
                return "blocked", exc.code

        with ThreadPoolExecutor(max_workers=5) as pool:
            rfuts = [pool.submit(resend) for _ in range(3)]
            sfut = pool.submit(settle)
            barrier.wait()
            rcodes = [f.result() for f in rfuts]
            s_outcome = sfut.result()
        self.assertEqual(set(rcodes), {"conflict"})
        cid = svc.list_conflicts(self.w.coop, "待复核")[0]["冲突编号"]
        if s_outcome[0] == "settled":
            sid = s_outcome[1]["结算编号"]
        else:
            self.assertEqual(s_outcome[1], "already_settled")
            sid = svc.list_ledger(self.w.coop, "农户结算")["记录"][0]["结算编号"]
        # 结算后更正：+40 元挂同一张结算单，原磅单链不变
        result = svc.resolve_ticket_conflict(self.w.coop, cid, "生成更正磅单")
        self.assertEqual(result["差异调整"]["结算编号"], sid)
        self.assertEqual(result["差异调整"]["金额"], "40.00")
        settled = svc.get_settlement(self.w.coop, sid)
        self.assertEqual(settled["明细行"][-1]["合计应收"], "400.00")
        self.assertEqual(settled["重量调整"][0]["差异kg"], "10")
        # 再次结算仍被拒：每公斤只结一次
        with self.assertRaises(Conflict) as cm:
            svc.settle(self.w.coop, did)
        self.assertEqual(cm.exception.code, "already_settled")


class RestartReplayTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.bid = self.w.svc.open_batch(
            self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")["批次编号"]

    def _roundtrip(self):
        blob = json.dumps(self.w.svc.dump_state(), ensure_ascii=False)
        return HeritageCitrusService.restore_state(json.loads(blob))

    def test_replay_after_restart_hits_original(self):
        svc = self.w.svc
        first = svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "R-1",
                          weight_kg="100", device="地磅-02", offline=True)
        restored = self._roundtrip()
        coop = restored.authenticate("coop")
        replay = restored.weigh(coop, self.bid, self.w.old_tree_id, "R-1",
                                weight_kg="100", device="地磅-02", offline=False)
        self.assertTrue(replay["幂等命中"])
        self.assertEqual(replay["磅单编号"], first["磅单编号"])
        self.assertEqual(len(restored.get_batch_detail(coop, self.bid)["磅单"]), 1)

    def test_pending_conflict_survives_restart(self):
        svc = self.w.svc
        svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "R-2",
                  weight_kg="100", device="地磅-02")
        with self.assertRaises(Conflict):
            svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "R-2",
                      weight_kg="95", device="地磅-02")
        restored = self._roundtrip()
        coop = restored.authenticate("coop")
        pending = restored.list_conflicts(coop, "待复核")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["补传净重kg"], "95")
        # 重启后冲突仍可处置：交货前更正
        out = restored.resolve_ticket_conflict(coop, pending[0]["冲突编号"], "生成更正磅单")
        self.assertEqual(out["更正磅单"]["重量kg"], "95")
        self.assertEqual(restored.get_tree_group(coop, self.w.old_tree_id)["本季已采重量kg"], "95")

    def test_settled_correction_state_survives_restart(self):
        svc = self.w.svc
        weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "R-3", "100", "A")
        did = delivered(self.w, self.bid)
        sid = svc.settle(self.w.coop, did)["结算编号"]
        with self.assertRaises(Conflict) as cm:
            svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "R-3",
                      weight_kg="110", device="地磅-02")
        cid = cm.exception.extra["冲突"]["冲突编号"]
        svc.resolve_ticket_conflict(self.w.coop, cid, "生成更正磅单")

        restored = self._roundtrip()
        coop = restored.authenticate("coop")
        settled = restored.get_settlement(coop, sid)
        self.assertEqual(settled["明细行"][-1]["合计应收"], "400.00")
        self.assertEqual(settled["重量调整"][0]["金额"], "40.00")
        # 原磅单仍不可替换，再来冲突只能形成新的差异调整
        with self.assertRaises(Conflict) as cm2:
            restored.weigh(coop, self.bid, self.w.old_tree_id, "R-3",
                           weight_kg="112", device="地磅-02")
        cid2 = cm2.exception.extra["冲突"]["冲突编号"]
        out = restored.resolve_ticket_conflict(coop, cid2, "生成更正磅单")
        self.assertEqual(out["差异调整"]["差异kg"], "12")


class ConflictVisibilityTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.bid = self.w.svc.open_batch(
            self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")["批次编号"]
        self.w.svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "V-1",
                         weight_kg="100", device="地磅-02")
        with self.assertRaises(Conflict) as cm:
            self.w.svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "V-1",
                             weight_kg="90", device="地磅-02")
        self.cid = cm.exception.extra["冲突"]["冲突编号"]

    def test_farmer_sees_only_own_masked_summary(self):
        svc = self.w.svc
        own = svc.list_conflicts(self.w.farmer)
        self.assertEqual(len(own), 1)
        view = own[0]
        self.assertEqual(view["冲突编号"], self.cid)
        self.assertEqual(view["字段差异"][0]["原值"], "100")  # 业务差异可见
        self.assertNotIn("调整编号", view)                   # 付款字段裁剪
        # 第二户果农不可见
        self.assertEqual(svc.list_conflicts(self.w.farmer2), [])
        with self.assertRaises(PermissionDenied):
            svc.get_conflict(self.w.farmer2, self.cid)

    def test_enterprise_and_guard_cannot_see_identity_or_payment_via_conflicts(self):
        svc = self.w.svc
        for actor in (self.w.ent, self.w.guard):
            with self.assertRaises(PermissionDenied):
                svc.list_conflicts(actor)
            with self.assertRaises(PermissionDenied):
                svc.get_conflict(actor, self.cid)

    def test_coop_and_reviewer_see_full_conflict(self):
        svc = self.w.svc
        for actor in (self.w.coop, self.w.reviewer):
            view = svc.get_conflict(actor, self.cid)
            self.assertIn("调整编号", view)
            self.assertEqual(view["补传人"], "秦会计")

    def test_unknown_conflict_is_not_found(self):
        from domain import NotFound
        with self.assertRaises(NotFound):
            self.w.svc.get_conflict(self.w.coop, "冲突-999")


if __name__ == "__main__":
    unittest.main(verbosity=2)
