"""老红橘保护性收购的运行入口与 HTTP 接口。

接口约定
========
* 除 ``GET /health`` 外，所有接口需要 ``X-Actor-Token`` 头（先经 ``POST /actors`` 登记）。
* 请求/响应均为 JSON；业务拒绝返回 ``{"error": 代码, "message": ...}`` 与对应 4xx 状态。
* 领域不变量全部在 :mod:`domain` 内校验，本层只做协议适配。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse, parse_qs

from domain import (
    Actor,
    AuthError,
    DomainError,
    HeritageCitrusService,
)

SERVICE_ID = "heritage-citrus"
SERVICE_NAME = "老红橘保护性收购"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def make_handler(service: HeritageCitrusService):
    """按领域服务实例构造 Handler，便于测试隔离。"""

    class Handler(BaseHTTPRequestHandler):
        svc = service

        # -------------------------------------------------------------- 框架

        def _send(self, status: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError:
                raise DomainError("bad_json", "请求体不是合法 JSON")
            if not isinstance(data, dict):
                raise DomainError("bad_json", "请求体必须是 JSON 对象")
            return data

        def _actor(self) -> Actor:
            return self.svc.authenticate(self.headers.get("X-Actor-Token"))

        def log_message(self, *_args):
            return

        # -------------------------------------------------------------- 路由

        def do_GET(self):
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            try:
                if path == "/health":
                    self._send(200, health_payload())
                    return
                known = (path == "/batches" or path == "/trace"
                         or path.startswith(("/batches/", "/trees/",
                                             "/settlements/", "/ledgers/")))
                if not known:
                    self._send(404, {"error": "not_found", "message": "未知路由"})
                    return
                actor = self._actor()
                qs = parse_qs(parsed.query)

                if path == "/batches":
                    self._send(200, {"记录": self.svc.list_batches(actor)})
                elif path.startswith("/batches/"):
                    self._send(200, self.svc.get_batch_detail(actor, path.split("/")[2]))
                elif path.startswith("/trees/"):
                    self._send(200, self.svc.get_tree_group(actor, path.split("/")[2]))
                elif path.startswith("/settlements/"):
                    self._send(200, self.svc.get_settlement(actor, path.split("/")[2]))
                elif path.startswith("/ledgers/"):
                    ledger = path.split("/", 2)[2]
                    self._send(200, self.svc.list_ledger(actor, ledger))
                elif path == "/trace":
                    delivery_id = qs.get("delivery", [""])[0]
                    route_id = qs.get("route", [""])[0]
                    if not delivery_id and not route_id:
                        raise DomainError("bad_request", "需要 delivery 或 route 查询参数")
                    self._send(200, self.svc.trace_from_product(
                        actor, route_id=route_id, delivery_id=delivery_id))
                else:
                    self._send(404, {"error": "not_found", "message": "未知路由"})
            except DomainError as exc:
                self._send(exc.status, {"error": exc.code, "message": exc.message})

        def do_POST(self):
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            try:
                data = self._read_json()

                # 身份登记是唯一的引导入口，不需要令牌
                if path == "/actors":
                    self._send(201, self.svc.register_actor(
                        token=data["token"], name=data.get("姓名", ""),
                        role=data["角色"], farmer_id=data.get("农户编号")))
                    return

                known_post = {
                    "/farmers", "/plots", "/trees", "/quotas", "/cares",
                    "/batches", "/reservations", "/weigh", "/inspections", "/reviews",
                    "/price-rules", "/contracts", "/deliveries", "/settlements",
                    "/returns", "/losses", "/processing", "/reports",
                }
                if path not in known_post:
                    self._send(404, {"error": "not_found", "message": "未知路由"})
                    return

                actor = self._actor()
                svc = self.svc

                if path == "/farmers":
                    self._send(201, svc.register_farmer(actor, data.get("姓名", "")))
                elif path == "/plots":
                    self._send(201, svc.create_plot(
                        actor, data["农户编号"], data["名称"], data.get("地点", "")))
                elif path == "/trees":
                    self._send(201, svc.register_tree_group(
                        actor, data["地块编号"], data["名称"], data["树种"],
                        int(data["树龄年"]), data["保护级别"]))
                elif path == "/quotas":
                    self._send(201, svc.set_quota(
                        actor, data["树群编号"], data["季"],
                        data["配额重量kg"], int(data["配额次数"])))
                elif path == "/cares":
                    self._send(201, svc.add_care_log(
                        actor, data["树群编号"], data["日期"], data["事项"],
                        bool(data.get("病害", False)), data.get("病害描述", "")))
                elif path == "/batches":
                    self._send(201, svc.open_batch(
                        actor, data["地块编号"], data["采收日期"], data["季"]))
                elif path == "/reservations":
                    self._send(201, svc.reserve_trees(
                        actor, data["批次编号"], data["树群编号"], data["重量kg"]))
                elif path == "/weigh":
                    self._send(201, svc.weigh(
                        actor, data["批次编号"], data["树群编号"], data["过磅流水号"],
                        weight_kg=data.get("重量kg"), gross_kg=data.get("毛重kg"),
                        tare_kg=data.get("皮重kg"), offline=bool(data.get("断网离线", False)),
                        device=data.get("设备号", ""), at=data.get("过磅时间")))
                elif path == "/inspections":
                    self._send(201, svc.inspect(
                        actor, data["磅单编号"], data["等级"], data["判定依据"],
                        brix=data.get("糖度"), sample_location=data.get("样本封存位置", "合作社留样柜")))
                elif path == "/reviews":
                    self._send(201, svc.review_grade(
                        actor, data["检验编号"], data["新等级"],
                        data["新判定依据"], brix=data.get("糖度")))
                elif path == "/price-rules":
                    self._send(201, svc.publish_price_rule(
                        actor, data["生效时间"], data["保护价"],
                        coefficients=data.get("质量系数"),
                        market_reference=data.get("市场价参考"),
                        note=data.get("备注", "")))
                elif path == "/contracts":
                    self._send(201, svc.sign_contract(
                        actor, data["农户编号"], data["企业编号"],
                        data["签约时间"], data["约定等级"], data["季"]))
                elif path == "/deliveries":
                    self._send(201, svc.deliver(
                        actor, data["批次编号"], data["企业编号"],
                        data["合约编号"], data["交货时间"]))
                elif path == "/settlements":
                    self._send(201, svc.settle(actor, data["交货单编号"]))
                elif path == "/returns":
                    self._send(201, svc.enterprise_return(
                        actor, data["交货单编号"], data["重量kg"],
                        data["原因"], data["检验依据编号"], data.get("处置", "")))
                elif path == "/losses":
                    self._send(201, svc.transit_loss(
                        actor, data["交货单编号"], data["到货重量kg"],
                        data.get("备注", "")))
                elif path == "/processing":
                    self._send(201, svc.route_to_processing(
                        actor, data["来源类型"], data["来源单号"],
                        data["制品"], data["投入重量kg"], data.get("经办人", "")))
                elif path == "/reports":
                    self._send(201, svc.report_tree_issue(
                        actor, data["树群编号"], data["类型"], data["描述"]))
                else:
                    self._send(404, {"error": "not_found", "message": "未知路由"})
            except KeyError as exc:
                self._send(422, {"error": "validation_failed",
                                 "message": f"缺少必填字段：{exc.args[0]}"})
            except DomainError as exc:
                self._send(exc.status, {"error": exc.code, "message": exc.message})

    return Handler


# 默认单例：命令行运行与既有 service_contract 测试使用
service = HeritageCitrusService()
Handler = make_handler(service)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
