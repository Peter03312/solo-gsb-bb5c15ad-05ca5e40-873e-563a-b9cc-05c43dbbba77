"""本轮修复测试: 删除凭据冻结序号 与 统一评审截止判断的交叉处理。

缺陷背景: 论文删除后当前发布版保留旧槽位; 该版统一评审截止已过的情况下,
删除并以同编号重录后尝试补位, 旧槽位仍被 _overdue_pairs 判为逾期:
  - 旧槽位计入 GET /review-deadline 的逾期汇总;
  - 补位预演/发布把原评审人按 review_overdue 对该稿排除 (barred_pairs),
    求解时两名合格评审人 (原评审人) 同时被排除导致补不齐 (feasible=false)。

修复契约:
- 删除凭据冻结当前发布序号的槽位 (含同编号重录但未重新发布) 不参与逾期判定:
  不计入 GET /review-deadline 当前截止状态汇总, 不进入补位 overdue_slots,
  不因旧截止阻止重录稿再次分给原评审人;
- 旧确认/旧评语仍仅供会务方追溯 (deleted_frozen 标记 + 删除凭据);
  补位发布推进序号后一律 pending, 须重新确认并重新提交评语
  (deleted_frozen_slots / released_deleted_confirmations 语义不变);
- 其他论文的真实逾期照常: 释放其已确认槽位 (fixed_review_overdue) 并禁止
  该稿重新分给原逾期评审人 (review_overdue), 同一补位事务内与删除冻结并存;
- 预演与发布结果一致; 版本不符或无完整方案时拒绝且不改发布版;
- 纯删除 (未重录) 的冻结槽位同样不计逾期汇总;
- 重新发布推进序号后旧截止不再生效; 多轮删除/重录/发布按冻结序号判定。
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

_TMPDIR = tempfile.mkdtemp(prefix="review-deadline-delete-test-")
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
    base = datetime(2026, 10, 4, 9, 0, 0, tzinfo=timezone.utc)
    holder = {"now": base}
    original = main._utcnow
    main._utcnow = lambda: holder["now"]
    try:
        yield holder
    finally:
        main._utcnow = original


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def add_reviewer(client, rid, institution=None, capacity=3, topics=None):
    return client.post(
        "/reviewers",
        headers=ORG,
        json={
            "reviewer_id": rid,
            "credential": f"cred-{rid}",
            "topics": topics if topics is not None else ["AI"],
            "institution": institution or f"Inst-{rid}",
            "capacity": capacity,
            "avoid_papers": [],
        },
    )


def add_paper(client, pid, manuscript=None, topics=None, institutions=None):
    return client.post(
        "/papers",
        headers=ORG,
        json={
            "paper_id": pid,
            "manuscript": manuscript or f"manuscript-of-{pid}",
            "topics": topics if topics is not None else ["AI"],
            "institutions": institutions or [f"Author-{pid}"],
        },
    )


def current_meta(client):
    return client.get("/meta", headers=ORG).json()


def normal_publish(client):
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 200, r.text
    return r.json()


def decide(client, rid, pid, decision="confirm", reason=None):
    body = {"paper_id": pid, "decision": decision}
    if reason is not None:
        body["reason"] = reason
    return client.post(
        f"/reviewer/assignments/{pid}/decision",
        headers=reviewer_headers(rid),
        json=body,
    )


def submit_review(client, rid, pid, serial, score=4, comment=None):
    return client.post(
        f"/reviewer/assignments/{pid}/review",
        headers=reviewer_headers(rid),
        json={
            "paper_id": pid, "serial": serial, "score": score,
            "comment": comment or f"评语-{rid}-{pid}",
        },
    )


def set_deadline(client, deadline_at):
    meta = current_meta(client)
    r = client.post(
        "/review-deadline",
        headers=ORG,
        json={
            "base_revision": meta["revision"], "base_serial": meta["published"]["serial"],
            "deadline_at": deadline_at,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def backfill_dry(client):
    return client.post("/assignment/backfill/dry-run", headers=ORG).json()


def backfill_publish(client, dry):
    return client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )


# --------------------------------------------------------------- 核心缺陷回归

def test_deleted_frozen_slots_not_counted_as_overdue(client, frozen_clock):
    """截止后删除并同编号重录: 冻结槽位不计逾期汇总, 截止视图逐槽位标记。"""
    add_reviewer(client, "R1")
    add_reviewer(client, "R2")
    add_paper(client, "P1")
    serial = normal_publish(client)["serial"]
    assert decide(client, "R1", "P1").status_code == 200
    assert decide(client, "R2", "P1").status_code == 200
    set_deadline(client, iso(frozen_clock["now"] + timedelta(hours=1)))
    frozen_clock["now"] += timedelta(hours=2)

    assert client.delete("/papers/P1", headers=ORG).status_code == 200
    add_paper(client, "P1", manuscript="重录稿")

    view = client.get("/review-deadline", headers=ORG).json()
    assert view["deadline_expired"] is True
    # 冻结槽位不作为任何当前状态计数 (尤其不得计入 overdue)
    assert view["summary"] == {
        "unconfirmed": 0, "confirmed_unsubmitted": 0, "submitted": 0,
        "recused": 0, "overdue": 0,
    }
    assert view["deleted_frozen_slots"] == 2
    frozen = [s for s in view["slots"] if s["deleted_frozen"]]
    assert {(s["paper_id"], s["reviewer_id"]) for s in frozen} == {
        ("P1", "R1"), ("P1", "R2"),
    }
    assert all(s["state"] != "overdue" for s in frozen)

    # /assignment 会务方追溯视图同形标记删除冻结 (重新发布后消失)
    assignment = client.get("/assignment", headers=ORG).json()
    assert assignment["deleted_frozen_papers"] == ["P1"]
    assert assignment["papers"]["P1"]["deleted_frozen"] is True
    assert all(s["deleted_frozen"] for s in assignment["papers"]["P1"]["slots"])


def test_pure_deleted_slots_not_counted_as_overdue(client, frozen_clock):
    """纯删除 (未重录) 的冻结槽位同样不计逾期汇总, 但保留 deleted_frozen 标记。"""
    add_reviewer(client, "R1")
    add_reviewer(client, "R2")
    add_paper(client, "P1")
    normal_publish(client)
    decide(client, "R1", "P1")
    decide(client, "R2", "P1")
    set_deadline(client, iso(frozen_clock["now"] + timedelta(hours=1)))
    frozen_clock["now"] += timedelta(hours=2)

    client.delete("/papers/P1", headers=ORG)  # 不重录

    view = client.get("/review-deadline", headers=ORG).json()
    assert view["summary"]["overdue"] == 0
    assert view["deleted_frozen_slots"] == 2
    assert all(s["deleted_frozen"] for s in view["slots"])


def test_backfill_after_deadline_delete_reentry_can_reassign_original_reviewers(
    client, frozen_clock
):
    """缺陷主场景: 截止后删除同编号重录再补位, 原评审人不被 review_overdue 排除,
    两名原评审人可重新获稿; 旧确认按删除规则释放, 须重新确认。"""
    add_reviewer(client, "R1")
    add_reviewer(client, "R2")
    add_reviewer(client, "R3")
    add_paper(client, "P1")
    serial = normal_publish(client)["serial"]
    assert decide(client, "R1", "P1").status_code == 200
    assert decide(client, "R2", "P1").status_code == 200
    set_deadline(client, iso(frozen_clock["now"] + timedelta(hours=1)))
    frozen_clock["now"] += timedelta(hours=2)

    client.delete("/papers/P1", headers=ORG)
    add_paper(client, "P1", manuscript="重录稿")

    dry = backfill_dry(client)
    assert dry["feasible"] is True, dry
    assert dry["unassigned"] == []
    # 无逾期明细: 旧截止随删除冻结的旧序号失效
    assert dry["overdue_slots"] == []
    # 删除冻结的旧确认槽位仍列明 (与截止无关的既有删除契约)
    frozen = {(s["paper_id"], s["reviewer_id"]) for s in dry["deleted_frozen_slots"]}
    assert frozen == {("P1", "R1"), ("P1", "R2")}
    # 原评审人不得被标 review_overdue, 且可以重新获稿
    excluded = {(e["reviewer_id"], e["reason"]) for e in dry["papers"]["P1"]["excluded"]}
    assert not any(reason == "review_overdue" for _, reason in excluded)
    assert set(dry["plan"]["P1"]) == {"R1", "R2"}
    # fixed 中不含删除冻结槽位
    assert dry["fixed"].get("P1", []) == []
    assert dry["fixed_problems"].get("P1", []) == []

    bp = backfill_publish(client, dry)
    assert bp.status_code == 200, bp.text
    body = bp.json()
    new_serial = body["serial"]
    assert new_serial == serial + 1
    assert body["overdue_slots"] == []
    assert body["released_overdue_confirmations"] == 0
    # 旧确认不沿用 (删除规则), 新序号全部 pending
    assert body["released_deleted_confirmations"] == 2
    assert body["carried_confirmations"] == 0
    assert body["carried_reviews"] == 0
    states = client.get(
        "/reviewer/assignments", headers=reviewer_headers("R1")
    ).json()["states"]
    assert states["P1"] == "pending"
    # 未确认直接交评语仍 409; 重新确认后可按新序号提交
    r = submit_review(client, "R1", "P1", new_serial, comment="新序号评语")
    assert r.status_code == 409
    assert decide(client, "R1", "P1").status_code == 200
    assert submit_review(client, "R1", "P1", new_serial, comment="新序号评语").status_code == 200
    # 推进序号后删除冻结标记消失 (当前方案为新序号槽位)
    assignment = client.get("/assignment", headers=ORG).json()
    assert assignment["deleted_frozen_papers"] == []
    assert assignment["papers"]["P1"]["deleted_frozen"] is False


def test_dry_run_and_publish_agree(client, frozen_clock):
    """预演与发布结果一致: 发布方案与预演 plan 相同。"""
    add_reviewer(client, "R1")
    add_reviewer(client, "R2")
    add_paper(client, "P1")
    normal_publish(client)
    decide(client, "R1", "P1")
    decide(client, "R2", "P1")
    set_deadline(client, iso(frozen_clock["now"] + timedelta(hours=1)))
    frozen_clock["now"] += timedelta(hours=2)
    client.delete("/papers/P1", headers=ORG)
    add_paper(client, "P1", manuscript="重录稿")

    dry = backfill_dry(client)
    bp = backfill_publish(client, dry)
    assert bp.status_code == 200, bp.text
    assert bp.json()["plan"] == dry["plan"]


def test_backfill_publish_version_mismatch_rejected_409_without_change(
    client, frozen_clock
):
    """删除/重录后版本不符的补位发布 409 且不改变当前发布版。"""
    add_reviewer(client, "R1")
    add_reviewer(client, "R2")
    add_paper(client, "P1")
    serial = normal_publish(client)["serial"]
    decide(client, "R1", "P1")
    set_deadline(client, iso(frozen_clock["now"] + timedelta(hours=1)))
    frozen_clock["now"] += timedelta(hours=2)
    client.delete("/papers/P1", headers=ORG)
    add_paper(client, "P1", manuscript="重录稿")

    dry = backfill_dry(client)
    before = client.get("/assignment", headers=ORG).json()
    bp = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": dry["revision"] - 1, "base_serial": dry["serial"]},
    )
    assert bp.status_code == 409
    after = client.get("/assignment", headers=ORG).json()
    assert after["serial"] == serial
    assert after["plan"] == before["plan"]
    # 用过期版本拒绝后, 当前版本仍可正常补位发布
    assert backfill_publish(client, dry).status_code == 200


# ----------------------------------------------- 与其他论文真实逾期并存

def test_real_overdue_of_other_paper_still_releases_and_bars(client, frozen_clock):
    """同一次补位中: 删除重录稿不被判逾期; 另一篇真实逾期稿照常释放并禁止重分。

    P1: 截止后删除并同编号重录 -> 原评审人 R1/R2 可重新获稿 (无 review_overdue)。
    P2: R3 已确认未交 (真实逾期) -> 槽位释放并禁止 P2 再分给 R3;
        R4 未确认 (亦逾期, 本就未固定); P2 由其余合格评审人补齐。
    """
    add_reviewer(client, "R1", institution="Inst-1")
    add_reviewer(client, "R2", institution="Inst-2")
    add_reviewer(client, "R3", institution="Inst-3")
    add_reviewer(client, "R4", institution="Inst-4")
    add_reviewer(client, "R5", institution="Inst-5")
    add_paper(client, "P1", institutions=["Author-1"])
    add_paper(client, "P2", institutions=["Author-2"])
    serial = normal_publish(client)["serial"]
    plan = client.get("/assignment", headers=ORG).json()["plan"]
    r1, r2 = plan["P1"]
    r3, r4 = plan["P2"]

    # P1 两人确认未交; P2 的 R3 确认未交, R4 保持 pending
    decide(client, r1, "P1")
    decide(client, r2, "P1")
    decide(client, r3, "P2")

    set_deadline(client, iso(frozen_clock["now"] + timedelta(hours=1)))
    frozen_clock["now"] += timedelta(hours=2)

    # 真实逾期在删除前即可见: P2 两槽位逾期
    view = client.get("/review-deadline", headers=ORG).json()
    assert view["summary"]["overdue"] == 4  # P1x2 + P2x2

    client.delete("/papers/P1", headers=ORG)
    add_paper(client, "P1", manuscript="重录稿", institutions=["Author-1"])

    view = client.get("/review-deadline", headers=ORG).json()
    # P1 冻结槽位移出汇总; P2 的两个真实逾期槽位照常计入
    assert view["summary"]["overdue"] == 2
    assert view["deleted_frozen_slots"] == 2

    dry = backfill_dry(client)
    assert dry["feasible"] is True, dry
    overdue = {(s["paper_id"], s["reviewer_id"]) for s in dry["overdue_slots"]}
    assert overdue == {("P2", r3), ("P2", r4)}
    # P1 无 review_overdue 排除; P2 对 R3/R4 标 review_overdue
    p1_excluded = {e["reason"] for e in dry["papers"]["P1"]["excluded"]}
    assert "review_overdue" not in p1_excluded
    p2_excluded = {e["reviewer_id"]: e["reason"] for e in dry["papers"]["P2"]["excluded"]}
    assert p2_excluded.get(r3) == "review_overdue"
    assert p2_excluded.get(r4) == "review_overdue"
    # P2 新方案不含被逾期排除的原槽位评审人
    assert set(dry["plan"]["P2"]).isdisjoint({r3, r4})
    # P1 可重新分给原评审人 (旧截止不阻止; 经容量比例全局择优后原两人至少一人在)
    assert set(dry["plan"]["P1"]) & {r1, r2}
    # fixed_problems: P2 的 R3 已确认因真实逾期释放
    p2_problems = {(p["reviewer_id"], p["reason"]) for p in dry["fixed_problems"].get("P2", [])}
    assert (r3, "fixed_review_overdue") in p2_problems
    assert dry["fixed_problems"].get("P1", []) == []

    bp = backfill_publish(client, dry)
    assert bp.status_code == 200, bp.text
    body = bp.json()
    assert body["serial"] == serial + 1
    assert body["released_overdue_confirmations"] == 1  # 仅 P2 的 R3 是已确认逾期槽位
    assert body["released_deleted_confirmations"] == 2  # P1 两个删除冻结确认
    assert body["plan"]["P1"] == dry["plan"]["P1"]
    assert body["plan"]["P2"] == dry["plan"]["P2"]


def test_overdue_bar_scoped_to_the_overdue_paper_only(client, frozen_clock):
    """真实逾期只对该论文排除该评审人, 不影响其在补位中获得其他论文。"""
    # R3 与 P1/P2 作者均不同机构且擅长 AI, 容量充足
    add_reviewer(client, "R1", institution="Inst-1")
    add_reviewer(client, "R2", institution="Inst-2")
    add_reviewer(client, "R3", institution="Inst-3", capacity=5)
    add_reviewer(client, "R4", institution="Inst-4", capacity=5)
    add_paper(client, "P1", institutions=["Author-1"])
    add_paper(client, "P2", institutions=["Author-2"])
    serial = normal_publish(client)["serial"]
    plan = client.get("/assignment", headers=ORG).json()["plan"]
    # 找到分配到 P1 的 R3 (若无则调整: 直接对 P1 槽位评审人构造)
    p1_rids = plan["P1"]
    # 让 P1 两名评审人都确认未交 -> 截止后 P1 真实逾期
    for rid in p1_rids:
        decide(client, rid, "P1")
    set_deadline(client, iso(frozen_clock["now"] + timedelta(hours=1)))
    frozen_clock["now"] += timedelta(hours=2)

    dry = backfill_dry(client)
    assert dry["feasible"] is True, dry
    # P1 逾期评审人仅对 P1 排除: 检查他们在 P2 的 excluded 中不带 review_overdue
    for rid in p1_rids:
        p2_excluded = {e["reviewer_id"]: e["reason"] for e in dry["papers"]["P2"]["excluded"]}
        assert p2_excluded.get(rid) != "review_overdue"
    # 新方案 P1 不含原逾期评审人
    assert set(dry["plan"]["P1"]).isdisjoint(p1_rids)


# ----------------------------------------------- 删除/重录 + 旧截止的生命周期

def test_old_deadline_does_not_restrain_new_serial_after_backfill_publish(
    client, frozen_clock
):
    """补位发布推进序号后: 新版无截止, 评审人确认恢复 (无逾期 409)。"""
    add_reviewer(client, "R1")
    add_reviewer(client, "R2")
    add_paper(client, "P1")
    serial = normal_publish(client)["serial"]
    decide(client, "R1", "P1")
    decide(client, "R2", "P1")
    set_deadline(client, iso(frozen_clock["now"] + timedelta(hours=1)))
    frozen_clock["now"] += timedelta(hours=2)
    client.delete("/papers/P1", headers=ORG)
    add_paper(client, "P1", manuscript="重录稿")

    dry = backfill_dry(client)
    assert backfill_publish(client, dry).status_code == 200

    # 新序号初始无截止
    view = client.get("/review-deadline", headers=ORG).json()
    assert view["review_deadline"] is None
    assert view["deadline_expired"] is False
    assert view["summary"]["overdue"] == 0
    # 旧截止已过也不阻止新序号确认
    r = decide(client, "R1", "P1")
    assert r.status_code == 200, r.text
    assert r.json()["serial"] == serial + 1


def test_multiple_delete_reentry_cycles_freeze_respective_serials(client, frozen_clock):
    """多轮 删除 -> 重录 -> 补位发布: 每轮删除冻结当时序号; 旧序号槽位不复活,
    新序号不受旧截止影响。"""
    add_reviewer(client, "R1")
    add_reviewer(client, "R2")
    add_paper(client, "P1")
    serial1 = normal_publish(client)["serial"]
    decide(client, "R1", "P1")
    decide(client, "R2", "P1")
    set_deadline(client, iso(frozen_clock["now"] + timedelta(hours=1)))
    frozen_clock["now"] += timedelta(hours=2)

    # 第一轮: 截止后删除重录补位
    client.delete("/papers/P1", headers=ORG)
    add_paper(client, "P1", manuscript="重录稿-1")
    dry = backfill_dry(client)
    assert dry["feasible"] and dry["overdue_slots"] == []
    bp = backfill_publish(client, dry)
    assert bp.status_code == 200
    serial2 = bp.json()["serial"]
    assert bp.json()["released_deleted_confirmations"] == 2
    assert decide(client, "R1", "P1").status_code == 200
    assert submit_review(client, "R1", "P1", serial2, comment="第二轮评语").status_code == 200

    # 第二轮: 再次删除 (不设新截止; 旧截止行属于 serial1)
    client.delete("/papers/P1", headers=ORG)
    add_paper(client, "P1", manuscript="重录稿-2")
    dry2 = backfill_dry(client)
    assert dry2["feasible"] is True, dry2
    assert dry2["overdue_slots"] == []
    frozen2 = {(s["paper_id"], s["reviewer_id"]) for s in dry2["deleted_frozen_slots"]}
    # serial2 仅 R1 有确认 (R2 pending), 故只有 R1 进入删除冻结确认明细
    assert frozen2 == {("P1", "R1")}
    bp2 = backfill_publish(client, dry2)
    assert bp2.status_code == 200, bp2.text
    assert bp2.json()["released_deleted_confirmations"] == 1
    serial3 = bp2.json()["serial"]
    assert serial3 == serial2 + 1
    # 新序号两人均 pending, 旧评语未复制
    states = client.get(
        "/reviewer/assignments", headers=reviewer_headers("R1")
    ).json()["states"]
    assert states["P1"] == "pending"
    assert submit_review(client, "R1", "P1", serial3, comment="第三轮").status_code == 409
    assert decide(client, "R1", "P1").status_code == 200
    assert submit_review(client, "R1", "P1", serial3, comment="第三轮").status_code == 200

    deletions = client.get("/papers/P1/deletions", headers=ORG).json()["deletions"]
    assert [d["published_serial"] for d in deletions] == [serial1, serial2]


def test_withdrawn_slot_and_deleted_frozen_slot_both_excluded_from_overdue(
    client, frozen_clock
):
    """撤回稿与删除冻结稿槽位同时存在时, 均不计逾期汇总, 其余真实槽位照常。"""
    add_reviewer(client, "R1", institution="Inst-1")
    add_reviewer(client, "R2", institution="Inst-2")
    add_reviewer(client, "R3", institution="Inst-3", capacity=5)
    add_reviewer(client, "R4", institution="Inst-4", capacity=5)
    add_reviewer(client, "R5", institution="Inst-5", capacity=5)
    add_paper(client, "P1", institutions=["Author-1"])
    add_paper(client, "P2", institutions=["Author-2"])
    add_paper(client, "P3", institutions=["Author-3"])
    normal_publish(client)
    for pid in ("P1", "P2", "P3"):
        plan = client.get("/assignment", headers=ORG).json()["plan"]
        decide(client, plan[pid][0], pid)
        decide(client, plan[pid][1], pid)
    set_deadline(client, iso(frozen_clock["now"] + timedelta(hours=1)))
    frozen_clock["now"] += timedelta(hours=2)

    # 撤回 P2, 删除 P3 (不重录); P1 保持真实逾期
    meta = current_meta(client)
    wr = client.post(
        "/papers/P2/withdrawal",
        headers=ORG,
        json={"paper_id": "P2", "base_revision": meta["revision"], "reason": "一稿多投"},
    )
    assert wr.status_code == 200, wr.text
    assert client.delete("/papers/P3", headers=ORG).status_code == 200

    view = client.get("/review-deadline", headers=ORG).json()
    # 仅 P1 的两个槽位计入逾期; P2 撤回/P3 删除冻结均不计数
    assert view["summary"]["overdue"] == 2
    assert view["withdrawn_slots"] == 2
    assert view["deleted_frozen_slots"] == 2
    by_paper = {}
    for s in view["slots"]:
        by_paper.setdefault(s["paper_id"], []).append(s)
    assert all(s["state"] == "overdue" for s in by_paper["P1"])
    assert all(s["withdrawn"] for s in by_paper["P2"])
    assert all(s["deleted_frozen"] for s in by_paper["P3"])
    assert all(s["state"] != "overdue" for s in by_paper["P3"])


# ----------------------------------------------- 无完整方案: 预演/发布拒绝不改发布版

def test_backfill_publish_422_when_infeasible_and_published_unchanged(client, frozen_clock):
    """删除重录稿后若客观上无合格评审人对, 预演 feasible=false, 发布 422 不改发布版。

    通过机构归并把除原两人外的可能性收窄: 仅两名合格评审人时把两人归为同组,
    重录稿无合法对; 此时不得因旧截止额外排除 (缺陷路径), 但客观约束仍应拒绝。
    """
    add_reviewer(client, "R1", institution="Inst-A")
    add_reviewer(client, "R2", institution="Inst-B")
    add_paper(client, "P1", institutions=["Author-1"])
    serial = normal_publish(client)["serial"]
    decide(client, "R1", "P1")
    decide(client, "R2", "P1")
    set_deadline(client, iso(frozen_clock["now"] + timedelta(hours=1)))
    frozen_clock["now"] += timedelta(hours=2)
    client.delete("/papers/P1", headers=ORG)
    add_paper(client, "P1", manuscript="重录稿", institutions=["Author-1"])
    # 归并两名合格评审人为同机构 -> 无合法对 (客观不可行, 与截止无关)
    meta = current_meta(client)
    mr = client.post(
        "/institutions/merge-groups",
        headers=ORG,
        json={"name_a": "Inst-A", "name_b": "Inst-B", "base_revision": meta["revision"]},
    )
    assert mr.status_code == 200, mr.text

    dry = backfill_dry(client)
    assert dry["feasible"] is False
    assert dry["unassigned"] == ["P1"]
    before = client.get("/assignment", headers=ORG).json()
    bp = backfill_publish(client, backfill_dry(client))
    assert bp.status_code == 422, bp.text
    after = client.get("/assignment", headers=ORG).json()
    assert after["serial"] == serial
    assert after["plan"] == before["plan"]
