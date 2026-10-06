#!/usr/bin/env bash
# 本轮用法端到端示例: 删除凭据冻结序号与统一评审截止的交叉处理
#
#   场景: 两篇论文 DDF1 / DDF2 发布后, 各两名评审人均"确认但未交评语";
#   会务方设置约 2 分钟后的统一评审截止并等待到点 -> 四个槽位全部逾期。
#   此时:
#     * 删除 DDF1 并以同编号重录: 删除凭据冻结旧序号槽位。旧截止属于被冻结的
#       旧序号——冻结槽位不计入当前截止状态汇总 (summary), 逐槽位明细带
#       deleted 标记并保留删除前历史状态, 但不标 overdue;
#     * 补位预演: DDF1 重录稿可再次分给原评审人 (不因旧截止判 review_overdue),
#       DDF2 的真实逾期照常释放 (fixed_review_overdue) 且原评审人被禁止在该稿
#       重分 (review_overdue); overdue_slots 只含 DDF2, deleted_frozen_slots
#       只含 DDF1;
#     * 补位发布 (推进发布序号): DDF1 旧确认按删除释放 (released_deleted_confirmations),
#       DDF2 旧确认按逾期释放 (released_overdue_confirmations); 预演与发布方案一致;
#     * 新序号无旧截止: 全部槽位 pending, 评审人重新确认并提交; 旧确认/评语仅供追溯。
#   另演示: 预演后资料再变更 -> 补位发布 409 且当前发布版不变 (单独成段, 见脚本末)。
#
# 前置: 建议对新库运行 (脚本自行建数据; 编号已存在可忽略 409/200)。
#   DB_PATH=./data/app.db ORGANIZER_KEY=dev-organizer-key python -m app.main
# 用法: ./examples/deadline_deleted_frozen.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

P1=DDF1
P2=DDF2
# 5 名不同机构评审人, 保证两篇论文逾期释放后仍有合格补位者
for spec in 'DF1 df1-secret Inst-A' 'DF2 df2-secret Inst-B' 'DF3 df3-secret Inst-C' \
            'DF4 df4-secret Inst-D' 'DF5 df5-secret Inst-E'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 3, \"avoid_papers\": []}"
done
for pid in "$P1" "$P2"; do
  curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"paper_id\": \"$pid\", \"manuscript\": \"$pid 匿名稿全文...\",
    \"topics\": [\"AI\"], \"institutions\": [\"Univ-$pid\"]}"
done

echo "== 1. 普通预演并发布 (serial=1) =="
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
SER=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}" | j 'd["serial"]')
PLAN=$(curl -s "$BASE/assignment" -H "$ORG")
P1A=$(echo "$PLAN" | j "d[\"plan\"][\"$P1\"][0]"); P1B=$(echo "$PLAN" | j "d[\"plan\"][\"$P1\"][1]")
P2A=$(echo "$PLAN" | j "d[\"plan\"][\"$P2\"][0]"); P2B=$(echo "$PLAN" | j "d[\"plan\"][\"$P2\"][1]")
echo "    serial=$SER; $P1: $P1A/$P1B; $P2: $P2A/$P2B"

cred_of() { case "$1" in DF1) echo df1-secret;; DF2) echo df2-secret;; DF3) echo df3-secret;;
  DF4) echo df4-secret;; DF5) echo df5-secret;; esac; }

echo "== 2. 四名评审人均只确认、不交评语 (截止后将全部逾期) =="
for pair in "$P1 $P1A" "$P1 $P1B" "$P2 $P2A" "$P2 $P2B"; do
  set -- $pair
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/$1/decision" \
    -H "X-Reviewer-Id: $2" -H "X-Reviewer-Credential: $(cred_of "$2")" \
    -H 'Content-Type: application/json' -d "{\"paper_id\":\"$1\",\"decision\":\"confirm\"}"
done

echo "== 3. 设置约 2 分钟后的统一 UTC 评审截止 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
DEADLINE=$(python3 -c 'from datetime import datetime,timezone,timedelta; print((datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat().replace("+00:00","Z"))')
curl -s -X POST "$BASE/review-deadline" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"base_serial\": $SER, \"deadline_at\": \"$DEADLINE\"}" \
  | j "{'changed': d['changed'], 'deadline_at': d['review_deadline']['deadline_at']}"

echo "== 4. 删除 $P1 并以同编号重录 (当前发布版/序号不变, 修订号推进) =="
curl -s -X DELETE "$BASE/papers/$P1" -H "$ORG" \
  | j "{'frozen_serial': d['deletion']['published_serial'], 'frozen_plan': d['deletion']['published_plan']}"
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d "{
  \"paper_id\": \"$P1\", \"manuscript\": \"$P1 同编号重录稿\",
  \"topics\": [\"AI\"], \"institutions\": [\"Univ-$P1\"]}"

echo "== 5. 等待截止到达 (约 130 秒; 可手动把截止设近后观察) =="
sleep 130

echo "== 6. 截止状态汇总: $P1 冻结槽位不判逾期, 只显示删除前历史状态; $P2 真实逾期 =="
curl -s "$BASE/review-deadline" -H "$ORG" \
  | j "{'deadline_expired': d['deadline_expired'], 'summary': d['summary'],
        'withdrawn_slots': d['withdrawn_slots'], 'deleted_slots': d['deleted_slots'],
        'slots': [{'paper': s['paper_id'], 'reviewer': s['reviewer_id'],
                   'state': s['state'], 'deleted': s['deleted']} for s in d['slots']]}"
# 期望: summary.overdue == 2 (仅 $P2), deleted_slots == 2;
#       $P1 两槽位 state=confirmed_unsubmitted (删除前历史状态) 且 deleted=true

echo "== 7. 补位预演: $P1 不因旧截止排除原评审人; $P2 逾期释放并禁止原槽位重分 =="
DRY=$(curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG")
echo "$DRY" | j "{'feasible': d['feasible'],
        'overdue_slots': d['overdue_slots'],
        'deleted_frozen_slots': d['deleted_frozen_slots'],
        'P2_fixed_problems': d['fixed_problems'].get('$P2', []),
        'P2_excluded': [e for e in d['papers']['$P2']['excluded']],
        'plan': d['plan']}"
# 期望: overdue_slots 仅含 $P2 两槽位; deleted_frozen_slots 仅含 $P1 两槽位;
#       $P2 excluded 中 P2A/P2B 原因均为 review_overdue; 方案完整 feasible=true

echo "== 8. 补位发布: 双版本复核; 与预演方案一致, 两类释放分别计数 =="
BREV=$(echo "$DRY" | j 'd["revision"]')
BP=$(curl -s -X POST "$BASE/assignment/backfill/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $BREV, \"base_serial\": $SER}")
echo "$BP" | j "{'ok': d['ok'], 'serial': d['serial'],
       'carried_confirmations': d['carried_confirmations'],
       'released_overdue_confirmations': d['released_overdue_confirmations'],
       'released_deleted_confirmations': d['released_deleted_confirmations'],
       'overdue_slots': d['overdue_slots'], 'plan': d['plan']}"
NEW_SER=$(echo "$BP" | j 'd["serial"]')
echo "    预演方案 == 发布方案: $([ "$(echo "$DRY" | j 'd["plan"]')" = "$(echo "$BP" | j 'd["plan"]')" ] && echo yes || echo NO)"

echo "== 9. 新序号无旧截止: 全部槽位 pending; 旧确认/评语仅供追溯, 须重新确认并提交 =="
curl -s "$BASE/review-deadline" -H "$ORG" \
  | j "{'review_deadline': d['review_deadline'], 'deadline_expired': d['deadline_expired'],
        'summary': d['summary'], 'deleted_slots': d['deleted_slots']}"
NEW_RID=$(echo "$BP" | j "d[\"plan\"][\"$P1\"][0]")
echo "    $P1 新槽位 $NEW_RID 未确认直接交评语 -> HTTP $(code -X POST "$BASE/reviewer/assignments/$P1/review" \
  -H "X-Reviewer-Id: $NEW_RID" -H "X-Reviewer-Credential: $(cred_of "$NEW_RID")" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"$P1\",\"serial\":$NEW_SER,\"score\":5,\"comment\":\"未确认先交\"}") (409)"
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/$P1/decision" \
  -H "X-Reviewer-Id: $NEW_RID" -H "X-Reviewer-Credential: $(cred_of "$NEW_RID")" \
  -H 'Content-Type: application/json' -d "{\"paper_id\":\"$P1\",\"decision\":\"confirm\"}"
curl -s -X POST "$BASE/reviewer/assignments/$P1/review" \
  -H "X-Reviewer-Id: $NEW_RID" -H "X-Reviewer-Credential: $(cred_of "$NEW_RID")" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"$P1\",\"serial\":$NEW_SER,\"score\":5,\"comment\":\"新序号重新提交的评语\"}" \
  | j "{'serial': d['serial'], 'receipt': d['receipt'], 'changed': d['changed']}"

echo "== 10. 版本不符拒绝示例: 对当前版本再次补位预演后, 先制造一次资料变更再发布 -> 409, 发布版不变 =="
DRY2=$(curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG")
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "DDF9", "manuscript": "使预演过期的新增稿", "topics": ["AI"], "institutions": ["Univ-DDF9"]}'
echo "    过期补位发布 -> HTTP $(curl -s -o /tmp/bp409.json -w '%{http_code}' -X POST "$BASE/assignment/backfill/publish" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $(echo "$DRY2" | j 'd["revision"]'), \"base_serial\": $(echo "$DRY2" | j 'd["serial"]')}") (409)"
echo "    当前发布序号仍为: $(curl -s "$BASE/assignment" -H "$ORG" | j 'd["serial"]') (应等于 $NEW_SER)"

echo "== 完成: 删除冻结槽位与截止判断交叉处理演示结束 =="
