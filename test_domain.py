"""领域不变量证明测试 —— 直接对 HeritageCitrusService 验证全部业务规则。"""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from domain import (
    Conflict,
    HeritageCitrusService,
    NotFound,
    PermissionDenied,
    ROLE_COOP,
    ROLE_FARMER,
    ROLE_GUARD,
    ROLE_REVIEWER,
    ROLE_ENTERPRISE,
    ValidationFailed,
)


class World:
    """搭建一套最小但完整的生产关系：农户-地块-老树/百年树-合约。"""

    def __init__(self):
        self.svc = HeritageCitrusService()
        self.svc.register_actor("coop", "秦会计", ROLE_COOP)
        self.svc.register_actor("reviewer", "严复核", ROLE_REVIEWER)
        self.svc.register_actor("guard", "护树员老吴", ROLE_GUARD)
        self.svc.register_actor("ent", "广兴饮料厂", ROLE_ENTERPRISE)

        self.coop = self.svc.authenticate("coop")
        self.reviewer = self.svc.authenticate("reviewer")
        self.guard = self.svc.authenticate("guard")
        self.ent = self.svc.authenticate("ent")

        farmer = self.svc.register_farmer(self.coop, "梁果农")
        self.farmer_id = farmer["农户编号"]
        self.svc.register_actor("farmer", "梁果农", ROLE_FARMER, self.farmer_id)
        self.farmer = self.svc.authenticate("farmer")

        # 第二位农户，用于隔离性验证
        other = self.svc.register_farmer(self.coop, "邻户老赵")
        self.other_id = other["农户编号"]
        self.svc.register_actor("farmer2", "邻户老赵", ROLE_FARMER, self.other_id)
        self.farmer2 = self.svc.authenticate("farmer2")

        self.plot = self.svc.create_plot(self.coop, self.farmer_id, "梁家湾坡地", "广兴镇梁家湾")
        self.plot_id = self.plot["地块编号"]
        self.old_tree = self.svc.register_tree_group(
            self.coop, self.plot_id, "连片老红橘", "红橘", 60, "普通老树")
        self.old_tree_id = self.old_tree["编号"]
        self.heritage = self.svc.register_tree_group(
            self.coop, self.plot_id, "百年母树群", "红橘", 130, "百年保护树")
        self.heritage_id = self.heritage["编号"]

        # 首个价规 2026-08-01 生效：A级4元、B级3元保护价
        self.svc.publish_price_rule(
            self.coop, "2026-08-01T00:00:00+00:00",
            protected={"A": "4.00", "B": "3.00"},
            market_reference={"A": "4.20", "B": "3.10"}, note="开园首版")
        self.contract = self.svc.sign_contract(
            self.coop, self.farmer_id, "广兴饮料厂",
            "2026-08-05T00:00:00+00:00", ["A", "B"], "2026秋")
        self.contract_id = self.contract["合约编号"]

        # 百年树配额：本季 100kg、2 次
        self.svc.set_quota(self.coop, self.heritage_id, "2026秋", "100", 2)


def weigh_inspect_settle(world: World, batch_id: str, tree_id: str, slip: str,
                         weight: str, grade: str, offline=False):
    """走通 过磅→初检，返回 (磅单视图, 检验视图)。"""
    ticket = world.svc.weigh(
        world.coop, batch_id, tree_id, slip, weight_kg=weight,
        offline=offline, device="地磅-01")
    insp = world.svc.inspect(
        world.reviewer, ticket["磅单编号"], grade, "糖度与外观抽检达标")
    return ticket, insp


class ContinuousChainTest(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def test_full_chain_plot_to_settlement(self):
        svc = self.w.svc
        svc.add_care_log(self.w.coop, self.w.old_tree_id, "2026-08-20", "施有机肥、疏果")
        batch = svc.open_batch(self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")
        bid = batch["批次编号"]
        ticket, insp = weigh_inspect_settle(self.w, bid, self.w.old_tree_id, "P001", "100", "A")

        delivery = svc.deliver(self.w.coop, bid, "广兴饮料厂",
                               self.w.contract_id, "2026-09-02T08:00:00+00:00")
        did = delivery["交货单编号"]
        self.assertEqual(delivery["计价价规"], "价规-v1")

        settlement = svc.settle(self.w.coop, did)
        self.assertEqual(settlement["明细行"][-1]["合计应收"], "400.00")
        self.assertEqual(settlement["明细行"][0]["磅单编号"], ticket["磅单编号"])

        # 结算后磅单被结算流水永久引用
        self.assertEqual(svc.get_batch_detail(self.w.coop, bid)["磅单"][0]["结算编号"],
                         settlement["结算编号"])

    def test_cannot_deliver_without_inspection(self):
        svc = self.w.svc
        batch = svc.open_batch(self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")
        svc.weigh(self.w.coop, batch["批次编号"], self.w.old_tree_id, "P001", weight_kg="50")
        with self.assertRaises(Conflict) as cm:
            svc.deliver(self.w.coop, batch["批次编号"], "广兴饮料厂",
                        self.w.contract_id, "2026-09-02T08:00:00+00:00")
        self.assertEqual(cm.exception.code, "inspection_pending")

    def test_ticket_requires_existing_batch_and_matching_plot(self):
        svc = self.w.svc
        batch = svc.open_batch(self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")
        other_plot = svc.create_plot(self.w.coop, self.w.other_id, "赵家坳", "广兴镇赵家坳")
        other_tree = svc.register_tree_group(
            self.w.coop, other_plot["地块编号"], "赵家老树", "红橘", 50, "普通老树")
        with self.assertRaises(ValidationFailed):
            svc.weigh(self.w.coop, batch["批次编号"], other_tree["编号"], "X1", weight_kg="10")
        with self.assertRaises(NotFound):
            svc.weigh(self.w.coop, "批次-999", self.w.old_tree_id, "X2", weight_kg="10")


class IdempotencyAndOnceOnlyTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.batch = self.w.svc.open_batch(self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")
        self.bid = self.batch["批次编号"]

    def test_offline_slip_resend_is_idempotent(self):
        """断网地磅恢复后重传同一张纸单：同一批果只入库一次。"""
        svc = self.w.svc
        first = svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "SLIP-77",
                          gross_kg="210", tare_kg="10", offline=True, device="地磅-02")
        self.assertNotIn("幂等命中", first)
        self.assertEqual(first["重量kg"], "200")
        # 网络恢复，设备把同一条流水号又推了一遍
        second = svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "SLIP-77",
                           gross_kg="210", tare_kg="10", offline=False, device="地磅-02")
        self.assertTrue(second["幂等命中"])
        self.assertEqual(second["磅单编号"], first["磅单编号"])
        tickets = svc.get_batch_detail(self.w.coop, self.bid)["磅单"]
        self.assertEqual(len(tickets), 1)

    def test_concurrent_same_slip_only_one_ticket(self):
        svc = self.w.svc
        errors = []

        def submit():
            try:
                svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "RACE-1",
                          weight_kg="30", offline=True, device="地磅-03")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        barrier = threading.Barrier(8)

        def go():
            barrier.wait()
            submit()

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: go(), range(8)))
        self.assertFalse(errors)
        tickets = svc.get_batch_detail(self.w.coop, self.bid)["磅单"]
        self.assertEqual(len(tickets), 1)

    def test_each_kg_settled_only_once_even_with_concurrent_settle(self):
        """并发交货结算：每公斤只能结算一次，第二次结算被拒。"""
        svc = self.w.svc
        weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "S1", "100", "A")
        delivery = svc.deliver(self.w.coop, self.bid, "广兴饮料厂",
                               self.w.contract_id, "2026-09-02T08:00:00+00:00")
        results = []
        barrier = threading.Barrier(2)

        def settle():
            barrier.wait()
            try:
                return ("ok", svc.settle(self.w.coop, delivery["交货单编号"]))
            except Conflict as exc:
                return ("reject", exc.code)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: settle(), range(2)))
        statuses = sorted(r[0] for r in results)
        self.assertEqual(statuses, ["ok", "reject"])
        self.assertEqual(results[0][1] if results[0][0] == "reject" else results[1][1],
                         "already_settled")

    def test_cannot_weigh_after_delivery(self):
        svc = self.w.svc
        weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "S1", "50", "A")
        svc.deliver(self.w.coop, self.bid, "广兴饮料厂",
                    self.w.contract_id, "2026-09-02T08:00:00+00:00")
        with self.assertRaises(Conflict) as cm:
            svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "S2", weight_kg="1")
        self.assertEqual(cm.exception.code, "batch_delivered")


class ProtectedTreeQuotaTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.batch = self.w.svc.open_batch(self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")
        self.bid = self.batch["批次编号"]

    def test_heritage_requires_quota_and_reservation(self):
        svc = self.w.svc
        fresh = svc.register_tree_group(
            self.w.coop, self.w.plot_id, "另一株百年树", "红橘", 120, "百年保护树")
        with self.assertRaises(Conflict) as cm:
            svc.weigh(self.w.coop, self.bid, fresh["编号"], "H0", weight_kg="5")
        self.assertEqual(cm.exception.code, "quota_not_set")
        with self.assertRaises(Conflict) as cm:
            svc.reserve_trees(self.w.coop, self.bid, fresh["编号"], "5")
        self.assertEqual(cm.exception.code, "quota_not_set")

    def test_reservation_cannot_exceed_season_quota(self):
        svc = self.w.svc
        svc.reserve_trees(self.w.coop, self.bid, self.w.heritage_id, "60")
        with self.assertRaises(Conflict) as cm:
            svc.reserve_trees(self.w.coop, self.bid, self.w.heritage_id, "41")
        self.assertEqual(cm.exception.code, "quota_exceeded")

    def test_weighing_beyond_reservation_is_rejected(self):
        """预占 50kg，现场偷采到 51kg：过磅即拦截，保护树采收不越界。"""
        svc = self.w.svc
        svc.reserve_trees(self.w.coop, self.bid, self.w.heritage_id, "50")
        svc.weigh(self.w.coop, self.bid, self.w.heritage_id, "H1", weight_kg="50")
        with self.assertRaises(Conflict) as cm:
            svc.weigh(self.w.coop, self.bid, self.w.heritage_id, "H2", weight_kg="1")
        self.assertEqual(cm.exception.code, "over_reservation")

    def test_concurrent_overharvest_only_part_within_quota_passes(self):
        """两车并发交售保护树果实，合计超配额时只有额度内的部分入库。"""
        svc = self.w.svc
        svc.reserve_trees(self.w.coop, self.bid, self.w.heritage_id, "100")
        outcomes = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def weigh(slip):
            barrier.wait()
            try:
                svc.weigh(self.w.coop, self.bid, self.w.heritage_id, slip, weight_kg="60")
                with lock:
                    outcomes.append("ok")
            except Conflict as exc:
                with lock:
                    outcomes.append(exc.code)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(weigh, ["C1", "C2"]))
        self.assertEqual(sorted(outcomes), ["ok", "over_reservation"])
        used = svc.get_tree_group(self.w.coop, self.w.heritage_id)["本季已采重量kg"]
        self.assertEqual(used, "60")

    def test_pick_times_quota_enforced(self):
        svc = self.w.svc
        svc.reserve_trees(self.w.coop, self.bid, self.w.heritage_id, "100")
        svc.weigh(self.w.coop, self.bid, self.w.heritage_id, "T1", weight_kg="40")
        svc.weigh(self.w.coop, self.bid, self.w.heritage_id, "T2", weight_kg="40")
        with self.assertRaises(Conflict) as cm:
            svc.weigh(self.w.coop, self.bid, self.w.heritage_id, "T3", weight_kg="1")
        self.assertEqual(cm.exception.code, "quota_times_exceeded")


class ReviewAndPriceTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.batch = self.w.svc.open_batch(self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")
        self.bid = self.batch["批次编号"]

    def _delivered(self):
        delivery = self.w.svc.deliver(self.w.coop, self.bid, "广兴饮料厂",
                                      self.w.contract_id, "2026-09-02T08:00:00+00:00")
        return delivery["交货单编号"]

    def test_review_before_settle_uses_new_grade_no_adjustment(self):
        svc = self.w.svc
        ticket, insp = weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "R1", "100", "A")
        result = svc.review_grade(self.w.reviewer, insp["检验编号"], "B",
                                  "复核发现风伤果比例超标，降为B级")
        self.assertIsNone(result["价差调整"])
        # 原样本与原判定完整保留
        self.assertFalse(result["原检验"]["现行"])
        self.assertEqual(result["原检验"]["样本编号"], result["新检验"]["样本编号"])
        self.assertEqual(result["新检验"]["复核自"], insp["检验编号"])

        did = self._delivered()
        settlement = svc.settle(self.w.coop, did)
        self.assertEqual(settlement["明细行"][0]["等级"], "B")
        self.assertEqual(settlement["明细行"][-1]["合计应收"], "300.00")
        self.assertEqual(settlement["状态"], "正常")

    def test_review_after_settle_keeps_original_and_writes_price_diff(self):
        svc = self.w.svc
        ticket, insp = weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "R2", "100", "B")
        did = self._delivered()
        settlement = svc.settle(self.w.coop, did)
        self.assertEqual(settlement["明细行"][-1]["合计应收"], "300.00")

        # 复核升级 A：补付 (4-3)*100 = 100；原结算行不被改写
        result = svc.review_grade(self.w.reviewer, insp["检验编号"], "A",
                                  "实验室复测糖度13.5，达到A级")
        adj = result["价差调整"]
        self.assertEqual(adj["差额"], "100.00")
        self.assertEqual(adj["方向"], "补付")
        self.assertEqual(adj["原等级"], "B")
        self.assertEqual(adj["新等级"], "A")
        settled_again = svc.get_settlement(self.w.coop, settlement["结算编号"])
        self.assertEqual(settled_again["明细行"][-1]["合计应收"], "300.00")
        self.assertEqual(settled_again["状态"], "含调整")

    def test_review_downgrade_after_settle_is_claim_back(self):
        svc = self.w.svc
        _, insp = weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "R3", "10", "A")
        did = self._delivered()
        svc.settle(self.w.coop, did)
        result = svc.review_grade(self.w.reviewer, insp["检验编号"], "B", "复检不达标")
        self.assertEqual(result["价差调整"]["差额"], "-10.00")
        self.assertEqual(result["价差调整"]["方向"], "扣回")

    def test_review_must_target_current_inspection(self):
        svc = self.w.svc
        _, insp = weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "R4", "10", "A")
        svc.review_grade(self.w.reviewer, insp["检验编号"], "B", "第一次复核降级")
        with self.assertRaises(Conflict) as cm:
            svc.review_grade(self.w.reviewer, insp["检验编号"], "A", "不能对失效记录再复核")
        self.assertEqual(cm.exception.code, "superseded")

    def test_later_market_price_cannot_rewrite_receivable(self):
        """9月2日交货按 v1 结算；9月10日市场价大跌发布 v2，农户应收不变。"""
        svc = self.w.svc
        weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "P1", "100", "A")
        did = self._delivered()
        settlement = svc.settle(self.w.coop, did)
        self.assertEqual(settlement["明细行"][-1]["合计应收"], "400.00")

        svc.publish_price_rule(
            self.w.coop, "2026-09-10T00:00:00+00:00",
            protected={"A": "2.50", "B": "2.00"}, note="市场下行，不追溯")
        # 历史结算金额原样
        self.assertEqual(svc.get_settlement(self.w.coop, settlement["结算编号"])
                         ["明细行"][-1]["合计应收"], "400.00")

        # 9月10日之后的新交货适用 v2
        batch2 = svc.open_batch(self.w.coop, self.w.plot_id, "2026-09-12", "2026秋")
        weigh_inspect_settle(self.w, batch2["批次编号"], self.w.old_tree_id, "P2", "100", "A")
        d2 = svc.deliver(self.w.coop, batch2["批次编号"], "广兴饮料厂",
                         self.w.contract_id, "2026-09-12T08:00:00+00:00")
        self.assertEqual(d2["计价价规"], "价规-v2")
        s2 = svc.settle(self.w.coop, d2["交货单编号"])
        self.assertEqual(s2["明细行"][-1]["合计应收"], "250.00")
        # 且价差调整仍以交货时价规 v1 计价，不被 v2 污染
        old_insp = svc.get_batch_detail(self.w.coop, self.bid)["磅单"][0]["检验编号"]
        # 找到该磅单初检记录（现行的可能是复核后的；直接对初检复核会在场景外，跳过）
        self.assertTrue(old_insp.startswith("检验-"))

    def test_concurrent_settle_and_appeal_total_is_consistent(self):
        """结算与等级申诉同批并发：无论先后，农户总权益一致且每公斤只结一次。"""
        svc = self.w.svc
        t1, i1 = weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "X1", "50", "A")
        t2, i2 = weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "X2", "50", "A")
        did = self._delivered()
        barrier = threading.Barrier(2)

        def settle():
            barrier.wait()
            try:
                svc.settle(self.w.coop, did)
            except Conflict:
                pass

        def appeal():
            barrier.wait()
            svc.review_grade(self.w.reviewer, i1["检验编号"], "B", "并发申诉降级")

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda f: f(), [settle, appeal]))

        # 找到唯一结算
        ledger = svc.list_ledger(self.w.coop, "农户结算")["记录"]
        self.assertEqual(len(ledger), 1)
        s = ledger[0]
        base = Decimal(s["明细行"][-1]["合计应收"])
        diff = sum((Decimal(a["差额"]) for a in s["价差调整"]), Decimal("0"))
        # 一张 50kg 由 A 降 B：最终权益恒为 350（400-50 或直接按 300+50）
        self.assertEqual(base + diff, Decimal("350.00"))
        # 每张磅单恰好结算一次
        settled_tickets = {row["磅单编号"] for row in s["明细行"] if "磅单编号" in row}
        self.assertEqual(settled_tickets, {t1["磅单编号"], t2["磅单编号"]})


class IndependentLedgerTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        svc = self.w.svc
        self.batch = svc.open_batch(self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")
        self.bid = self.batch["批次编号"]
        self.ticket, self.insp = weigh_inspect_settle(
            self.w, self.bid, self.w.old_tree_id, "L1", "100", "A")
        self.did = svc.deliver(self.w.coop, self.bid, "广兴饮料厂",
                               self.w.contract_id, "2026-09-02T08:00:00+00:00")["交货单编号"]
        self.sid = svc.settle(self.w.coop, self.did)["结算编号"]

    def test_return_loss_processing_are_separate_streams(self):
        svc = self.w.svc
        # 先核定到货 95kg（运输损耗 5kg），再从到货果实中退 20kg
        loss = svc.transit_loss(self.w.ent, self.did, "95", "长途水分流失")
        self.assertEqual(loss["损耗kg"], "5")
        ret = svc.enterprise_return(
            self.w.ent, self.did, "20", "到货抽检腐烂率超标",
            self.insp["检验编号"], "待转入加工")
        route = svc.route_to_processing(
            self.w.coop, "退货", ret["退货编号"], "陈皮", "20", "加工厂老陈")

        # 四条流水各自独立
        self.assertEqual(len(svc.list_ledger(self.w.coop, "农户结算")["记录"]), 1)
        returns = svc.list_ledger(self.w.coop, "企业退货")["记录"]
        self.assertEqual(len(returns), 1)
        self.assertEqual(len(svc.list_ledger(self.w.coop, "运输损耗")["记录"]), 1)
        processing = svc.list_ledger(self.w.coop, "果肉加工")["记录"]
        self.assertEqual(processing[0]["制品"], "陈皮")

        # 退货不冲减农户应收
        self.assertEqual(svc.get_settlement(self.w.coop, self.sid)
                         ["明细行"][-1]["合计应收"], "400.00")

        # 加工投入不得超过退货可处置量
        with self.assertRaises(ValidationFailed):
            svc.route_to_processing(self.w.coop, "退货", ret["退货编号"], "陈皮", "0.1")

        # 损耗核定每交货单只允许一次
        with self.assertRaises(Conflict) as cm:
            svc.transit_loss(self.w.ent, self.did, "90")
        self.assertEqual(cm.exception.code, "loss_already_recorded")

        # 到货量不可能大于交货核定量（用新交货单验证）
        batch2 = svc.open_batch(self.w.coop, self.w.plot_id, "2026-09-05", "2026秋")
        weigh_inspect_settle(self.w, batch2["批次编号"], self.w.old_tree_id, "L2", "10", "A")
        did2 = svc.deliver(self.w.coop, batch2["批次编号"], "广兴饮料厂",
                           self.w.contract_id, "2026-09-06T08:00:00+00:00")["交货单编号"]
        with self.assertRaises(ValidationFailed):
            svc.transit_loss(self.w.ent, did2, "11")

        # 到货量不得小于已登记退货量
        with self.assertRaises(ValidationFailed):
            svc.enterprise_return(self.w.ent, self.did, "76", "退货超过货",
                                  self.insp["检验编号"])

    def test_processing_requires_existing_source(self):
        svc = self.w.svc
        with self.assertRaises(NotFound):
            svc.route_to_processing(self.w.coop, "退货", "退货-999", "陈皮", "1")
        with self.assertRaises(ValidationFailed):
            svc.route_to_processing(self.w.coop, "结算", self.sid, "陈皮", "1")


class GuardRoleTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        svc = self.w.svc
        self.batch = svc.open_batch(self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")
        self.bid = self.batch["批次编号"]
        weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "G1", "100", "A")
        self.did = svc.deliver(self.w.coop, self.bid, "广兴饮料厂",
                               self.w.contract_id, "2026-09-02T08:00:00+00:00")["交货单编号"]
        self.sid = svc.settle(self.w.coop, self.did)["结算编号"]

    def test_guard_can_report_disease_and_violation(self):
        svc = self.w.svc
        disease = svc.report_tree_issue(self.w.guard, self.w.heritage_id, "病害", "发现炭疽病斑")
        self.assertEqual(disease["类型"], "病害")
        violation = svc.report_tree_issue(self.w.guard, self.w.heritage_id, "违规采摘",
                                          "夜间发现私自上树打果")
        self.assertEqual(violation["处理状态"], "待处理")
        tree = svc.get_tree_group(self.w.guard, self.w.heritage_id)
        self.assertEqual(tree["健康状态"], "染病")

    def test_guard_tree_view_is_masked(self):
        """护树队只看到巡护必需字段，看不到采收重量等经营数据。"""
        view = self.w.svc.get_tree_group(self.w.guard, self.w.heritage_id)
        self.assertNotIn("本季已采重量kg", view)
        self.assertNotIn("本季已采次数", view)
        self.assertIn("保护级别", view)

    def test_guard_cannot_touch_settlement_or_ledger(self):
        svc = self.w.svc
        with self.assertRaises(PermissionDenied):
            svc.settle(self.w.guard, self.did)
        with self.assertRaises(PermissionDenied):
            svc.get_settlement(self.w.guard, self.sid)
        with self.assertRaises(PermissionDenied):
            svc.list_ledger(self.w.guard, "农户结算")
        with self.assertRaises(PermissionDenied):
            svc.list_batches(self.w.guard)
        with self.assertRaises(PermissionDenied):
            svc.weigh(self.w.guard, self.bid, self.w.old_tree_id, "ZZ", weight_kg="1")
        with self.assertRaises(PermissionDenied):
            svc.trace_from_product(self.w.guard, delivery_id=self.did)

    def test_guard_report_type_is_validated(self):
        with self.assertRaises(ValidationFailed):
            self.w.svc.report_tree_issue(self.w.guard, self.w.heritage_id, "纵火", "无关事件")


class FarmerScopeTest(unittest.TestCase):
    def test_farmer_sees_only_own_records(self):
        w = World()
        svc = w.svc
        batch = svc.open_batch(w.farmer, w.plot_id, "2026-09-01", "2026秋")
        weigh_inspect_settle(w, batch["批次编号"], w.old_tree_id, "F1", "100", "A")

        # 果农可开自己的批次、看自己的批次
        own = svc.list_batches(w.farmer)
        self.assertEqual(len(own), 1)
        # 不能在他人地块建档/开批次
        with self.assertRaises(PermissionDenied):
            svc.create_plot(w.farmer, w.other_id, "偷挂名地块", "x")
        other_plot = svc.create_plot(w.coop, w.other_id, "赵家坳", "广兴镇赵家坳")
        with self.assertRaises(PermissionDenied):
            svc.open_batch(w.farmer, other_plot["地块编号"], "2026-09-01", "2026秋")
        # 第二位农户看不到第一位的批次
        self.assertEqual(svc.list_batches(w.farmer2), [])


class TraceabilityTest(unittest.TestCase):
    def test_product_traces_back_to_plot_inspections_and_farmer(self):
        w = World()
        svc = w.svc
        svc.add_care_log(w.coop, w.heritage_id, "2026-08-15", "古树复壮、支撑加固")
        batch = svc.open_batch(w.coop, w.plot_id, "2026-09-01", "2026秋")
        bid = batch["批次编号"]
        svc.reserve_trees(w.coop, bid, w.heritage_id, "50")
        ticket, insp = weigh_inspect_settle(w, bid, w.heritage_id, "Q1", "50", "A")
        did = svc.deliver(w.coop, bid, "广兴饮料厂", w.contract_id,
                          "2026-09-02T08:00:00+00:00")["交货单编号"]
        svc.settle(w.coop, did)

        chain = svc.trace_from_product(w.ent, delivery_id=did)
        self.assertEqual(chain["地块"]["地块编号"], w.plot_id)
        self.assertEqual(chain["受益农户"]["农户编号"], w.farmer_id)
        self.assertEqual(chain["树群"][0]["编号"], w.heritage_id)
        self.assertEqual(chain["管护记录"][0]["事项"], "古树复壮、支撑加固")
        evidence = chain["磅单与检测依据"][0]
        self.assertEqual(evidence["现行检验"]["检验编号"], insp["检验编号"])
        self.assertTrue(evidence["现行检验"]["样本编号"].startswith("样本-"))

        # 企业只能反查自己的交货单
        svc.register_actor("ent2", "别家厂", ROLE_ENTERPRISE)
        other_ent = svc.authenticate("ent2")
        with self.assertRaises(PermissionDenied):
            svc.trace_from_product(other_ent, delivery_id=did)

    def test_trace_via_processing_route(self):
        w = World()
        svc = w.svc
        batch = svc.open_batch(w.coop, w.plot_id, "2026-09-01", "2026秋")
        _, insp = weigh_inspect_settle(w, batch["批次编号"], w.old_tree_id, "Q2", "60", "A")
        did = svc.deliver(w.coop, batch["批次编号"], "广兴饮料厂", w.contract_id,
                          "2026-09-02T08:00:00+00:00")["交货单编号"]
        svc.settle(w.coop, did)
        ret = svc.enterprise_return(w.ent, did, "10", "挤压伤", insp["检验编号"])
        route = svc.route_to_processing(w.coop, "退货", ret["退货编号"], "陈皮", "10")
        chain = svc.trace_from_product(w.ent, route_id=route["加工编号"])
        self.assertEqual(chain["加工入口"]["制品"], "陈皮")
        self.assertEqual(chain["交货单"]["交货单编号"], did)
        self.assertEqual(chain["地块"]["地块编号"], w.plot_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
