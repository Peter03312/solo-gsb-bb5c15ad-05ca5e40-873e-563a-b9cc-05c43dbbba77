#!/usr/bin/env bash
# 本轮用法端到端示例: 删除凭据冻结序号 与 统一评审截止判断的交叉处理
#
# 缺陷背景: 论文删除后发布方案保留旧槽位; 若该版统一评审截止已过, 删除并以同编号
#           重录后尝试补位, 旧槽位仍被判逾期 -> 原评审人被以 review_overdue 排除,
#           两名合格评审人同时被排除导致补不齐 (feasible=false)。
# 修复行为:
#   1. 截止后删除 -> 同编号重录 -> 补位: 冻结槽位不计逾期汇总 (deleted_frozen 标记),
#      不进 overdue_slots, 不产生 review_overdue 排除, 原评审人可重新获稿;
#   2. 旧确认/旧评语仅供追溯, 补位发布推进序号后一律 pending, 须重新确认并重新提交;
#   3. 同一补位事务内另一篇论文的真实逾期照常释放 (fixed_review_overdue) 且禁止该稿
#      重新分给原逾期评审人 (review_overdue);
#   4. 版本不符 409、无完整方案 422 均拒绝且不改发布版; 预演与发布方案一致;
#   5. 推进序号后旧截止不再生效, 新版初始无截止。
#
# 脚本把截止时刻设为很短的未来时刻 (约 5 秒) 并等待到点, 总等待很短。
# 建议对新库运行: ./examples/deadline_deleted_frozen.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }

echo "== 1. 录入 5 名不同机构评审人与两篇论文 P1(删除重录) / P2(真实逾期对照) =="
for spec in 'R1 r1-secret Inst-A' 'R2 r2-secret Inst-B' 'R3 r3-secret Inst-C' \
            'R4 r4-secret Inst-D' 'R5 r5-secret Inst-E'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 3, \"avoid_papers\": []}"
done
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿", "topics": ["AI"], "institutions": ["Univ-1"]}'
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P2", "manuscript": "P2 匿名稿", "topics": ["AI"], "institutions": ["Univ-2"]}'

echo "== 2. 普通预演并发布, 记下两篇各自的槽位 =="
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
SER=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}" | j 'd["serial"]')
PLAN=$(curl -s "$BASE/assignment" -H "$ORG")
P1A=$(echo "$PLAN" | j 'd["plan"]["P1"][0]'); P1B=$(echo "$PLAN" | j 'd["plan"]["P1"][1]')
P2A=$(echo "$PLAN" | j 'd["plan"]["P2"][0]'); P2B=$(echo "$PLAN" | j 'd["plan"]["P2"][1]')
echo "    P1: $P1A/$P1B   P2: $P2A/$P2B   serial=$SER"
cred() { case "$1" in R1) echo r1-secret;; R2) echo r2-secret;; R3) echo r3-secret;; R4) echo r4-secret;; R5) echo r5-secret;; esac; }
confirm() { curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/$2/decision" \
  -H "X-Reviewer-Id: $1" -H "X-Reviewer-Credential: $(cred "$1")" \
  -H 'Content-Type: application/json' -d "{\"paper_id\":\"$2\",\"decision\":\"confirm\"}"; }

echo "== 3. P1 两人均确认未交; P2 仅 $P2A 确认未交 ($P2B 保持 pending) =="
confirm "$P1A" P1; confirm "$P1B" P1; confirm "$P2A" P2

echo "== 4. 设置约 5 秒后的统一 UTC 截止并等待到点 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
DL=$(python3 -c 'from datetime import datetime,timezone,timedelta; print((datetime.now(timezone.utc)+timedelta(seconds=5)).isoformat().replace("+00:00","Z"))')
curl -s -o /dev/null -X POST "$BASE/review-deadline" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"base_serial\": $SER, \"deadline_at\": \"$DL\"}"
sleep 7
echo "    截止后状态汇总 (此时 4 个未交槽位全部判逾期, 含 P2 未确认的 $P2B):"
curl -s "$BASE/review-deadline" -H "$ORG" \
  | j "{'deadline_expired': d['deadline_expired'], 'summary': d['summary']}"

echo "== 5. 删除 P1 并以同编号重新录入 (当前发布版/序号不变, 修订号 +1) =="
curl -s -X DELETE "$BASE/papers/P1" -H "$ORG" \
  | j "{'revision': d['revision'], 'frozen_serial': d['deletion']['published_serial']}"
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 同编号重录稿", "topics": ["AI"], "institutions": ["Univ-1"]}'

echo "== 6. 截止视图: P1 冻结槽位移出逾期汇总 (deleted_frozen=true), P2 真实逾期保留 =="
curl -s "$BASE/review-deadline" -H "$ORG" | j "{
  'summary': d['summary'],
  'deleted_frozen_slots': d['deleted_frozen_slots'],
  'frozen': [(s['paper_id'], s['reviewer_id'], s['state']) for s in d['slots'] if s['deleted_frozen']]}"

echo "== 7. 补位预演: P1 可重新分给原评审人 (无 review_overdue); P2 真实逾期释放并禁分 =="
DRY=$(curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG")
echo "$DRY" | j "{'feasible': d['feasible'], 'unassigned': d['unassigned'], 'plan': d['plan'],
  'overdue_slots': d['overdue_slots'],
  'deleted_frozen_slots': d['deleted_frozen_slots'],
  'P1_excluded': [e['reason'] for e in d['papers']['P1']['excluded']],
  'P2_excluded': {e['reviewer_id']: e['reason'] for e in d['papers']['P2']['excluded']},
  'fixed_problems': d['fixed_problems']}"

echo "== 8. 版本不符 -> 409 且当前发布版不变; 再以预演版本发布 -> 成功, 预演/发布方案一致 =="
DRY_REV=$(echo "$DRY" | j 'd["revision"]'); DRY_SER=$(echo "$DRY" | j 'd["serial"]')
curl -s -o /dev/null -w '    过期修订号补位发布 -> HTTP %{http_code} (409)\n' \
  -X POST "$BASE/assignment/backfill/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $((DRY_REV-1)), \"base_serial\": $DRY_SER}"
PUB=$(curl -s -X POST "$BASE/assignment/backfill/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $DRY_REV, \"base_serial\": $DRY_SER}")
echo "$PUB" | j "{'serial': d['serial'],
  'carried_confirmations': d['carried_confirmations'],
  'released_overdue_confirmations': d['released_overdue_confirmations'],
  'released_deleted_confirmations': d['released_deleted_confirmations'],
  'overdue_slots': d['overdue_slots'], 'plan': d['plan']}"

echo "== 9. 新序号下 P1 槽位全部 pending: 未确认交评语 409, 重新确认后可提交 (新收据) =="
NEW_SER=$(echo "$PUB" | j 'd["serial"]')
curl -s -o /dev/null -w '    未确认直接交评语   -> HTTP %{http_code} (409)\n' \
  -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $P1A" -H "X-Reviewer-Credential: $(cred "$P1A")" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"P1\",\"serial\":$NEW_SER,\"score\":5,\"comment\":\"未确认先交\"}"
confirm "$P1A" P1
curl -s -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $P1A" -H "X-Reviewer-Credential: $(cred "$P1A")" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\":\"P1\",\"serial\":$NEW_SER,\"score\":5,\"comment\":\"新序号下重新提交的评语\"}" \
  | j "{'changed': d['changed'], 'serial': d['serial'], 'receipt': d['receipt']}"

echo "== 10. 旧截止不约束新发布版: 当前无截止, 逾期 409 不再出现 =="
curl -s "$BASE/review-deadline" -H "$ORG" \
  | j "{'review_deadline': d['review_deadline'], 'deadline_expired': d['deadline_expired'], 'overdue': d['summary']['overdue']}"

echo "== 11. 旧确认/旧评语仍由会务方按删除凭据追溯 =="
curl -s "$BASE/papers/P1/deletions" -H "$ORG" \
  | j "{'deletions': [(x['id'], x['published_serial'], x['published_plan']) for x in d['deletions']]}"
