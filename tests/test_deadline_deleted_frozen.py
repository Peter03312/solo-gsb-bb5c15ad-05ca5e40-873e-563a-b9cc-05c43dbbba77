"""本轮功能测试: 删除凭据冻结序号与统一评审截止的交叉处理。

缺陷背景: 论文删除后当前发布版保留旧槽位 (删除凭据冻结该发布序号); 若该版
统一评审截止已过, 同编号重录后尝试补位时, 旧槽位仍被判逾期——求解器把原评审
人以 review_overdue 排除, 可能导致两名合格评审人也无法补齐。

修复契约:
- 删除凭据冻结的旧序号槽位 (含同编号重录但未重新发布) 不计入当前截止状态
  汇总 (GET /review-deadline 的 summary), 逐槽位明细带 deleted 标记并保留
  删除前的历史状态供追溯, 但绝不标 overdue;
- 补位预演的逾期明细 (overdue_slots) 与 fixed_review_overdue 释放不含冻结
  槽位; 也不得因旧截止把重录稿再次分给原评审人 (无 review_overdue 排除),
  重录稿对全部合格评审人开放, 仅服从既有求解硬约束;
- 旧确认与旧评语仅供会务方追溯: 补位发布推进序号后冻结槽位全部释放, 评审人
  在新序号为 pending, 须重新确认并重新提交;
- 其他论文的真实逾期照常释放, 且禁止该稿原槽位评审人重新获稿;
- 预演与发布结果一致; 版本不符或无完整方案时拒绝且不改发布版。
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

_TMPDIR = tempfile.mkdtemp(prefix="review-deadline-deleted-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["ORGANIZER_KEY"] = "test-organizer-key"

import pytest
from fastapi.testclient import TestClient

from app import db, main
from app.main import app

ORG = {"X-Organizer-Key": "test-organizer-key"}


def reviewer_headers(rid):
    return {"X-Reviewer-Id": rid, "X-Reviewer-Credential": f"cred-{rid}"}


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_for_tests()
    with db.write_txn() as conn:
        for table in (
            "papers", "reviewers", "published",
            "assignment_decisions", "reviewer_recusals", "submitted_reviews",
            "review_corrections", "assignment_locks", "paper_guarantee_levels",
            "review_deadlines", "paper_deletions", "paper_withdrawals",
            "feedback_snapshots", "feedback_objections",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'revision'")
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def frozen_clock():
    """固定 _utcnow, 返回 [时刻持有器] 供测试推进; 结束后恢复真实时钟。"""
    base = datetime(2026, 10, 4, 9, 0, 0, tzinfo=timezone.utc)
    holder = {"now": base}
    original = main._utcnow
    main._utcnow = lambda: holder["now"]
    try:
        yield holder
    finally:
        main._utcnow = original


def add_reviewer(client, rid, institution, capacity=5, topics=None):
    return client.post(
        "/reviewers",
        headers=ORG,
        json={
            "reviewer_id": rid,
            "credential": f"cred-{rid}",
            "topics": topics if topics is not None else ["AI"],
            "institution": institution,
            "capacity": capacity,
            "avoid_papers": [],
        },
    )


def add_paper(client, pid, manuscript=None, institutions=None):
    return client.post(
        "/papers",
        headers=ORG,
        json={
            "paper_id": pid,
            "manuscript": manuscript or f"manuscript-of-{pid}",
            "topics": ["AI"],
            "institutions": institutions or [f"Author-{pid}"],
        },
    )


def publish(client):
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 200, r.text
    return r.json()


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def set_deadline(client, rev, serial, deadline_at):
    return client.post(
        "/review-deadline",
        headers=ORG,
        json={"base_revision": rev, "base_serial": serial, "deadline_at": deadline_at},
    )


def decide(client, rid, pid, decision="confirm"):
    return client.post(
        f"/reviewer/assignments/{pid}/decision",
        headers=reviewer_headers(rid),
        json={"paper_id": pid, "decision": decision},
    )


def expire_current_deadline(client, frozen_clock, hours=1):
    """为当前发布版设置 1 小时后的截止并把时钟拨到截止之后。"""
    meta = client.get("/meta", headers=ORG).json()
    serial = client.get("/assignment", headers=ORG).json()["serial"]
    r = set_deadline(
        client, meta["revision"], serial, iso(frozen_clock["now"] + timedelta(hours=hours))
    )
    assert r.status_code == 200, r.text
    frozen_clock["now"] += timedelta(hours=hours + 1)


# ------------------------------------------------------------ 截止状态汇总

def test_deadline_view_excludes_deleted_frozen_slots_from_summary(client, frozen_clock):
    for rid, inst in [("R1", "I1"), ("R2", "I2"), ("R3", "I3"), ("R4", "I4")]:
        add_reviewer(client, rid, inst)
    add_paper(client, "P1")
    serial = publish(client)["serial"]
    plan = client.get("/assignment", headers=ORG).json()["plan"]["P1"]
    # 两槽位均确认但未交评语 -> 若无删除, 截止后必为逾期
    for rid in plan:
        assert decide(client, rid, "P1").status_code == 200
    expire_current_deadline(client, frozen_clock)

    assert client.delete("/papers/P1", headers=ORG).status_code == 200
    add_paper(client, "P1", manuscript="重录稿")

    view = client.get("/review-deadline", headers=ORG).json()
    # 冻结槽位不计入当前截止状态汇总: 无逾期
    assert view["summary"]["overdue"] == 0
    assert sum(view["summary"].values()) == 0
    assert view["deleted_slots"] == 2
    frozen = [s for s in view["slots"] if s["paper_id"] == "P1"]
    assert {s["reviewer_id"] for s in frozen} == set(plan)
    assert all(s["deleted"] is True for s in frozen)
    assert all(s["state"] == "confirmed_unsubmitted" for s in frozen)  # 删除前历史状态
    assert all(s["withdrawn"] is False for s in frozen)


def test_pure_delete_without_reentry_slots_also_not_overdue(client, frozen_clock):
    """纯删除 (资料行不存在): 冻结槽位仍在当前发布版视图, 但同样不判逾期。"""
    add_reviewer(client, "R1", "I1")
    add_reviewer(client, "R2", "I2")
    add_paper(client, "P1")
    publish(client)
    plan = client.get("/assignment", headers=ORG).json()["plan"]["P1"]
    for rid in plan:
        decide(client, rid, "P1")
    expire_current_deadline(client, frozen_clock)
    client.delete("/papers/P1", headers=ORG)

    view = client.get("/review-deadline", headers=ORG).json()
    assert view["summary"]["overdue"] == 0
    assert view["deleted_slots"] == 2
    assert all(s["deleted"] and s["state"] != "overdue" for s in view["slots"])


# ------------------------------------------------------------ 补位: 冻结槽位不判逾期

def test_backfill_does_not_bar_original_reviewers_for_reentered_paper(client, frozen_clock):
    """原缺陷场景: 两名原评审人截止后因删除冻结被错误排除, 导致无法补齐。"""
    # R1/R2/R3 同机构 A; R4 机构 B; 作者机构 X 与全部不同, 均擅长 AI
    for rid, inst in [("R1", "A"), ("R2", "A"), ("R3", "A"), ("R4", "B")]:
        add_reviewer(client, rid, inst)
    add_paper(client, "P1", institutions=["X"])
    serial = publish(client)["serial"]
    pair = client.get("/assignment", headers=ORG).json()["plan"]["P1"]
    assert set(pair) == {"R1", "R4"}  # 字典序最优: R1 + 唯一异机构者 R4
    for rid in pair:
        decide(client, rid, "P1")
    expire_current_deadline(client, frozen_clock)

    client.delete("/papers/P1", headers=ORG)
    add_paper(client, "P1", manuscript="重录稿", institutions=["X"])

    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    # 旧截止不约束重录稿: 无逾期明细, 原评审人不被 review_overdue 排除, 方案完整
    assert dry["feasible"] is True, dry
    assert dry["overdue_slots"] == []
    frozen = {(s["paper_id"], s["reviewer_id"]) for s in dry["deleted_frozen_slots"]}
    assert frozen == {("P1", "R1"), ("P1", "R4")}
    excluded = {
        e["reviewer_id"]: e["reason"]
        for e in dry["papers"].get("P1", {}).get("excluded", [])
    }
    assert "review_overdue" not in excluded.values()
    # 重录稿可再次分给原评审人 (求解硬约束/字典序不变 -> 仍为 R1+R4)
    assert dry["plan"]["P1"] == ["R1", "R4"]
    assert dry["fixed_problems"] == {}


def test_backfill_publish_releases_frozen_slots_and_requires_reconfirm(client, frozen_clock):
    for rid, inst in [("R1", "A"), ("R2", "A"), ("R3", "A"), ("R4", "B")]:
        add_reviewer(client, rid, inst)
    add_paper(client, "P1", institutions=["X"])
    serial = publish(client)["serial"]
    pair = client.get("/assignment", headers=ORG).json()["plan"]["P1"]
    for rid in pair:
        decide(client, rid, "P1")
    expire_current_deadline(client, frozen_clock)
    client.delete("/papers/P1", headers=ORG)
    add_paper(client, "P1", manuscript="重录稿", institutions=["X"])

    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"] is True
    bp = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert bp.status_code == 200, bp.text
    body = bp.json()
    # 预演与发布一致
    assert body["plan"] == dry["plan"]
    assert body["serial"] == serial + 1
    # 旧确认不沿用: 两个冻结槽位全部按删除释放, 不产生逾期释放
    assert body["released_deleted_confirmations"] == 2
    assert body["released_overdue_confirmations"] == 0
    assert body["carried_confirmations"] == 0
    assert body["overdue_slots"] == []
    # 新序号槽位为 pending: 未重新确认直接交评语 409, 重新确认后可提交
    for rid in pair:
        states = client.get("/reviewer/assignments", headers=reviewer_headers(rid)).json()["states"]
        assert states["P1"] == "pending"
        late = client.post(
            "/reviewer/assignments/P1/review",
            headers=reviewer_headers(rid),
            json={"paper_id": "P1", "serial": body["serial"], "score": 4, "comment": "未确认先交"},
        )
        assert late.status_code == 409
        assert decide(client, rid, "P1").status_code == 200
        ok = client.post(
            "/reviewer/assignments/P1/review",
            headers=reviewer_headers(rid),
            json={"paper_id": "P1", "serial": body["serial"], "score": 5, "comment": f"新序号评语-{rid}"},
        )
        assert ok.status_code == 200, ok.text


def test_stale_backfill_publish_rejected_after_further_change(client, frozen_clock):
    """版本不符 (补位预演后又有资料变更/发布) -> 409 且发布版不变。"""
    for rid, inst in [("R1", "A"), ("R2", "A"), ("R3", "A"), ("R4", "B")]:
        add_reviewer(client, rid, inst)
    add_paper(client, "P1", institutions=["X"])
    serial = publish(client)["serial"]
    pair = client.get("/assignment", headers=ORG).json()["plan"]["P1"]
    for rid in pair:
        decide(client, rid, "P1")
    expire_current_deadline(client, frozen_clock)
    client.delete("/papers/P1", headers=ORG)
    add_paper(client, "P1", manuscript="重录稿", institutions=["X"])

    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    # 预演后再发生一次资料变更 (新增论文), 预演所据版本过期
    add_paper(client, "P9", institutions=["Y"])
    bp = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert bp.status_code == 409
    # 当前发布版/序号不变
    assert client.get("/assignment", headers=ORG).json()["serial"] == serial


# ------------------------------------------------------------ 混合: 冻结稿 + 真实逾期稿

def test_real_overdue_other_paper_still_released_and_barred(client, frozen_clock):
    """P1 删除重录 (冻结, 不判逾期); P2 真实逾期 (释放 + 禁止原槽位重分)。"""
    for rid, inst in [("R1", "U1"), ("R2", "U2"), ("R3", "U3"),
                      ("R4", "U4"), ("R5", "U5")]:
        add_reviewer(client, rid, inst)
    add_paper(client, "P1", institutions=["AUTH-1"])
    add_paper(client, "P2", institutions=["AUTH-2"])
    serial = publish(client)["serial"]
    plan = client.get("/assignment", headers=ORG).json()["plan"]
    p1_pair, p2_pair = plan["P1"], plan["P2"]
    for pid, pair in (("P1", p1_pair), ("P2", p2_pair)):
        for rid in pair:
            decide(client, rid, pid)
    expire_current_deadline(client, frozen_clock)

    client.delete("/papers/P1", headers=ORG)
    add_paper(client, "P1", manuscript="重录稿", institutions=["AUTH-1"])

    view = client.get("/review-deadline", headers=ORG).json()
    # 汇总只含 P2 的两个真实逾期槽位
    assert view["summary"]["overdue"] == 2
    assert view["deleted_slots"] == 2
    p1_states = [s["state"] for s in view["slots"] if s["paper_id"] == "P1"]
    p2_states = [s["state"] for s in view["slots"] if s["paper_id"] == "P2"]
    assert set(p1_states) == {"confirmed_unsubmitted"}
    assert set(p2_states) == {"overdue"}

    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"] is True, dry
    assert {s["paper_id"] for s in dry["overdue_slots"]} == {"P2"}
    assert {(s["paper_id"], s["reviewer_id"]) for s in dry["deleted_frozen_slots"]} == {
        ("P1", rid) for rid in p1_pair
    }
    # P2 原评审人被固定位置释放且禁止重分
    p2_problems = {
        p["reviewer_id"]: p["reason"] for p in dry["fixed_problems"].get("P2", [])
    }
    assert all(p2_problems.get(rid) == "fixed_review_overdue" for rid in p2_pair)
    p2_excluded = {
        e["reviewer_id"]: e["reason"] for e in dry["papers"]["P2"]["excluded"]
    }
    assert all(p2_excluded.get(rid) == "review_overdue" for rid in p2_pair)
    assert all(rid not in dry["plan"]["P2"] for rid in p2_pair)
    # P1 原评审人不受旧截止影响
    p1_excluded = {
        e["reviewer_id"]: e["reason"]
        for e in dry["papers"].get("P1", {}).get("excluded", [])
    }
    assert "review_overdue" not in p1_excluded.values()

    bp = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert bp.status_code == 200, bp.text
    body = bp.json()
    assert body["plan"] == dry["plan"]  # 预演与发布一致
    assert body["released_overdue_confirmations"] == 2
    assert body["released_deleted_confirmations"] == 2
    assert all(rid not in body["plan"]["P2"] for rid in p2_pair)
    # 新序号无旧截止约束: P2 新槽位 pending, 可重新确认 (不因旧截止被拒)
    new_rid = body["plan"]["P2"][0]
    frozen_clock["now"] += timedelta(hours=10)  # 已远超旧截止时刻
    r = decide(client, new_rid, "P2")
    assert r.status_code == 200 and r.json()["serial"] == serial + 1


def test_old_deadline_dropped_after_backfill_republish_for_reentered_paper(client, frozen_clock):
    """补位发布推进序号后旧版截止不再约束新版本 (含删除重录稿)。"""
    for rid, inst in [("R1", "A"), ("R2", "A"), ("R3", "A"), ("R4", "B")]:
        add_reviewer(client, rid, inst)
    add_paper(client, "P1", institutions=["X"])
    publish(client)
    pair = client.get("/assignment", headers=ORG).json()["plan"]["P1"]
    for rid in pair:
        decide(client, rid, "P1")
    expire_current_deadline(client, frozen_clock)
    client.delete("/papers/P1", headers=ORG)
    add_paper(client, "P1", manuscript="重录稿", institutions=["X"])

    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    bp = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    ).json()
    view = client.get("/review-deadline", headers=ORG).json()
    assert view["review_deadline"] is None
    assert view["deadline_expired"] is False
    assert view["summary"]["overdue"] == 0
    assert view["summary"]["unconfirmed"] == 2  # 新序号槽位全部 pending
    assert view["deleted_slots"] == 0
    # 时钟仍在旧截止之后, 新序号确认不受旧截止阻挡
    r = decide(client, bp["plan"]["P1"][0], "P1")
    assert r.status_code == 200
