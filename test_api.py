"""HTTP 层契约测试：鉴权、错误码映射、断网重传与护树队隔离走真实端口。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from domain import HeritageCitrusService
from service import make_handler


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = HeritageCitrusService()
        handler = make_handler(cls.service)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method: str, path: str, payload=None, token=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["X-Actor-Token"] = token
        req = Request(f"{self.base}{quote(path, safe='/?=&')}", data=data,
                      headers=headers, method=method)
        try:
            with urlopen(req, timeout=3) as resp:
                return resp.status, json.load(resp)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    def setUp(self):
        # 每个用例重新登记角色（编号会累计，但语义互不影响）
        _, = (self.call("POST", "/actors",
                        {"token": "coop", "姓名": "秦会计", "角色": "合作社"}),)
        self.call("POST", "/actors", {"token": "reviewer", "姓名": "严复核", "角色": "质量复核人"})
        self.call("POST", "/actors", {"token": "guard", "姓名": "护树员老吴", "角色": "护树队"})
        self.call("POST", "/actors", {"token": "ent", "姓名": "广兴饮料厂", "角色": "收购企业"})
        _, farmer = self.call("POST", "/farmers", {"姓名": "梁果农"}, token="coop")
        self.farmer_id = farmer["农户编号"]
        self.call("POST", "/actors",
                  {"token": "farmer", "姓名": "梁果农", "角色": "果农",
                   "农户编号": self.farmer_id})
        _, plot = self.call("POST", "/plots",
                            {"农户编号": self.farmer_id, "名称": "梁家湾坡地",
                             "地点": "广兴镇"}, token="coop")
        self.plot_id = plot["地块编号"]
        _, tree = self.call("POST", "/trees",
                            {"地块编号": self.plot_id, "名称": "连片老红橘",
                             "树种": "红橘", "树龄年": 60, "保护级别": "普通老树"},
                            token="coop")
        self.tree_id = tree["编号"]
        _, heritage = self.call("POST", "/trees",
                                {"地块编号": self.plot_id, "名称": "百年母树群",
                                 "树种": "红橘", "树龄年": 130,
                                 "保护级别": "百年保护树"}, token="coop")
        self.heritage_id = heritage["编号"]
        self.call("POST", "/price-rules",
                  {"生效时间": "2026-08-01T00:00:00+00:00",
                   "保护价": {"A": "4.00", "B": "3.00"}}, token="coop")
        _, contract = self.call("POST", "/contracts",
                                {"农户编号": self.farmer_id, "企业编号": "广兴饮料厂",
                                 "签约时间": "2026-08-05T00:00:00+00:00",
                                 "约定等级": ["A", "B"], "季": "2026秋"}, token="coop")
        self.contract_id = contract["合约编号"]
        self.call("POST", "/quotas",
                  {"树群编号": self.heritage_id, "季": "2026秋",
                   "配额重量kg": "100", "配额次数": 2}, token="coop")

    def test_health_is_open(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_endpoints_require_token(self):
        status, body = self.call("GET", "/batches")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")
        status, _ = self.call("POST", "/weigh", {"批次编号": "x"})
        self.assertEqual(status, 401)

    def test_end_to_end_flow_with_offline_resend_and_settlement(self):
        _, batch = self.call("POST", "/batches",
                             {"地块编号": self.plot_id, "采收日期": "2026-09-01",
                              "季": "2026秋"}, token="coop")
        bid = batch["批次编号"]

        payload = {"批次编号": bid, "树群编号": self.tree_id, "过磅流水号": "NET-1",
                   "毛重kg": "210", "皮重kg": "10", "断网离线": True, "设备号": "地磅-02"}
        status, ticket = self.call("POST", "/weigh", payload, token="coop")
        self.assertEqual(status, 201)
        self.assertEqual(ticket["重量kg"], "200")
        # 断网恢复后重放：幂等
        _, again = self.call("POST", "/weigh", payload, token="coop")
        self.assertTrue(again["幂等命中"])
        self.assertEqual(again["磅单编号"], ticket["磅单编号"])

        _, insp = self.call("POST", "/inspections",
                            {"磅单编号": ticket["磅单编号"], "等级": "A",
                             "判定依据": "糖度外观达标"}, token="reviewer")
        _, delivery = self.call("POST", "/deliveries",
                                {"批次编号": bid, "企业编号": "广兴饮料厂",
                                 "合约编号": self.contract_id,
                                 "交货时间": "2026-09-02T08:00:00+00:00"}, token="coop")
        _, settlement = self.call("POST", "/settlements",
                                  {"交货单编号": delivery["交货单编号"]}, token="coop")
        self.assertEqual(settlement["明细行"][-1]["合计应收"], "800.00")

        # 重复结算 → 409
        status, body = self.call("POST", "/settlements",
                                 {"交货单编号": delivery["交货单编号"]}, token="coop")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "already_settled")

    def test_guard_permission_boundary_over_http(self):
        # 护树队上报病害：允许
        status, _ = self.call("POST", "/reports",
                              {"树群编号": self.heritage_id, "类型": "病害",
                               "描述": "发现病斑"}, token="guard")
        self.assertEqual(status, 201)
        # 护树队查看结算：403
        status, body = self.call("GET", "/ledgers/农户结算", token="guard")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")
        # 护树队过磅：403
        _, batch = self.call("POST", "/batches",
                             {"地块编号": self.plot_id, "采收日期": "2026-09-01",
                              "季": "2026秋"}, token="coop")
        status, _ = self.call("POST", "/weigh",
                              {"批次编号": batch["批次编号"], "树群编号": self.tree_id,
                               "过磅流水号": "Z1", "重量kg": "1"}, token="guard")
        self.assertEqual(status, 403)
        # 护树队的树群视图被裁剪
        _, view = self.call("GET", f"/trees/{self.heritage_id}", token="guard")
        self.assertNotIn("本季已采重量kg", view)

    def test_heritage_quota_enforced_over_http(self):
        _, batch = self.call("POST", "/batches",
                             {"地块编号": self.plot_id, "采收日期": "2026-09-01",
                              "季": "2026秋"}, token="coop")
        bid = batch["批次编号"]
        status, _ = self.call("POST", "/reservations",
                              {"批次编号": bid, "树群编号": self.heritage_id,
                               "重量kg": "101"}, token="coop")
        self.assertEqual(status, 409)
        self.call("POST", "/reservations",
                  {"批次编号": bid, "树群编号": self.heritage_id, "重量kg": "50"},
                  token="coop")
        self.call("POST", "/weigh",
                  {"批次编号": bid, "树群编号": self.heritage_id,
                   "过磅流水号": "H1", "重量kg": "50"}, token="coop")
        status, body = self.call("POST", "/weigh",
                                 {"批次编号": bid, "树群编号": self.heritage_id,
                                  "过磅流水号": "H2", "重量kg": "1"}, token="coop")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "over_reservation")

    def test_replay_vs_conflict_are_distinguished_over_http(self):
        _, batch = self.call("POST", "/batches",
                             {"地块编号": self.plot_id, "采收日期": "2026-09-01",
                              "季": "2026秋"}, token="coop")
        bid = batch["批次编号"]
        payload = {"批次编号": bid, "树群编号": self.tree_id, "过磅流水号": "W-1",
                   "重量kg": "100", "断网离线": True, "设备号": "地磅-02"}
        status, ticket = self.call("POST", "/weigh", payload, token="coop")
        self.assertEqual(status, 201)

        # 完全相同补传：200 幂等命中（重放）
        status, replay = self.call("POST", "/weigh", payload, token="coop")
        self.assertEqual(status, 201)
        self.assertTrue(replay["幂等命中"])

        # 净重被人工补录改掉：409 + 结构化冲突载荷（冲突，不是重放）
        changed = {**payload, "重量kg": "110", "断网离线": False}
        status, body = self.call("POST", "/weigh", changed, token="coop")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "ticket_conflict")
        self.assertIn("冲突", body)
        self.assertEqual(body["冲突"]["字段差异"][0]["字段"], "净重kg")
        cid = body["冲突"]["冲突编号"]

        # 磅单仍只有一张、原值未动
        _, detail = self.call("GET", f"/batches/{bid}", token="coop")
        self.assertEqual(len(detail["磅单"]), 1)
        self.assertEqual(detail["磅单"][0]["重量kg"], "100")

        # 非法补传不登记冲突、不占流水号
        bad = {**payload, "过磅流水号": "W-2", "毛重kg": "10", "皮重kg": "20", "重量kg": None}
        status, _ = self.call("POST", "/weigh", bad, token="coop")
        self.assertEqual(status, 422)
        status, conflicts = self.call("GET", "/conflicts?状态=待复核", token="coop")
        self.assertEqual(status, 200)
        self.assertEqual(len(conflicts["记录"]), 1)

        # 护树队/企业无权看冲突
        for token in ("guard", "ent"):
            status, denied = self.call("GET", "/conflicts", token=token)
            self.assertEqual(status, 403)
            self.assertEqual(denied["error"], "forbidden")

        # 果农只看到自己的裁剪摘要（无付款字段）
        status, mine = self.call("GET", "/conflicts", token="farmer")
        self.assertEqual(status, 200)
        self.assertEqual(len(mine["记录"]), 1)
        self.assertNotIn("调整编号", mine["记录"][0])

        # 无权限角色不能复核
        status, _ = self.call("POST", "/conflicts/resolve",
                              {"冲突编号": cid, "复核结论": "保留原值"}, token="farmer")
        self.assertEqual(status, 403)

    def test_resolve_keep_then_correct_after_settlement_http(self):
        _, batch = self.call("POST", "/batches",
                             {"地块编号": self.plot_id, "采收日期": "2026-09-01",
                              "季": "2026秋"}, token="coop")
        bid = batch["批次编号"]
        _, ticket = self.call("POST", "/weigh",
                              {"批次编号": bid, "树群编号": self.tree_id, "过磅流水号": "W-9",
                               "重量kg": "100", "设备号": "地磅-02"}, token="coop")
        self.call("POST", "/inspections",
                  {"磅单编号": ticket["磅单编号"], "等级": "A", "判定依据": "达标"},
                  token="reviewer")

        def raise_conflict(new_weight):
            status, body = self.call("POST", "/weigh",
                                     {"批次编号": bid, "树群编号": self.tree_id,
                                      "过磅流水号": "W-9", "重量kg": new_weight,
                                      "设备号": "地磅-02"}, token="coop")
            self.assertEqual(status, 409)
            return body["冲突"]["冲突编号"]

        # 第一轮冲突：合作社保留原值
        cid1 = raise_conflict("95")
        status, kept = self.call("POST", "/conflicts/resolve",
                                 {"冲突编号": cid1, "复核结论": "保留原值",
                                  "复核意见": "纸单为准"}, token="coop")
        self.assertEqual(status, 200)
        self.assertEqual(kept["处置"], "保留原值")

        # 交货→结算
        _, delivery = self.call("POST", "/deliveries",
                                {"批次编号": bid, "企业编号": "广兴饮料厂",
                                 "合约编号": self.contract_id,
                                 "交货时间": "2026-09-02T08:00:00+00:00"}, token="coop")
        did = delivery["交货单编号"]
        _, settlement = self.call("POST", "/settlements",
                                  {"交货单编号": did}, token="coop")
        sid = settlement["结算编号"]
        self.assertEqual(settlement["明细行"][-1]["合计应收"], "400.00")

        # 结算后再起冲突：只能形成差异调整（质量复核人处置）
        cid2 = raise_conflict("110")
        status, resolved = self.call("POST", "/conflicts/resolve",
                                     {"冲突编号": cid2, "复核结论": "生成更正磅单",
                                      "复核意见": "补录净重110"}, token="reviewer")
        self.assertEqual(status, 200)
        self.assertEqual(resolved["处置"], "差异调整(已结算)")
        self.assertEqual(resolved["差异调整"]["金额"], "40.00")
        self.assertEqual(resolved["差异调整"]["结算编号"], sid)

        # 原结算不改写，重量调整独立挂账；磅单未被替换
        _, again = self.call("GET", f"/settlements/{sid}", token="coop")
        self.assertEqual(again["明细行"][-1]["合计应收"], "400.00")
        self.assertEqual(again["重量调整"][0]["差异kg"], "10")

        # 已解决冲突不能重复处置
        status, body = self.call("POST", "/conflicts/resolve",
                                 {"冲突编号": cid2, "复核结论": "保留原值"}, token="coop")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "conflict_resolved")

    def test_pre_delivery_correction_then_chain_http(self):
        _, batch = self.call("POST", "/batches",
                             {"地块编号": self.plot_id, "采收日期": "2026-09-01",
                              "季": "2026秋"}, token="coop")
        bid = batch["批次编号"]
        _, ticket = self.call("POST", "/weigh",
                              {"批次编号": bid, "树群编号": self.tree_id, "过磅流水号": "W-7",
                               "重量kg": "100", "设备号": "地磅-02"}, token="coop")
        self.call("POST", "/inspections",
                  {"磅单编号": ticket["磅单编号"], "等级": "A", "判定依据": "达标"},
                  token="reviewer")
        _, body = self.call("POST", "/weigh",
                            {"批次编号": bid, "树群编号": self.tree_id, "过磅流水号": "W-7",
                             "重量kg": "90", "设备号": "地磅-02"}, token="coop")
        cid = body["冲突"]["冲突编号"]
        status, resolved = self.call("POST", "/conflicts/resolve",
                                     {"冲突编号": cid, "复核结论": "生成更正磅单"},
                                     token="coop")
        self.assertEqual(status, 200)
        new_id = resolved["更正磅单"]["磅单编号"]
        self.assertNotEqual(new_id, ticket["磅单编号"])
        self.assertEqual(resolved["原磅单"]["状态"], "已更正")

        # 待检验冲突清空后，更正单需重新检验才能交货
        status, blocked = self.call("POST", "/deliveries",
                                    {"批次编号": bid, "企业编号": "广兴饮料厂",
                                     "合约编号": self.contract_id,
                                     "交货时间": "2026-09-02T08:00:00+00:00"}, token="coop")
        self.assertEqual(status, 409)
        self.assertEqual(blocked["error"], "inspection_pending")
        self.call("POST", "/inspections",
                  {"磅单编号": new_id, "等级": "A", "判定依据": "更正单复检"},
                  token="reviewer")
        _, delivery = self.call("POST", "/deliveries",
                                {"批次编号": bid, "企业编号": "广兴饮料厂",
                                 "合约编号": self.contract_id,
                                 "交货时间": "2026-09-02T08:00:00+00:00"}, token="coop")
        _, settlement = self.call("POST", "/settlements",
                                  {"交货单编号": delivery["交货单编号"]}, token="coop")
        # 只有更正单 90kg 进结算链
        rows = [r for r in settlement["明细行"] if "磅单编号" in r]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["磅单编号"], new_id)
        self.assertEqual(settlement["明细行"][-1]["合计应收"], "360.00")

    def test_trace_and_validation_errors(self):
        _, batch = self.call("POST", "/batches",
                             {"地块编号": self.plot_id, "采收日期": "2026-09-01",
                              "季": "2026秋"}, token="coop")
        bid = batch["批次编号"]
        _, ticket = self.call("POST", "/weigh",
                              {"批次编号": bid, "树群编号": self.tree_id,
                               "过磅流水号": "T1", "重量kg": "100"}, token="coop")
        self.call("POST", "/inspections",
                  {"磅单编号": ticket["磅单编号"], "等级": "A",
                   "判定依据": "达标"}, token="reviewer")
        _, delivery = self.call("POST", "/deliveries",
                                {"批次编号": bid, "企业编号": "广兴饮料厂",
                                 "合约编号": self.contract_id,
                                 "交货时间": "2026-09-02T08:00:00+00:00"}, token="coop")
        status, chain = self.call("GET", f"/trace?delivery={delivery['交货单编号']}",
                                  token="ent")
        self.assertEqual(status, 200)
        self.assertEqual(chain["地块"]["地块编号"], self.plot_id)
        self.assertEqual(chain["受益农户"]["农户编号"], self.farmer_id)

        # 缺字段 → 422
        status, body = self.call("POST", "/plots", {"名称": "无名地块"}, token="coop")
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "validation_failed")
        # 未知路由 → 404
        status, _ = self.call("GET", "/nope", token="coop")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
