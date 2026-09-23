"""老红橘保护性收购 —— 领域层。

设计要点
========

* **连续溯源链**：地块 → 树群 → 管护记录 → 采收批次 → 磅单 → 检验 → 企业交货，
  每一环节都引用上一环节的编号，任何一环缺失都无法进入下一环节。
* **每公斤只结算一次**：磅单以 ``(批次, 过磅流水号)`` 幂等，断网重传同一批果不会
  重复入库；结算以磅单为粒度，已结算磅单被结算流水永久引用。
* **保护树不得越界**：保护树群有按采收季生效的配额（重量 + 次数），配额按批次
  预占，磅单回冲；预占超额或磅单超出所属批次的预占额度都会被拒绝。
* **价格不回溯**：农户应收按 *签约时规则 + 交货时适用规则* 计算并写入结算流水，
  之后市场价变动只生成新价规版本，不改写历史流水。
* **复核留痕**：质量复核可改级，但原样本、原等级、原判定依据保留，等级变化产生
  独立的价差调整（补付或扣回），不覆盖原结算。
* **四条独立流水**：农户结算 / 企业退货 / 运输损耗 / 果肉加工路线（陈皮等）分账记录，
  互不混记。
* **角色隔离**：护树队只能上报病害与违规采摘，读取树群管护视图，无法接触农户
  结算明细；不同农户之间也互不可见。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------

ROLE_FARMER = "果农"          # 查看本人地块/批次/应收
ROLE_COOP = "合作社"          # 全链管理与结算
ROLE_REVIEWER = "质量复核人"  # 取样、复核改级
ROLE_GUARD = "护树队"         # 仅上报病害、违规采摘
ROLE_ENTERPRISE = "收购企业"  # 交货对接、成品原料反查

# 结算明细属于合作社财务域，护树队无权查看
SETTLER_ROLES = {ROLE_COOP}
SETTLE_DETAIL_VIEWERS = {ROLE_COOP}

# 护树队可见的最小树群视图
GUARD_TREE_VIEW = {"编号", "树群编号", "地块编号", "保护级别", "树种", "树龄年", "健康状态"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _money(value) -> str:
    """金额统一为两位小数字符串，避免浮点误差。"""
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class DomainError(Exception):
    """所有可预期的业务拒绝，HTTP 层映射为 4xx。"""

    status = 400

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class NotFound(DomainError):
    status = 404

    def __init__(self, what: str):
        super().__init__("not_found", f"{what}不存在或无权访问")


class Conflict(DomainError):
    status = 409

    def __init__(self, code: str, message: str):
        super().__init__(code, message)


class AuthError(DomainError):
    status = 401

    def __init__(self):
        super().__init__("unauthorized", "缺少有效身份令牌")


class PermissionDenied(DomainError):
    status = 403

    def __init__(self, what: str = "该操作"):
        super().__init__("forbidden", f"无权{what}")


class ValidationFailed(DomainError):
    status = 422

    def __init__(self, message: str):
        super().__init__("validation_failed", message)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Actor:
    token: str
    name: str
    role: str
    farmer_id: str | None = None  # 果农身份绑定的农户编号


@dataclass
class Plot:
    编号: str
    农户编号: str
    名称: str
    地点: str


@dataclass
class TreeGroup:
    编号: str
    地块编号: str
    名称: str
    树种: str
    树龄年: int
    保护级别: str          # 普通老树 / 百年保护树
    健康状态: str = "正常"
    本季已采重量: Decimal = Decimal("0")
    本季已采次数: int = 0


@dataclass
class CareLog:
    编号: str
    树群编号: str
    日期: str
    事项: str
    记录人: str
    病害: bool = False
    病害描述: str = ""


@dataclass
class HarvestBatch:
    编号: str
    地块编号: str
    农户编号: str
    采收日期: str
    季: str
    状态: str = "采收中"           # 采收中/待检验/待收购/已交货
    预占重量: Decimal = Decimal("0")
    交货单编号: str | None = None
    来源树群: list[str] = field(default_factory=list)


@dataclass
class WeighTicket:
    编号: str
    批次编号: str
    树群编号: str
    过磅流水号: str
    重量kg: Decimal
    毛重kg: Decimal | None
    皮重kg: Decimal | None
    过磅时间: str
    断网离线: bool
    设备号: str
    同步时间: str
    检验编号: str | None = None
    结算编号: str | None = None


@dataclass
class Inspection:
    编号: str
    磅单编号: str
    批次编号: str
    检验时间: str
    检验员: str
    等级: str
    糖度: Decimal | None
    判定依据: str
    样本编号: str
    样本封存位置: str
    来源: str = "初检"             # 初检 / 复核
    复核自: str | None = None
    现行: bool = True


@dataclass
class PriceRule:
    编号: str
    版本: int
    生效时间: str
    保护价: dict                  # 等级 -> 元/kg
    质量系数: dict                # 等级 -> 系数
    市场价参考: dict
    失效时间: str | None = None
    备注: str = ""


@dataclass
class Contract:
    编号: str
    农户编号: str
    企业编号: str
    签约时间: str
    价规编号: str                 # 签约时锁定的价规版本
    等级: list[str]
    季: str


@dataclass
class Delivery:
    编号: str
    企业编号: str
    批次编号: str
    农户编号: str
    合约编号: str
    交货时间: str
    价规编号: str                 # 交货时适用的价规版本
    毛重kg: Decimal
    备注: str = ""


@dataclass
class Settlement:
    编号: str
    农户编号: str
    批次编号: str
    合约编号: str
    交货单编号: str
    时间: str
    行: list[dict] = field(default_factory=list)
    调整编号: list[str] = field(default_factory=list)
    状态: str = "正常"            # 正常 / 含调整


@dataclass
class GradeAdjustment:
    编号: str
    原检验编号: str
    新检验编号: str
    磅单编号: str
    农户编号: str
    原等级: str
    新等级: str
    原单价: str
    新单价: str
    重量kg: Decimal
    差额: str                     # 正=补付，负=扣回
    时间: str
    结算编号: str
    方向: str                     # 补付 / 扣回


@dataclass
class EnterpriseReturn:
    编号: str
    交货单编号: str
    批次编号: str
    重量kg: Decimal
    原因: str
    时间: str
    检验依据编号: str
    处置: str = ""                # 备注，退款金额走企业对账，不进农户结算流水


@dataclass
class TransitLoss:
    编号: str
    交货单编号: str
    批次编号: str
    核定重量kg: Decimal
    到货重量kg: Decimal
    损耗kg: Decimal
    时间: str
    核定人: str
    备注: str = ""


@dataclass
class ProcessingRoute:
    编号: str
    来源类型: str                 # 退货 / 损耗
    来源单号: str
    批次编号: str
    制品: str                     # 陈皮 / 果酱 / ...
    投入重量kg: Decimal
    时间: str
    经办人: str


@dataclass
class Violation:
    编号: str
    树群编号: str
    地块编号: str | None
    类型: str                     # 病害 / 违规采摘
    描述: str
    上报人: str
    时间: str
    处理状态: str = "待处理"


# ---------------------------------------------------------------------------
# 领域服务
# ---------------------------------------------------------------------------


class HeritageCitrusService:
    """线程安全的内存领域服务（持久化可在序列面层替换存储）。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._actors: dict[str, Actor] = {}

        self._plots: dict[str, Plot] = {}
        self._trees: dict[str, TreeGroup] = {}
        self._cares: deque[CareLog] = deque()
        self._batches: dict[str, HarvestBatch] = {}
        self._tickets: dict[str, WeighTicket] = {}
        self._inspections: dict[str, Inspection] = {}
        self._rules: dict[str, PriceRule] = {}
        self._rule_versions: deque[str] = deque()
        self._contracts: dict[str, Contract] = {}
        self._deliveries: dict[str, Delivery] = {}
        self._settlements: dict[str, Settlement] = {}
        self._adjustments: dict[str, GradeAdjustment] = {}
        self._returns: dict[str, EnterpriseReturn] = {}
        self._losses: dict[str, TransitLoss] = {}
        self._routes: dict[str, ProcessingRoute] = {}
        self._violations: dict[str, Violation] = {}

        # 配额：树群 -> 季 -> 限额
        self._quotas: dict[tuple[str, str], dict] = {}
        # 保护树预占：批次 -> 树群 -> 重量
        self._reservations: dict[str, dict[str, Decimal]] = {}

        self._seq = 0
        self._farmer_of_plot: dict[str, str] = {}

    # ------------------------------------------------------------------ 工具

    def _id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:06d}"

    @staticmethod
    def _dec(value, label: str) -> Decimal:
        try:
            d = Decimal(str(value))
        except Exception:
            raise ValidationFailed(f"{label}必须是数字")
        if d < 0:
            raise ValidationFailed(f"{label}不能为负")
        return d

    def _require_role(self, actor: Actor, roles: set[str], what: str):
        if actor.role not in roles:
            raise PermissionDenied(what)

    # ------------------------------------------------------------------ 身份

    def register_actor(self, token: str, name: str, role: str, farmer_id: str | None = None) -> dict:
        with self._lock:
            if role not in {ROLE_FARMER, ROLE_COOP, ROLE_REVIEWER, ROLE_GUARD, ROLE_ENTERPRISE}:
                raise ValidationFailed("未知角色")
            if role == ROLE_FARMER and not farmer_id:
                raise ValidationFailed("果农身份必须绑定农户编号")
            self._actors[token] = Actor(token=token, name=name, role=role, farmer_id=farmer_id)
            return {"令牌": token, "姓名": name, "角色": role, "农户编号": farmer_id}

    def authenticate(self, token: str | None) -> Actor:
        if not token:
            raise AuthError()
        actor = self._actors.get(token)
        if actor is None:
            raise AuthError()
        return actor

    def _farmer_scope(self, actor: Actor, farmer_id: str):
        """果农只能操作/查看本人档案；护树队根本不在此列。"""
        if actor.role == ROLE_FARMER and actor.farmer_id != farmer_id:
            raise PermissionDenied("访问他人农户档案")
        if actor.role == ROLE_GUARD:
            raise PermissionDenied("访问农户业务档案")

    # ------------------------------------------------------------------ 基础建档

    def register_farmer(self, actor: Actor, name: str) -> dict:
        self._require_role(actor, {ROLE_COOP}, "登记农户")
        with self._lock:
            fid = self._id("农户")
            return {"农户编号": fid, "姓名": name}

    def create_plot(self, actor: Actor, farmer_id: str, name: str, location: str) -> dict:
        self._require_role(actor, {ROLE_COOP, ROLE_FARMER}, "建档地块")
        self._farmer_scope(actor, farmer_id)
        with self._lock:
            pid = self._id("地块")
            self._plots[pid] = Plot(pid, farmer_id, name, location)
            self._farmer_of_plot[pid] = farmer_id
            return self._plot_view(self._plots[pid])

    def register_tree_group(self, actor: Actor, plot_id: str, name: str, species: str,
                            age_years: int, protection: str) -> dict:
        self._require_role(actor, {ROLE_COOP, ROLE_FARMER}, "登记树群")
        with self._lock:
            plot = self._plots.get(plot_id)
            if plot is None:
                raise NotFound("地块")
            self._farmer_scope(actor, plot.农户编号)
            if protection not in {"普通老树", "百年保护树"}:
                raise ValidationFailed("保护级别必须是 普通老树 / 百年保护树")
            tid = self._id("树群")
            self._trees[tid] = TreeGroup(tid, plot_id, name, species, int(age_years), protection)
            return self._tree_view(self._trees[tid], actor)

    def set_quota(self, actor: Actor, tree_id: str, season: str, weight_kg, times: int) -> dict:
        """合作社对保护树群按采收季下达配额（百年树必须留种，配额从严）。"""
        self._require_role(actor, {ROLE_COOP}, "下达保护树配额")
        with self._lock:
            tree = self._trees.get(tree_id)
            if tree is None:
                raise NotFound("树群")
            if tree.保护级别 != "百年保护树":
                raise ValidationFailed("配额仅对百年保护树设置")
            weight = self._dec(weight_kg, "配额重量")
            if times < 0:
                raise ValidationFailed("配额次数不能为负")
            self._quotas[(tree_id, season)] = {"重量": weight, "次数": int(times)}
            return {"树群编号": tree_id, "季": season, "配额重量kg": str(weight), "配额次数": int(times)}

    def add_care_log(self, actor: Actor, tree_id: str, date: str, item: str,
                     disease: bool = False, disease_desc: str = "") -> dict:
        """管护记录。护树队可登记病害；日常管护由合作社/果农记录。"""
        allowed = {ROLE_COOP, ROLE_FARMER, ROLE_GUARD}
        self._require_role(actor, allowed, "记录管护信息")
        with self._lock:
            tree = self._trees.get(tree_id)
            if tree is None:
                raise NotFound("树群")
            if actor.role == ROLE_FARMER:
                self._farmer_scope(actor, self._farmer_of_plot[tree.地块编号])
            if disease:
                tree.健康状态 = "染病"
            log = CareLog(self._id("管护"), tree_id, date, item, actor.name,
                          disease, disease_desc)
            self._cares.append(log)
            return self._care_view(log)

    # ------------------------------------------------------------------ 采收与过磅

    def open_batch(self, actor: Actor, plot_id: str, harvest_date: str, season: str) -> dict:
        self._require_role(actor, {ROLE_COOP, ROLE_FARMER}, "开立采收批次")
        with self._lock:
            plot = self._plots.get(plot_id)
            if plot is None:
                raise NotFound("地块")
            self._farmer_scope(actor, plot.农户编号)
            bid = self._id("批次")
            self._batches[bid] = HarvestBatch(bid, plot_id, plot.农户编号, harvest_date, season)
            return self._batch_view(self._batches[bid])

    def reserve_trees(self, actor: Actor, batch_id: str, tree_id: str, weight_kg) -> dict:
        """采收前对保护树群预占额度（普通老树无需预占）。

        预占按树群-季汇总核校验配额，是“保护树采收不得越界”的第一道闸。
        """
        self._require_role(actor, {ROLE_COOP, ROLE_FARMER}, "预占保护树采收额度")
        weight = self._dec(weight_kg, "预占重量")
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                raise NotFound("采收批次")
            self._farmer_scope(actor, batch.农户编号)
            tree = self._trees.get(tree_id)
            if tree is None:
                raise NotFound("树群")
            if self._farmer_of_plot[tree.地块编号] != batch.农户编号:
                raise ValidationFailed("树群不属于该批次所属农户的地块")
            if tree.保护级别 != "百年保护树":
                raise ValidationFailed("普通老树无需预占")
            quota = self._quotas.get((tree_id, batch.季))
            if quota is None:
                raise Conflict("quota_not_set", "该保护树群本采收季尚未下达配额，不得采收")
            # 该树群本季总预占 = 其他批次预占 + 本批次既有预占 + 本次预占
            batch_res = self._reservations.setdefault(batch_id, {})
            already = batch_res.get(tree_id, Decimal("0"))
            # 该树群本季总预占 = 其他批次预占 + 本批次新值
            other = Decimal("0")
            for b in self._batches.values():
                if b.编号 == batch_id or b.季 != batch.季:
                    continue
                other += self._reservations.get(b.编号, {}).get(tree_id, Decimal("0"))
            total = other + already + weight
            if total > quota["重量"]:
                raise Conflict(
                    "quota_exceeded",
                    f"预占越界：{tree.名称} 本季配额 {quota['重量']}kg，"
                    f"已有预占 {other + already}kg，本次 {weight}kg",
                )
            if weight > 0:
                batch_res[tree_id] = already + weight
                batch.预占重量 += weight
                if tree_id not in batch.来源树群:
                    batch.来源树群.append(tree_id)
            return {"批次编号": batch_id, "树群编号": tree_id,
                    "本次预占kg": str(weight), "累计预占kg": str(batch_res[tree_id])}

    def weigh(self, actor: Actor, batch_id: str, tree_id: str, slip_no: str,
              weight_kg=None, gross_kg=None, tare_kg=None, offline=False,
              device: str = "", at: str | None = None) -> dict:
        """登记过磅单。

        * ``(批次, 过磅流水号)`` 幂等：断网设备恢复后重传同一张纸单/同一流水号，
          返回原记录而不是第二次入账——这是“同一批果不重复结算”的根。
        * 保护树群的磅单回冲本批次预占，超出预占即拒绝（现场超采立即暴露）。
        """
        self._require_role(actor, {ROLE_COOP, ROLE_FARMER}, "过磅登记")
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                raise NotFound("采收批次")
            self._farmer_scope(actor, batch.农户编号)
            if batch.状态 == "已交货":
                raise Conflict("batch_delivered", "批次已交货，不能追加过磅")
            tree = self._trees.get(tree_id)
            if tree is None:
                raise NotFound("树群")
            if self._farmer_of_plot[tree.地块编号] != batch.农户编号:
                raise ValidationFailed("树群不属于该批次所属农户的地块")

            # 幂等：同批次同流水号只认第一次
            for t in self._tickets.values():
                if t.批次编号 == batch_id and t.过磅流水号 == slip_no:
                    return self._ticket_view(t, actor, duplicated=True)

            if weight_kg is not None:
                weight = self._dec(weight_kg, "净重")
                gross = self._dec(gross_kg, "毛重") if gross_kg is not None else None
                tare = self._dec(tare_kg, "皮重") if tare_kg is not None else None
            else:
                gross = self._dec(gross_kg, "毛重")
                tare = self._dec(tare_kg, "皮重")
                weight = gross - tare
                if weight < 0:
                    raise ValidationFailed("皮重不能大于毛重")
            if weight <= 0:
                raise ValidationFailed("过磅净重必须大于零")

            # 保护树：回冲预占 + 配额次数
            if tree.保护级别 == "百年保护树":
                quota = self._quotas.get((tree_id, batch.季))
                if quota is None:
                    raise Conflict("quota_not_set", "该保护树群本采收季尚未下达配额，不得采收")
                batch_res = self._reservations.get(batch_id, {})
                reserved = batch_res.get(tree_id, Decimal("0"))
                used = sum(
                    tk.重量kg for tk in self._tickets.values()
                    if tk.批次编号 == batch_id and tk.树群编号 == tree_id
                )
                if used + weight > reserved:
                    raise Conflict(
                        "over_reservation",
                        f"超量采摘：{tree.名称} 本批次预占 {reserved}kg，"
                        f"已过磅 {used}kg，本次 {weight}kg 超出额度",
                    )
                times_used = sum(
                    1 for tk in self._tickets.values()
                    if tk.树群编号 == tree_id
                    and self._batches[tk.批次编号].季 == batch.季
                )
                if times_used + 1 > quota["次数"]:
                    raise Conflict("quota_times_exceeded",
                                   f"{tree.名称} 本季采摘次数超过配额 {quota['次数']} 次")

            tid = self._id("磅单")
            ticket = WeighTicket(
                编号=tid, 批次编号=batch_id, 树群编号=tree_id, 过磅流水号=slip_no,
                重量kg=weight, 毛重kg=gross, 皮重kg=tare,
                过磅时间=at or _now(), 断网离线=bool(offline), 设备号=device,
                同步时间=_now(),
            )
            self._tickets[tid] = ticket
            tree.本季已采重量 += weight
            tree.本季已采次数 += 1
            if tree_id not in batch.来源树群:
                batch.来源树群.append(tree_id)
            if batch.状态 == "采收中":
                batch.状态 = "待检验"
            return self._ticket_view(ticket, actor)

    # ------------------------------------------------------------------ 检验与复核

    def inspect(self, actor: Actor, ticket_id: str, grade: str, basis: str,
                brix=None, sample_location: str = "合作社留样柜") -> dict:
        """初检：取样、定级。样本必须封存，复核时据此比对。"""
        self._require_role(actor, {ROLE_COOP, ROLE_REVIEWER}, "果品检验")
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise NotFound("磅单")
            batch = self._batches[ticket.批次编号]
            if ticket.检验编号 is not None:
                raise Conflict("already_inspected", "该磅单已完成初检")
            iid = self._id("检验")
            sid = self._id("样本")
            insp = Inspection(
                编号=iid, 磅单编号=ticket_id, 批次编号=batch.编号,
                检验时间=_now(), 检验员=actor.name, 等级=grade,
                糖度=Decimal(str(brix)) if brix is not None else None,
                判定依据=basis, 样本编号=sid, 样本封存位置=sample_location,
            )
            self._inspections[iid] = insp
            ticket.检验编号 = iid
            if batch.状态 == "待检验":
                batch.状态 = "待收购"
            return self._inspection_view(insp)

    def review_grade(self, actor: Actor, inspection_id: str, new_grade: str,
                     new_basis: str, brix=None) -> dict:
        """质量复核改级。

        原检验记录与原样本完整保留（``现行=False`` 但不删除），新记录标明“复核自”，
        等级变化另走价差调整流水；尚未结算的磅单按新等级结算，不产生调整。
        """
        self._require_role(actor, {ROLE_REVIEWER, ROLE_COOP}, "质量复核")
        with self._lock:
            old = self._inspections.get(inspection_id)
            if old is None:
                raise NotFound("检验记录")
            if not old.现行:
                raise Conflict("superseded", "该检验已被后续复核取代，请对最新记录复核")
            ticket = self._tickets[old.磅单编号]
            old.现行 = False
            iid = self._id("检验")
            new = Inspection(
                编号=iid, 磅单编号=ticket.编号, 批次编号=old.批次编号,
                检验时间=_now(), 检验员=actor.name, 等级=new_grade,
                糖度=Decimal(str(brix)) if brix is not None else old.糖度,
                判定依据=new_basis, 样本编号=old.样本编号,
                样本封存位置=old.样本封存位置, 来源="复核", 复核自=old.编号,
            )
            self._inspections[iid] = new
            ticket.检验编号 = iid

            adjustment = None
            if ticket.结算编号 is not None:
                settlement = self._settlements[ticket.结算编号]
                adjustment = self._apply_grade_adjustment(actor, settlement, ticket, old, new)
            return {"新检验": self._inspection_view(new),
                    "原检验": self._inspection_view(old),
                    "价差调整": self._adjustment_view(adjustment) if adjustment else None}

    def _apply_grade_adjustment(self, actor: Actor, settlement: Settlement,
                                ticket: WeighTicket, old: Inspection, new: Inspection):
        delivery = self._deliveries[settlement.交货单编号]
        old_price = self._grade_unit_price(delivery.价规编号, old.等级)
        new_price = self._grade_unit_price(delivery.价规编号, new.等级)
        diff = (new_price - old_price) * ticket.重量kg
        aid = self._id("价差")
        adj = GradeAdjustment(
            编号=aid, 原检验编号=old.编号, 新检验编号=new.编号,
            磅单编号=ticket.编号, 农户编号=settlement.农户编号,
            原等级=old.等级, 新等级=new.等级,
            原单价=_money(old_price), 新单价=_money(new_price),
            重量kg=ticket.重量kg, 差额=_money(diff), 时间=_now(),
            结算编号=settlement.编号, 方向="补付" if diff >= 0 else "扣回",
        )
        self._adjustments[aid] = adj
        settlement.调整编号.append(aid)
        settlement.状态 = "含调整"
        return adj

    # ------------------------------------------------------------------ 价规与合约

    def publish_price_rule(self, actor: Actor, effective_at: str, protected: dict,
                           coefficients: dict | None = None, market_reference: dict | None = None,
                           note: str = "") -> dict:
        """发布新价规版本。历史版本永不修改——后续市场价只能另出新版。"""
        self._require_role(actor, {ROLE_COOP}, "发布价规")
        with self._lock:
            version = len(self._rule_versions) + 1
            rid = f"价规-v{version}"
            rule = PriceRule(
                编号=rid, 版本=version, 生效时间=effective_at,
                保护价={g: Decimal(str(p)) for g, p in protected.items()},
                质量系数={g: Decimal(str(c)) for g, c in (coefficients or {}).items()} or
                         {g: Decimal("1") for g in protected},
                市场价参考={g: Decimal(str(p)) for g, p in (market_reference or {}).items()},
                备注=note,
            )
            self._rules[rid] = rule
            self._rule_versions.append(rid)
            return self._rule_view(rule)

    def sign_contract(self, actor: Actor, farmer_id: str, enterprise_id: str,
                      signed_at: str, grades: list[str], season: str) -> dict:
        """签约：记录签约时刻的价规版本，作为该合约的基准规则。"""
        self._require_role(actor, {ROLE_COOP}, "签订收购合约")
        with self._lock:
            if not self._rule_versions:
                raise Conflict("no_price_rule", "尚未发布任何价规，无法签约")
            base_rule = self._rule_at(signed_at)
            cid = self._id("合约")
            contract = Contract(cid, farmer_id, enterprise_id, signed_at,
                                base_rule.编号, list(grades), season)
            self._contracts[cid] = contract
            return self._contract_view(contract)

    def _rule_at(self, timestamp: str) -> PriceRule:
        """返回指定时刻适用（已生效）的最新价规。"""
        current = None
        for rid in self._rule_versions:
            rule = self._rules[rid]
            if rule.生效时间 <= timestamp:
                current = rule
        if current is None:
            raise Conflict("no_applicable_rule", f"{timestamp} 前没有已生效的价规")
        return current

    def _grade_unit_price(self, rule_id: str, grade: str) -> Decimal:
        rule = self._rules[rule_id]
        if grade not in rule.保护价:
            raise ValidationFailed(f"价规 {rule_id} 未覆盖等级「{grade}」")
        return rule.保护价[grade] * rule.质量系数.get(grade, Decimal("1"))

    # ------------------------------------------------------------------ 交货与结算

    def deliver(self, actor: Actor, batch_id: str, enterprise_id: str,
                contract_id: str, delivered_at: str) -> dict:
        """整批交货：锁定交货时适用价规版本，随后按磅单结算。"""
        self._require_role(actor, {ROLE_COOP, ROLE_ENTERPRISE}, "登记交货")
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                raise NotFound("采收批次")
            if actor.role == ROLE_ENTERPRISE and enterprise_id != actor.name:
                # 企业编号与身份的弱校验；合作社代办不受限
                pass
            contract = self._contracts.get(contract_id)
            if contract is None:
                raise NotFound("收购合约")
            if contract.农户编号 != batch.农户编号:
                raise ValidationFailed("合约与批次不属于同一农户")
            if contract.企业编号 != enterprise_id:
                raise ValidationFailed("合约收购企业与交货企业不一致")
            if batch.状态 == "已交货":
                raise Conflict("already_delivered", "该批次已交货")
            tickets = [t for t in self._tickets.values() if t.批次编号 == batch_id]
            if not tickets:
                raise Conflict("empty_batch", "空批次不能交货")
            unspected = [t.编号 for t in tickets if t.检验编号 is None]
            if unspected:
                raise Conflict("inspection_pending", f"尚有磅单未检验：{unspected}")

            applicable = self._rule_at(delivered_at)
            did = self._id("交货")
            gross = sum(t.重量kg for t in tickets)
            delivery = Delivery(did, enterprise_id, batch_id, batch.农户编号,
                                contract_id, delivered_at, applicable.编号, gross)
            self._deliveries[did] = delivery
            batch.状态 = "已交货"
            batch.交货单编号 = did
            return self._delivery_view(delivery)

    def settle(self, actor: Actor, delivery_id: str) -> dict:
        """按 *签约规则 + 交货时适用价规* 为交货批次逐磅单结算。

        已结算磅单不可再次结算；结算金额在此时固化，之后任何价规新版本都不影响它。
        """
        self._require_role(actor, SETTLER_ROLES, "办理农户结算")
        with self._lock:
            delivery = self._deliveries.get(delivery_id)
            if delivery is None:
                raise NotFound("交货单")
            batch = self._batches[delivery.批次编号]

            tickets = [t for t in self._tickets.values() if t.批次编号 == batch.编号]
            fresh = [t for t in tickets if t.结算编号 is None]
            if not fresh:
                settled = [t.结算编号 for t in tickets]
                raise Conflict("already_settled",
                               f"该交货批次全部磅单已结算（{sorted(set(settled))}），每公斤果实只能结算一次")

            sid = self._id("结算")
            settlement = Settlement(
                编号=sid, 农户编号=delivery.农户编号, 批次编号=batch.编号,
                合约编号=delivery.合约编号, 交货单编号=delivery_id, 时间=_now(),
            )
            total = Decimal("0")
            for t in sorted(fresh, key=lambda x: x.编号):
                insp = self._inspections[t.检验编号]
                unit = self._grade_unit_price(delivery.价规编号, insp.等级)
                amount = unit * t.重量kg
                total += amount
                t.结算编号 = sid
                settlement.行.append({
                    "磅单编号": t.编号,
                    "过磅流水号": t.过磅流水号,
                    "树群编号": t.树群编号,
                    "重量kg": str(t.重量kg),
                    "等级": insp.等级,
                    "检验编号": insp.编号,
                    "单价": _money(unit),
                    "金额": _money(amount),
                })
            settlement.行.append({"合计应收": _money(total),
                                  "计价价规": delivery.价规编号,
                                  "签约价规": self._contracts[delivery.合约编号].价规编号})
            self._settlements[sid] = settlement
            return self._settlement_view(settlement)

    # ------------------------------------------------------------------ 三条后置独立流水

    def enterprise_return(self, actor: Actor, delivery_id: str, weight_kg,
                          reason: str, inspection_id: str, disposal: str = "") -> dict:
        """企业退货：独立流水。退货不冲减农户已结算应收（价差另走质量复核/合约争议）。

        数量勾稽：退货只能来自实际到货果实，故累计退货不得超过交货量；
        若运输损耗已核定，退货量还不得超过到货量。
        """
        self._require_role(actor, {ROLE_COOP, ROLE_ENTERPRISE}, "登记企业退货")
        weight = self._dec(weight_kg, "退货重量")
        with self._lock:
            delivery = self._deliveries.get(delivery_id)
            if delivery is None:
                raise NotFound("交货单")
            if inspection_id not in self._inspections:
                raise NotFound("退货检验依据")
            returned = sum(r.重量kg for r in self._returns.values()
                           if r.交货单编号 == delivery_id)
            if returned + weight > delivery.毛重kg:
                raise ValidationFailed(
                    f"退货累计 {returned + weight}kg 超过交货量 {delivery.毛重kg}kg")
            losses = [r for r in self._losses.values() if r.交货单编号 == delivery_id]
            if losses and returned + weight > losses[0].到货重量kg:
                raise ValidationFailed(
                    f"退货累计 {returned + weight}kg 超过实际到货量 "
                    f"{losses[0].到货重量kg}kg（退货只能来自到货果实）")
            rid = self._id("退货")
            rec = EnterpriseReturn(rid, delivery_id, delivery.批次编号, weight,
                                   reason, _now(), inspection_id, disposal)
            self._returns[rid] = rec
            return self._return_view(rec)

    def transit_loss(self, actor: Actor, delivery_id: str, arrived_kg, note: str = "") -> dict:
        """运输损耗：以交货核定毛重与到货过磅差额独立入账（每交货单核定一次）。"""
        self._require_role(actor, {ROLE_COOP, ROLE_ENTERPRISE}, "核定运输损耗")
        arrived = self._dec(arrived_kg, "到货重量")
        with self._lock:
            delivery = self._deliveries.get(delivery_id)
            if delivery is None:
                raise NotFound("交货单")
            if any(r.交货单编号 == delivery_id for r in self._losses.values()):
                raise Conflict("loss_already_recorded", "该交货单已核定过运输损耗")
            if arrived > delivery.毛重kg:
                raise ValidationFailed("到货重量不能大于交货核定重量")
            returned = sum(r.重量kg for r in self._returns.values()
                           if r.交货单编号 == delivery_id)
            if arrived < returned:
                raise ValidationFailed(
                    f"到货量 {arrived}kg 小于已登记退货 {returned}kg，数量无法勾稽")
            loss = delivery.毛重kg - arrived
            lid = self._id("损耗")
            rec = TransitLoss(lid, delivery_id, delivery.批次编号,
                              delivery.毛重kg, arrived, loss, _now(), actor.name, note)
            self._losses[lid] = rec
            return self._loss_view(rec)

    def route_to_processing(self, actor: Actor, source_type: str, source_id: str,
                            product: str, input_kg, handler: str = "") -> dict:
        """果肉转入加工路线（陈皮等）：只接受退货/损耗果实，独立流水，可回溯来源。"""
        self._require_role(actor, {ROLE_COOP}, "登记加工路线")
        amount = self._dec(input_kg, "投入重量")
        with self._lock:
            if source_type == "退货":
                src = self._returns.get(source_id)
            elif source_type == "损耗":
                src = self._losses.get(source_id)
            else:
                raise ValidationFailed("加工来源类型必须是 退货 / 损耗")
            if src is None:
                raise NotFound("加工来源单据")
            used = sum(r.投入重量kg for r in self._routes.values()
                       if r.来源类型 == source_type and r.来源单号 == source_id)
            if used + amount > src.重量kg:
                raise ValidationFailed(
                    f"加工投入 {used + amount}kg 超过该来源可处置量 {src.重量kg}kg")
            pid = self._id("加工")
            rec = ProcessingRoute(pid, source_type, source_id, src.批次编号,
                                  product, amount, _now(), handler or actor.name)
            self._routes[pid] = rec
            return self._route_view(rec)

    # ------------------------------------------------------------------ 护树队上报

    def report_tree_issue(self, actor: Actor, tree_id: str, kind: str,
                          description: str) -> dict:
        """护树队职责入口：上报病害或违规采摘。仅此而已——无结算读取权。"""
        self._require_role(actor, {ROLE_GUARD, ROLE_COOP}, "上报树体问题")
        if kind not in {"病害", "违规采摘"}:
            raise ValidationFailed("上报类型必须是 病害 / 违规采摘")
        with self._lock:
            tree = self._trees.get(tree_id)
            if tree is None:
                raise NotFound("树群")
            vid = self._id("巡护")
            rec = Violation(vid, tree_id, tree.地块编号, kind, description,
                            actor.name, _now())
            self._violations[vid] = rec
            if kind == "病害":
                tree.健康状态 = "染病"
            return self._violation_view(rec)

    # ------------------------------------------------------------------ 查询与反查

    def get_tree_group(self, actor: Actor, tree_id: str) -> dict:
        with self._lock:
            tree = self._trees.get(tree_id)
            if tree is None:
                raise NotFound("树群")
            if actor.role == ROLE_FARMER:
                self._farmer_scope(actor, self._farmer_of_plot[tree.地块编号])
            return self._tree_view(tree, actor)

    def list_batches(self, actor: Actor) -> list[dict]:
        with self._lock:
            out = []
            for b in self._batches.values():
                if actor.role == ROLE_FARMER and b.农户编号 != actor.farmer_id:
                    continue
                if actor.role == ROLE_GUARD:
                    raise PermissionDenied("查看农户采收批次")
                out.append(self._batch_view(b))
            return out

    def get_batch_detail(self, actor: Actor, batch_id: str) -> dict:
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                raise NotFound("采收批次")
            self._farmer_scope(actor, batch.农户编号)
            tickets = [t for t in self._tickets.values() if t.批次编号 == batch_id]
            return {
                **self._batch_view(batch),
                "磅单": [self._ticket_view(t, actor) for t in tickets],
                "来源树群": batch.来源树群,
            }

    def get_settlement(self, actor: Actor, settlement_id: str) -> dict:
        with self._lock:
            settlement = self._settlements.get(settlement_id)
            if settlement is None:
                raise NotFound("结算单")
            self._farmer_scope(actor, settlement.农户编号)
            if actor.role not in SETTLE_DETAIL_VIEWERS and actor.role != ROLE_FARMER:
                raise PermissionDenied("查看农户结算明细")
            return self._settlement_view(settlement)

    def list_ledger(self, actor: Actor, ledger: str) -> dict:
        """四条独立流水的账本导出。护树队对任何结算性账本均不可见。"""
        names = {
            "农户结算": (self._settlements, self._settlement_view),
            "企业退货": (self._returns, self._return_view),
            "运输损耗": (self._losses, self._loss_view),
            "果肉加工": (self._routes, self._route_view),
        }
        if ledger not in names:
            raise ValidationFailed("账本必须是 农户结算/企业退货/运输损耗/果肉加工")
        if actor.role == ROLE_GUARD:
            raise PermissionDenied("查看结算与货物流水")
        store, viewer = names[ledger]
        with self._lock:
            rows = []
            for rec in store.values():
                fid = getattr(rec, "农户编号", None)
                if actor.role == ROLE_FARMER and fid is not None and fid != actor.farmer_id:
                    continue
                if actor.role == ROLE_FARMER and ledger != "农户结算":
                    # 退货/损耗/加工是企业与合作社侧账，不向农户开放明细
                    continue
                rows.append(viewer(rec))
            return {"账本": ledger, "条数": len(rows), "记录": rows}

    def trace_from_product(self, actor: Actor, route_id: str = "", delivery_id: str = "") -> dict:
        """成品原料反查：从交货单（或其退货/加工去向）一路回到地块、检测依据、受益农户。

        企业可用此接口自证原料来源；护树队无权使用（会暴露农户信息）。
        """
        if actor.role == ROLE_GUARD:
            raise PermissionDenied("反查成品原料来源")
        with self._lock:
            if route_id:
                route = self._routes.get(route_id)
                if route is None:
                    raise NotFound("加工路线单")
                delivery_id = self._delivery_of_source(route.来源类型, route.来源单号)
                chain_route = self._route_view(route)
            else:
                chain_route = None

            delivery = self._deliveries.get(delivery_id)
            if delivery is None:
                raise NotFound("交货单")
            if actor.role == ROLE_FARMER and delivery.农户编号 != actor.farmer_id:
                raise PermissionDenied("反查他人交货批次")
            if actor.role == ROLE_ENTERPRISE and delivery.企业编号 != actor.name:
                raise PermissionDenied("反查非本企业交货单")

            batch = self._batches[delivery.批次编号]
            plot = self._plots[batch.地块编号]
            tickets = [t for t in self._tickets.values() if t.批次编号 == batch.编号]
            tree_ids = sorted({t.树群编号 for t in tickets})
            chain = {
                "交货单": self._delivery_view(delivery),
                "受益农户": {"农户编号": batch.农户编号},
                "地块": self._plot_view(plot),
                "采收批次": self._batch_view(batch),
                "树群": [
                    {k: v for k, v in self._tree_view(self._trees[t], actor).items()}
                    for t in tree_ids
                ],
                "管护记录": [
                    self._care_view(c) for c in self._cares
                    if c.树群编号 in tree_ids
                ],
                "磅单与检测依据": [
                    {
                        "磅单": self._ticket_view(t, actor),
                        "现行检验": self._inspection_view(self._inspections[t.检验编号]),
                        "历次检验": [
                            self._inspection_view(i) for i in self._inspections.values()
                            if i.磅单编号 == t.编号
                        ],
                    }
                    for t in sorted(tickets, key=lambda x: x.编号)
                ],
                "企业退货": [self._return_view(r) for r in self._returns.values()
                          if r.交货单编号 == delivery_id],
                "运输损耗": [self._loss_view(r) for r in self._losses.values()
                          if r.交货单编号 == delivery_id],
                "果肉加工": [self._route_view(r) for r in self._routes.values()
                          if r.批次编号 == batch.编号],
            }
            if chain_route:
                chain["加工入口"] = chain_route
            return chain

    def _delivery_of_source(self, source_type: str, source_id: str) -> str:
        if source_type == "退货":
            return self._returns[source_id].交货单编号
        return self._losses[source_id].交货单编号

    # ------------------------------------------------------------------ 序列化视图

    def _plot_view(self, p: Plot) -> dict:
        return {"地块编号": p.编号, "农户编号": p.农户编号, "名称": p.名称, "地点": p.地点}

    def _tree_view(self, t: TreeGroup, actor: Actor | None = None) -> dict:
        view = {
            "编号": t.编号, "树群编号": t.编号, "地块编号": t.地块编号,
            "名称": t.名称, "树种": t.树种, "树龄年": t.树龄年,
            "保护级别": t.保护级别, "健康状态": t.健康状态,
            "本季已采重量kg": str(t.本季已采重量), "本季已采次数": t.本季已采次数,
        }
        if actor is not None and actor.role == ROLE_GUARD:
            return {k: view[k] for k in GUARD_TREE_VIEW if k in view}
        return view

    def _care_view(self, c: CareLog) -> dict:
        return {"编号": c.编号, "树群编号": c.树群编号, "日期": c.日期,
                "事项": c.事项, "记录人": c.记录人, "病害": c.病害,
                "病害描述": c.病害描述}

    def _batch_view(self, b: HarvestBatch) -> dict:
        return {"批次编号": b.编号, "地块编号": b.地块编号, "农户编号": b.农户编号,
                "采收日期": b.采收日期, "季": b.季, "状态": b.状态,
                "预占重量kg": str(b.预占重量), "来源树群": b.来源树群,
                "交货单编号": b.交货单编号}

    def _ticket_view(self, t: WeighTicket, actor: Actor | None = None, duplicated: bool = False) -> dict:
        view = {"磅单编号": t.编号, "批次编号": t.批次编号, "树群编号": t.树群编号,
                "过磅流水号": t.过磅流水号, "重量kg": str(t.重量kg),
                "毛重kg": str(t.毛重kg) if t.毛重kg is not None else None,
                "皮重kg": str(t.皮重kg) if t.皮重kg is not None else None,
                "过磅时间": t.过磅时间, "断网离线": t.断网离线, "设备号": t.设备号,
                "同步时间": t.同步时间, "检验编号": t.检验编号, "结算编号": t.结算编号}
        if duplicated:
            view["幂等命中"] = True
        return view

    def _inspection_view(self, i: Inspection) -> dict:
        return {"检验编号": i.编号, "磅单编号": i.磅单编号, "批次编号": i.批次编号,
                "检验时间": i.检验时间, "检验员": i.检验员, "等级": i.等级,
                "糖度": str(i.糖度) if i.糖度 is not None else None,
                "判定依据": i.判定依据, "样本编号": i.样本编号,
                "样本封存位置": i.样本封存位置, "来源": i.来源,
                "复核自": i.复核自, "现行": i.现行}

    def _rule_view(self, r: PriceRule) -> dict:
        return {"价规编号": r.编号, "版本": r.版本, "生效时间": r.生效时间,
                "保护价": {g: str(p) for g, p in r.保护价.items()},
                "质量系数": {g: str(c) for g, c in r.质量系数.items()},
                "市场价参考": {g: str(p) for g, p in r.市场价参考.items()},
                "备注": r.备注}

    def _contract_view(self, c: Contract) -> dict:
        return {"合约编号": c.编号, "农户编号": c.农户编号, "企业编号": c.企业编号,
                "签约时间": c.签约时间, "签约价规": c.价规编号,
                "约定等级": c.等级, "季": c.季}

    def _delivery_view(self, d: Delivery) -> dict:
        return {"交货单编号": d.编号, "企业编号": d.企业编号, "批次编号": d.批次编号,
                "农户编号": d.农户编号, "合约编号": d.合约编号,
                "交货时间": d.交货时间, "计价价规": d.价规编号,
                "核定毛重kg": str(d.毛重kg)}

    def _settlement_view(self, s: Settlement) -> dict:
        return {"结算编号": s.编号, "农户编号": s.农户编号, "批次编号": s.批次编号,
                "合约编号": s.合约编号, "交货单编号": s.交货单编号,
                "时间": s.时间, "状态": s.状态, "明细行": s.行,
                "价差调整": [self._adjustment_view(self._adjustments[a])
                            for a in s.调整编号]}

    def _adjustment_view(self, a: GradeAdjustment) -> dict:
        return {"价差编号": a.编号, "原检验编号": a.原检验编号,
                "新检验编号": a.新检验编号, "磅单编号": a.磅单编号,
                "原等级": a.原等级, "新等级": a.新等级,
                "原单价": a.原单价, "新单价": a.新单价,
                "重量kg": str(a.重量kg), "差额": a.差额, "方向": a.方向,
                "时间": a.时间, "结算编号": a.结算编号}

    def _return_view(self, r: EnterpriseReturn) -> dict:
        return {"退货编号": r.编号, "交货单编号": r.交货单编号,
                "批次编号": r.批次编号, "重量kg": str(r.重量kg),
                "原因": r.原因, "检验依据编号": r.检验依据编号,
                "处置": r.处置, "时间": r.时间}

    def _loss_view(self, r: TransitLoss) -> dict:
        return {"损耗编号": r.编号, "交货单编号": r.交货单编号,
                "批次编号": r.批次编号, "核定重量kg": str(r.核定重量kg),
                "到货重量kg": str(r.到货重量kg), "损耗kg": str(r.损耗kg),
                "核定人": r.核定人, "时间": r.时间, "备注": r.备注}

    def _route_view(self, r: ProcessingRoute) -> dict:
        return {"加工编号": r.编号, "来源类型": r.来源类型, "来源单号": r.来源单号,
                "批次编号": r.批次编号, "制品": r.制品,
                "投入重量kg": str(r.投入重量kg), "经办人": r.经办人, "时间": r.时间}

    def _violation_view(self, v: Violation) -> dict:
        return {"巡护编号": v.编号, "树群编号": v.树群编号, "地块编号": v.地块编号,
                "类型": v.类型, "描述": v.描述, "上报人": v.上报人,
                "时间": v.时间, "处理状态": v.处理状态}
