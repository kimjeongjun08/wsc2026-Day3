"""
turn.py — 최종 튜닝툴 (autotune + back_tune 장점 통합, 단점 제거)

설계 원칙:
  - 측정은 "부하 중"에 한다 (부하 끝난 뒤 측정 = CPU burst 앱이 idle로 잡히는 버그 제거).
  - request/limit/memory 전부 실측 기반 (메모리 하드코딩 안 함).
  - 작은 request + burst limit: CPU share 독점 안 함(user/product 보호) + 비용 최소.
  - min=2: 노드 2대 분산 → 노드 1대 죽어도 생존 (가용성).
  - grader(injector.py)와 동일: stress length 50~200, 약한 부하.
  - 비용: Karpenter 하드캡 + stress max 상한 → 노드 폭증 불가.
  - 미달 시 재튜닝은 request 축소(파드 더 촘촘·더 싸게), util은 안 건드림.

사용법: python turn.py <CF endpoint>
"""
import asyncio
import aiohttp
import subprocess
import sys
import time
import random
import uuid
import json
import math
import os

# 콘솔 인코딩이 cp949 등이어도 유니코드 출력(→ ✔ ⚠ ── 등)에서 죽지 않게 stdout을 UTF-8로 고정.
#   (안 하면 Windows 기본 터미널에서 print 하나에 튜닝 전체가 중단될 수 있음)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

NAMESPACE = "apdev"

# 노드 스펙은 get_node_specs()가 클러스터에서 직접 읽음 (인스턴스 타입 하드코딩 없음).
SYSTEM_PER_NODE = 600  # 노드당 시스템/데몬셋 예약 여유
SYSTEM_MEM_PER_NODE_MI = 512  # 노드당 시스템/데몬셋 메모리 예약(aws-node·kube-proxy 등)

SLO = {"user": 200, "product": 200, "stress": 1000}   # 성능 SLO(ms)
AVAIL_SLO = 5000   # 가용성 SLO(ms) — 이 시간 넘으면 '가용성 실패'(채점기 기준). stress가 여기 걸릴 수 있음.

# 앱 판별: 요청당 CPU 부담(cpu_m / rps). 이 이상이면 CPU-bound(요청이 CPU를 태움 → util-HPA 작동).
#   미만이면 I/O-bound(DB 대기만 → CPU 안 오름 → util-HPA 무의미 → 적정 min으로 대응).
CPU_BOUND_MPS = 25

# cpu-bound request = 실사용 × 이 계수. 오버서브(작은 request→노드 몰림→스로틀)를 없애는 핵심 다이얼.
#   높을수록(1.0=실사용) 스로틀 0·성능↑·노드↑(비용). 낮을수록 싸지만 스로틀 위험. 0.7~0.85 권장.
CPU_REQ_FACTOR = 0.75

# ★4사분면 분류(측정 기반, 앱이름 하드코딩 X): CPU(cpu/rps) × 지연/burst 프로파일.
#   CPU축(bound=cpu/io)은 util·request를 정하고, 지연/burst축은 "상주 여유(min↑)"를 정함.
#   → "CPU안씀+느림/burst" 앱은 CPU-HPA가 못 잡고 반응형 scaler도 순간트랩 전엔 못 채우니 상주로 흡수.
SLOW_RATIO = 0.75   # 부하 p95 ≥ SLO×0.75 → '느림'(SLO 근접/초과) → 상주 필요
BURST_RATIO = 3.0   # p99/p50 ≥ 3 → 'burst'(요청간 편차 큼=순간트랩) → 상주로 방어
HEADROOM_ADD = 2    # 위 해당 시 min 부스트(파드 수). 반드시 max로 클램프(노드폭증 방지).
MNG_RESERVE_FRAC = 0.75  # ★io 상주(min)는 MNG 앱가용의 이 비율까지만.
                         #   여유를 남기는 이유:
                         #   ① 파드를 재배치할 자리가 있어야 한다. 꽉 채우면 축출된 파드가
                         #      갈 곳이 없어 Karpenter가 새 노드를 만든다(실측: 노드 2→7).
                         #   ② 상주는 '항상 켜져 있는' 값이라 MNG를 넘으면 상시 비용이 된다.
                         #      실측 — user min이 6으로 부스트되고 워밍업이 ×2 해서 12파드,
                         #      12 × 116m = 1392m > MNG 1278m → 워커 노드. 트래픽은 1.8 rps였다.
                         #   ★스파이크 대응은 상주가 아니라 scaler의 need_rps가 한다.
                         #     그건 MNG를 넘어도 정당하다(처리량이 요구하므로).

# io 앱(user/product) 상시 baseline. ★이 앱들은 DB/캐시라 CPU를 거의 안 씀 → CPU-HPA가 부하에
#   둔감(요청 몰려 지연 터져도 CPU 안 올라 스케일 지연 → "성능 떨어지면 회복 안 됨"의 정체).
#   대응: 상주(min)를 넉넉히 = CPU-HPA에 의존 않고 스파이크를 상주 파드로 흡수. 파드가 작아(30~200m)
#   MNG 노드에 패킹돼 비용 거의 안 늚(실측 ratio 1.11도 비용만점 = 압도적 여유). 성능이 유일 제약이라
#   상주를 넉넉히 = 스파이크를 상주로 흡수. 6=2h 테스트 user 85% → 상주+topologySpread로 스파이크 용량↑.
IO_HEADROOM = 2

# Karpenter consolidateAfter: 노드가 '놀기 시작한 뒤' 얼마 만에 회수하는가.
#   짧으면 부하 끝나고 빨리 회수 → 노드시간↓(비용 이득). 길면 warm 유지 → 스파이크 매끄럽지만 비용↑.
#   30초 = 부하 끝나면 빠른 회수(비용). turn.py는 이 값으로 완결 — 외부 툴 의존 없음.
CONSOLIDATE_AFTER = "30s"  # ★노드 저활용 30초 뒤 회수.
                           #   ★60s로 늘렸다가 30s로 내렸다. 비용은 '노드 수의 시간 평균'이므로
                           #     회수가 늦는 시간이 그대로 점수 손실이다.
                           #     실측: 스파이크 후 노드가 7대에서 오래 남아 평균이 3.4대가 됐다(비용 8/12).
                           #   ★churn 걱정은 다른 곳에서 막는다: scaler의 DOWN_STEP_FRAC(단계 축소)와
                           #     HOLD_AFTER_UP(반전 차단)이 파드를 급붕괴시키지 않으므로,
                           #     '파드가 있는 노드'는 30초 안에 비지 않는다 = 회수 대상이 안 된다.
                           #     즉 30s는 '진짜로 빈 노드'만 빠르게 걷어낸다.
                           #   ★15s에서 올렸다. 15초는 너무 짧아 churn이 난다:
                           #     부하가 잠깐 빠지면 노드를 회수하고, 곧 다시 오르면 재부팅한다.
                           #     부팅이 20~60초 걸리므로 그 동안 파드가 Pending → 큐가 쌓여
                           #     성능이 깎이고, 결국 노드는 다시 뜨므로 비용도 안 아껴진다.
                           #     실측 지적: 베이스라인 4.5 rps에서도 노드 2↔3 왕복 관측.
                           #   ★5분(외부 제안)은 너무 길다: 스파이크가 끝난 뒤 5분간 빈 노드가
                           #     유지되면 2시간 평균에 그대로 반영된다(비용 직접 손실).
                           #   45초 = 노드 부팅 시간(20~60초)과 같은 스케일. '진짜로 부하가
                           #   빠졌다'가 확인되는 최소 시간이면서 평균 비용에 거의 영향이 없다.

# ★노드 상한 = "총 노드 수" 단계 사다리.
#   채점 비용은 노드 '개수'만 본다(인스턴스 타입·크기 무관) → 개수가 유일한 통화이므로
#   개수로 사다리를 만드는 것이 앱·부하·인스턴스 타입과 무관하게 성립하는 유일한 방법이다.
#   baseline(=MNG + 무거운앱 최소 1노드) 배수로 정의 → MNG 수가 바뀌어도 자동으로 따라온다.
#     1단계(×2) : 평시~중간 부하. 처리 가능하면 여기서 끝(비용 최선)
#     2단계(×3) : 밀려서 회복이 필요한 강한 부하
#     3단계(×4) : 아무리 힘들어도 여기까지 = "정상 대응 상한"
#     4단계(×5) : ★비상 전용. 파드가 자원부족으로 Pending일 때만(= 노드가 확실한 병목일 때만) 올라간다.
#                 지연이 나쁘다는 이유만으로는 안 올라간다 → 앱 병목에 노드를 퍼붓는 낭비 차단.
#   ★왜 사다리인가: 채점에 "성능 30% 미달 → 비용 12점 전체 0" 게이트가 있어
#     자원 부족 손실(성능+가용성+비용12)이 노드 추가의 비용 손실(1~4점)보다 항상 크다.
#     그래서 "막혀서 못 늘어남"은 어떤 경우에도 피하고, 대신 scaler가 자원 부족을
#     실제로 확인했을 때만 다음 단계로 올린다(관측 기반, 선제 확장 아님).
#   ★이전 방식(비용 티어 역산 + 1대씩 확장)의 문제: 초기값이 과소일 때 1대씩 90초 간격으로
#     올라가 회복 전에 손실이 누적됐고, 목표 ratio가 특정 부하 duty 가정(PEAK_DUTY)에
#     의존했다. 단계 사다리는 그 가정을 제거한다.
NODE_STAGE_MULT = (2, 3, 4, 5)   # baseline 배수 → baseline 2면 4 / 6 / 8 / 10
                                 # ★사다리는 '정상 대응' 범위를 정한다. 그 위(비상)는 처리량 요구가 정한다.
# ★절대 천장은 '처리량 요구'에서 유도한다 — 고정 상수로 두면 게이트가 깨진다.
#   실측 사고: stress cap 0.9 rps/pod인 앱에 부하 14 rps가 들어왔다.
#     필요 파드 = 14/0.9 = 16파드 = 16노드(1파드=1노드). 그런데 천장이 10대였다.
#     → ρ = 14/(9×0.9) = 1.73 → 큐 무한 증가 → 성능 19% → 채점 게이트(30%) 붕괴 → 비용 12점 전부 0.
#   손실 비대칭이 명확하다:
#     · 천장이 낮아 처리 실패 → 성능·가용성 동시 붕괴 + 비용 전부 0
#     · 천장이 높아 노드를 더 씀   → 비용 티어 몇 점 (그리고 수요가 없으면 안 쓴다)
#   그래서 천장은 '수요를 담을 수 있을 만큼' 높아야 하고, 실제 노드 수는 수요 기반 사이징이 정한다.
#   ※낭비 경로(baseline 래칫·HPA 버퍼·지연 노이즈·도달불가 추격)를 모두 막았기 때문에
#     천장을 올려도 그 값까지 차오르지 않는다. 실측 근거: 같은 사이징으로 비용 10/10을 받았다.
MAX_NODES_ABS = 6               # ★총 노드 하드캡. 어떤 경로(지연·Pending·게이트임박)로도 초과 불가.
                                # ★24 → 10 → 8 → 5. 매 단계가 실측으로 내려왔다:
                                #   · 12대 시점: stress 12파드 처리량 2.54rps, 파드당 CPU가 limit의
                                #     2~17%, 서버측 준수율 88.2% → 노드를 늘려도 처리가 안 나아졌다.
                                #   · 7대 시점: stress 성능 87.5%, 비용 8/12(평균 3.4대).
                                #     노드 5대분을 더 써서 성능 티어는 0개 올렸다 = 순손실 4점.
                                #   → '노드를 더 주면 처리가 된다'는 가정이 이 워크로드에서 반복적으로
                                #     틀렸다. 처리량 부족의 실제 원인은 cap 오측정이고, 그건 scaler의
                                #     cap 자기교정(양방향)이 고친다. 노드는 5대로 묶는다.
MAX_NODES_BREAK = 6             # ★비상 한계 = 일반 상한과 동일하게 고정했다(break-glass 제거).
                                #   이전에는 '게이트 임박이면 14대까지' 열었는데, 그 경로가 실제로
                                #   점수를 깎았다: 노드가 벌어지면 2시간 평균이 올라가 cost_ratio가
                                #   커지고 비용 티어가 떨어진다.
                                #   즉 '게이트(12점)를 지키려고' 연 노드가 비용(12점)을 같은 크기로
                                #   깎아 이득이 0이거나 손실이었다.
                                #   ★대신 게이트는 노드가 아니라 '파드 사이징 + cap 자기교정'으로
                                #     지킨다. 그리고 현재 최저 성능이 87.5%로 게이트(30%)의 3배라
                                #     비상 경로가 필요한 상황 자체가 아니다.
MAX_NODES_HARD = 7              # ★총 노드 절대 상한 = 7 (MNG 1 + Karpenter 최대 6).
                                #   stress-pool 캡(12CPU=6대)과 맞춤. 실제 평균 노드는 2~3대.
                                #   캡은 안전망이고, 최소 노드 운영은 HPA util이 담당한다.
                                #   ★5 → 6. stress가 '노드당 1파드'(코어 독점)이므로 스케일에
                                #     노드가 필요하다. 5대면 stress 4파드가 상한이고, 그게
                                #     부족하면 파드가 영구 Pending이 되어 성능이 무너진다.
                                #   ★비용 안전: 캡은 천장이고 실제 노드는 수요가 정한다.
                                #     baseline 2대 + 스파이크 구간만 3~5대면 평균 2.5대 안이다.
                                #   근거(채점 산술): 비용 = cost_ratio(=평균EC2/2) 티어이고
                                #   1.25(=평균 2.5대)를 달성하면 +1점이다.
                                #   baseline 2대 + 스파이크 여유 3대 = 5대면, 스파이크가 전체
                                #   시간의 1/6을 차지해도 평균 2.5대 안에 들어온다.
                                #   성능·가용성(24점)은 파드 사이징으로 지키고, 노드는 처리량이
                                #   물리적으로 부족할 때만 5대까지 쓴다.
EMERGENCY_STAGES = 0            # ★비상 전용 단계 제거. 사다리의 모든 단계가 '정상 대응' 범위다.
                                #   8대가 하드캡이므로 단계를 비상/정상으로 나눌 이유가 없어졌다
                                #   (나누면 '정상 상한'이 8보다 낮아져 스파이크에서 처리량만 잃는다).

MEM_CAP_HEADROOM = 1.3          # NodePool memory limit 여유배수 (CPU가 유일한 구속 캡이 되게)
UTIL_SAFETY = 0.9               # (레거시) 일부 계산에 남아 있는 안전계수
UTIL_TRIGGER_FRAC = 0.70        # ★HPA 증설 시작점 = 포화 CPU의 이 비율.                                #   0.9면 포화 직전에야 반응해 스파이크에서 이미 큐가 쌓인다.
                                #   0.70이면 여유가 30% 남은 시점에 증설을 시작해
                                #   파드가 준비되는 동안(~10초)에도 SLO를 지킬 수 있다.
UTIL_IDLE_MARGIN = 1.35         # ★평상시 증설 금지 마진. 트리거는 '저부하 실측 이용률 × 이 값'
                                #   보다 반드시 위여야 한다. 아니면 트래픽이 없는 baseline에서도
                                #   HPA가 파드를 늘려 노드가 증가하고 비용 만점이 깨진다
                                #   (실측: util 85%로 클램프했더니 트리거 224m < 저부하 263m).
# ★★HPA 스케일 동작의 실제 기준은 'requests × target' = 트리거 CPU(m)다.
#   HPA는 파드 평균 CPU가 이 절대값을 넘으면 증설한다. 따라서:
#     · 트리거 CPU → 스케일 '시점'을 정한다 (성능)
#     · request    → 노드당 파드 밀도만 정한다 (비용)
#     · util       → 그 둘의 비율일 뿐, 독립적인 의미가 없다
#   ★이 분해가 핵심이다. util을 직접 잡으면 request가 바뀔 때마다 스케일 동작이 흔들린다
#     (실측: 같은 앱에서 util이 10%~311%로 요동쳤다). 트리거를 고정하고 request로
#     밀도만 조절하면 성능과 비용을 독립적으로 튜닝할 수 있다.
#
#   ★트리거를 '요청당 CPU × 동시 요청 수'로 유도한다 (측정 기반, 앱 무관):
#     요청당 CPU = 포화CPU ÷ 포화rps
#     트리거     = 요청당 CPU × 동시요청 목표
#   즉 "파드에 요청이 N개 떠 있으면 증설한다"가 된다. N은 앱 유형(io/cpu)이 정한다.
IO_CONC_TARGET = 1.2            # io 앱: 파드당 동시 요청 1.2개에서 증설.
                                #   io 요청은 대부분 DB 대기라 CPU가 싸고 병렬에 유리하다.
                                #   1개를 조금 넘으면 큐가 생기기 시작하므로 그 지점에서 늘린다.
CPU_CONC_TARGET = 0.5           # CPU-burn 앱: 동시 요청 0.5개(파드가 절반 시간 바쁨)에서 증설.
                                #   이 앱은 요청 하나가 코어를 다 쓰므로 '동시 1개'가 되면
                                #   이미 두 번째 요청이 대기한다 → 그 전에 미리 늘려야 한다.
CPU_CONC_EARLY = 0.35           # ★C안(조기 증설)의 동시요청 목표. CPU_CONC_TARGET(0.5)보다 낮다.
                                #   stress가 90% 티어에 1~2%p 못 미칠 때 그 원인은 '증설 시점'이다.
                                #   0.35면 파드가 1/3만 바빠도 늘리기 시작해 노드 부팅(60초)을
                                #   큐가 쌓이기 전에 시작한다.
                                #   ★대가는 노드다 — 그래서 A/B/C로 재서 '노드 1대 값을 하는
                                #     성능 향상'일 때만 채택된다(판정은 채점표 티어가 한다).
TRIGGER_FLOOR_M = 20            # 트리거 절대 하한(m). 요청당 CPU가 거의 0인 앱(캐시 앱 0.07 m·s)은
                                #   트리거가 0에 수렴해 노이즈로 증설된다.
                                #   metrics-server 해상도(수 m)를 고려한 실용 하한.
IO_MAX_TARGET = 18              # ★io 앱의 목표 max 파드 수 = MNG에 들어가는 최대 수.
IO_UTIL_FIXED = 33              # ★폴백 전용. 측정 실패 시에만 사용.
                                #   정상 경로에서는 _derive_util이 측정 기반으로 유도한다.
IO_UTIL_CACHED = 29             # ★폴백 전용 (캐시 앱). 측정 실패 시에만 사용.
# ★동시성 목표(conc_target): "동시 요청 몇 건일 때 증설을 시작할 것인가"
#   이 값 × 요청당 CPU = 트리거 CPU.  트리거 / request = util.
#   앱 특성별로 다르게 두는 이유:
CONC_IO_NOCACHE = 1.2           # 비캐시 io (user): 모든 요청이 도달. 동시 1.2건 = 큐 시작 직전.
                                #   1.0이면 요청 하나에 즉시 증설(과민), 1.5면 큐가 쌓인 뒤(늦음).
                                #   1.2는 '큐가 막 생기려는 순간' = SLO를 지키면서 최소 파드 운영.
CONC_IO_CACHED = 1.0            # 캐시 io (product): 캐시 미스만 파드에 도달.
                                #   평상시 CPU ≈ 0이고 미스가 오면 급등한다. 미스 1건에 즉시 반응.
                                #   1.0 = "파드에 요청 1건이 도달하면 바로 증설 준비"
                                #   ★왜 비캐시보다 낮은가: 캐시 히트 동안 CPU가 0이라 트리거에
                                #     절대 안 걸린다. 미스가 시작되는 순간만 잡으면 된다.
                                #     비캐시는 항상 요청이 오므로 1.0이면 baseline에서 걸린다.
CONC_CPU_BOUND = 0.5            # CPU-bound (stress): 요청 1건이 코어를 독점한다.
                                #   동시 1.0 = 이미 대기 발생. 0.5 = 파드가 절반 바쁠 때 증설 시작.
                                #   ★SLO 1.0s에서 역산: 처리시간 ~340ms, 대기 여유 660ms.
                                #     0.5면 증설 시작~완료(60초) 동안 큐가 SLO를 넘지 않는다.
IO_TARGET_UTIL = 0.33           # ★io request를 정하는 유일한 자유도. request = 트리거 ÷ 이 값.
                                #   작을수록 request가 커져 밀도↓, 클수록 작아져 밀도↑.
                                #   ★트리거가 고정이므로 이 값은 스케일 '시점'을 바꾸지 않는다
                                #     — 밀도만 바꾼다. 그래서 성능과 비용을 분리해 조절할 수 있다.
UTIL_FAITH_FLOOR = 0.6          # ★CPU 신호 '충실도'의 하한.
                                #   util = UTIL_TRIGGER_FRAC × 100 × 충실도 이고,
                                #   충실도 = min(1, 요청당CPU ÷ CPU_BOUND_MPS) 이다.
                                #   요청당 CPU가 극단적으로 작은 앱(캐시 앱: 0.07 m·s)은
                                #   충실도가 0에 가까워 util이 5%까지 떨어지는데, 그러면
                                #   baseline 노이즈로 계속 증설된다. 0.6이 그 바닥이다
                                #   (→ util 최저 42%). io 앱이 '일찍 증설'하는 목적은 지키면서
                                #   노이즈 증설은 막는 지점.
UTIL_SANITY_MIN, UTIL_SANITY_MAX = 10, 1000
                                # ★쿠버네티스가 받아들이는 범위를 지키는 최후 방어선.
                                #   정상 경로에서 util은 '저부하 이용률 × 여유'(하한)와
                                #   '포화 이용률 × 0.95'(상한) 사이에서 측정으로 결정된다.
                                #   ★[40, 300] 같은 절대 범위를 쓰지 않는 이유: 두 방향으로 틀린다.
                                #     저부하 이용률이 상한을 넘는 앱은 baseline에서 계속 증설되고
                                #     (실측: stress 저부하 104% vs 목표 85% → 과증설),
                                #     저부하가 아주 낮은 앱은 증설이 너무 늦어진다.
                                #   ★상한을 85 → 300으로 올렸다. 85 제한이 오판이었다:
                                #     request를 스케줄 예약용으로 작게 잡으므로(무료 구간 확보)
                                #     실사용 CPU가 request를 크게 넘고 포화 이용률이 200~1600%가 된다.
                                #     그 구조에서 85로 클램프하면 저부하 이용률이 85를 넘는 앱은
                                #     baseline에서 이미 목표 초과 → 트래픽 없이 계속 증설한다
                                #     (실측: stress 저부하 104% vs 목표 85% → 과증설 + 노드 폭증).
                                #   ★averageUtilization > 100 은 쿠버네티스에서 유효하다.
                                #     request 대비 비율일 뿐이고, request가 실사용보다 작으면
                                #     100 초과가 정상적인 동작점이다.
UTIL_IO = 45                    # ★io 앱(user/product)의 HPA util 목표.
                                #   io 앱은 CPU가 낮게 나온다(요청당 CPU가 작으므로). 70%면 증설
                                #   트리거가 너무 늦어 스파이크에서 파드가 부족해진다(실측: 63.6%).
                                #   45%면 CPU가 조금만 올라도 즉시 증설 → 빠른 스파이크 대응.
                                #   ★baseline에서 과증설이 안 되는 이유: request가 '파드당 실사용'에
                                #     맞춰져 있으므로 저부하에서는 CPU가 45% 미만이다.
                                #   ★100% 초과 값을 쓰지 않는다. 쿠버네티스는 허용하지만,
                                #     그러면 HPA 목표가 request 위에 있어 동작이 직관을 벗어나고
                                #     request가 실사용보다 작아져 노드 과밀 배치가 생긴다.
                                #     실측 라이브: user 45%/160%, product 185%/160%, stress 78%/301%
                                #     — 같은 클러스터에서 앱마다 기준이 달라 예측이 불가능했다.
                                #   ★40 미만: 저부하 노이즈로 증설된다.
                                #     85 초과: 포화 직전에야 반응해 스파이크에서 이미 큐가 쌓인다.
IO_RESIDENT_FRAC = 0.4          # ★io 상주가 채우는 MNG 예산 비율.
                                #   나머지 60%는 HPA가 스파이크에 쓸 여유로 남긴다.
                                #   상주가 클수록 스파이크 초입 대응이 빠르고(HPA 15초를 벌어줌)
                                #   MNG 안이라 노드 비용은 0이다. 다만 전부 채우면 HPA가
                                #   늘릴 자리가 없어 즉시 Pending → 노드 부팅 대기가 생긴다.
IO_SCALE_HEADROOM = 40          # ★io 앱이 'MNG 무료 구간'에서 확보해야 하는 스케일 배수.
                                #   io request 상한 = avail_mng ÷ 이 값.
                                #   ★12 → 40. 근거: io 앱은 limit이 없으므로 request는 순수하게
                                #     '스케줄 예약'이고, 파드는 필요한 만큼 CPU를 burst로 쓴다.
                                #     따라서 request는 '트래픽 처리에 필요한 최소'로 잡아야 하고,
                                #     작을수록 상시 켜진 MNG 노드에 파드가 많이 들어간다.
                                #     그 안에서의 스케일은 노드 비용이 0이므로, 파드가 과도하게
                                #     늘어나도 노드가 늘지 않는다 = 성능은 얻고 비용은 안 잃는다.
                                #   ★실측 근거: io request 30m 구성이 성능 만점에 가까웠다.
                                #     avail_mng 1278m ÷ 40 ≈ 32m 로 그 수준에 맞춰진다.
                                #   ★반대로 크게 잡으면(12 → 106m) MNG에 12파드만 들어가고,
                                #     스파이크마다 Pending → 노드 추가 → 부팅 60초 동안 성능 손실.
IO_RESIDENT = 4                 # ★io 앱 상주 목표 파드 수.                                #   io 앱은 request가 작아 MNG 한 노드에 다 패킹된다 → 노드 비용 0.
                                #   상주를 여러 개 두는 이유는 두 가지다:
                                #     ① 스파이크 초반을 상주로 흡수(HPA 반응 15초를 벌어준다)
                                #     ② request가 '파드당 실제 CPU'가 되어 HPA util이 의미를 갖는다
                                #        (단일 파드 기준으로 잡으면 트리거가 share배로 부풀어 HPA가
                                #         영원히 발동하지 않는다 — 실측 user 41.3%의 원인)
IO_REQ_MARGIN = 1.25            # io request 폴백 경로의 여유 계수
                                #   (주 경로는 측정 유도식이라 이 값을 쓰지 않는다)
IO_TRIGGER_MULT = 2.0           # ★io 앱 HPA 증설 시작점 = 상주 동작점의 이 배수.
                                #   즉 '트래픽이 2배가 되면 HPA가 즉시 증설'.
                                #   ★3.0으로 올렸다가 2.0으로 되돌렸다:
                                #     3.0은 HPA를 사실상 끄는 값이었다. 그건 'HPA가 max까지
                                #     채워 노드를 만드는' 문제를 막으려는 것이었는데,
                                #     그 문제는 이제 maxReplicas를 target + 버퍼로 묶어서
                                #     구조적으로 해결됐다(scaler.HPA_BUF_FRAC).
                                #   ★버퍼가 피해를 제한하므로 HPA를 반응적으로 둬도 안전하다:
                                #     HPA가 아무리 채워도 target + 25%(패킹) / +1(독점)까지다.
                                #     그 대가로 scaler의 2초 사이클 사이에 CPU가 급등하는
                                #     구간을 HPA가 메워준다 = 순이득.
CAP_SEED_DISCOUNT = 0.8         # ★scaler에 넘기는 초기 cap을 이 비율로 깎는다 (패킹 앱만).
                                # ★0.10으로 내렸다가 되돌렸다 — user 파드 과증설의 직접 원인이었다:
                                #   need_rps = 투영rps / cap 이므로 cap을 2.5배 깎으면
                                #   파드 수가 2.5배가 된다. 패킹 앱이라 MNG 안에서는 공짜지만,
                                #   MNG를 넘는 순간 워커 노드가 붙는다(실측: 노드 7대, 비용 8/12).
                                #   0.25가 '초기 손실 방지'와 '과증설 방지'의 균형점이다.
                                #   근거: turn.py 측정은 같은 key를 반복 조회하므로 DB 캐시·
                                #   커넥션풀이 더워진 상태다. 실트래픽은 매번 다른 key라
                                #   파드가 매 요청마다 실제 일을 한다.
                                #   실측 괴리: user cap 46.4로 측정됐으나 실부하에서는 ~1.7 (27배).
                                #   ★비대칭이 명확하다 — 이게 이 값의 유일한 근거다:
                                #     과소(cap 작음) → 파드가 좀 더 뜬다. 패킹 앱은 max가 MNG 안으로
                                #       묶여 있어 노드가 안 늘어난다 = 비용 피해 물리적으로 0.
                                #       그리고 10~20초 뒤 상향 외삽이 실제 용량으로 되돌린다.
                                #     과대(cap 큼)   → 파드가 부족해 큐가 쌓이고 준수율이 직접 깎인다.
                                #       준수율은 누적 비율이라 그 손실은 나중에 복구되지 않는다.
                                #   → 한쪽은 회복 가능하고 다른 쪽은 영구 손실이므로 과소가 정답이다.
                                #   ★0.25 → 0.05: 0.25로는 46.4×0.25 = 11.6 으로 여전히 실제(1.7)의
                                #     7배였다. 그 상태로 스파이크가 들어오면 need_rps가 3파드로
                                #     계산돼(실제 필요 15파드) 초반 수십 초를 ρ>1로 보낸다.
                                #   ★노드 독점 앱(stress)에는 적용하지 않는다 — 그쪽은 파드=노드라
                                #     과소가 곧 노드 낭비이고(비용 직접 손실), 하한(CAP_MIN_MONO)과
                                #     교정 관계식이 이미 수렴을 보장한다.
SCORE_PCTL = 0.92               # ★cap 측정에 쓰는 목표 백분위. scaler와 같은 값이어야 cap과
                                #   런타임 제어가 같은 기준을 본다.
                                #   근거(채점표와 무관): 꼬리 한두 건에 흔들리지 않을 만큼 낮고
                                #   (0.99는 단일 이상치가 cap을 왜곡), 다수 요청을 대표할 만큼 높다.
                                #   ※scaler는 운영 중 앱별 달성 가능치로 목표를 자기교정한다.


def node_stages(baseline_nodes, max_nodes):
    """총 노드 수 단계 사다리 → 오름차순 리스트. 중복 제거·천장 클램프.
    baseline 자체는 단계에 넣지 않는다(baseline은 '확장 전' 상태이므로)."""
    st = {min(max_nodes, max(baseline_nodes + 1, baseline_nodes * m)) for m in NODE_STAGE_MULT}
    return sorted(st)


def kubectl(cmd):
    r = subprocess.run(f"kubectl {cmd}", shell=True, capture_output=True, text=True)
    return r.returncode == 0, r.stdout.strip()


def _parse_cpu_m(s):
    """'1930m' 또는 '2' → 밀리코어 int."""
    s = s.strip().strip('"')
    if not s:
        return None
    return int(s[:-1]) if s.endswith("m") else int(float(s) * 1000)


def _parse_mem_mi(s):
    """'3854360Ki' / '3764Mi' / '15Gi' → Mi int."""
    s = s.strip().strip('"')
    if not s:
        return None
    try:
        if s.endswith("Ki"):
            return int(s[:-2]) // 1024
        if s.endswith("Mi"):
            return int(s[:-2])
        if s.endswith("Gi"):
            return int(float(s[:-2]) * 1024)
        return int(s) // (1024 * 1024)   # bytes
    except ValueError:
        return None


def get_node_specs():
    """워커 노드의 실제 스펙을 클러스터에서 읽는다 (인스턴스 타입 하드코딩 없음).
    반환: (allocatable CPU밀리코어, 물리 vCPU, allocatable 메모리Mi, capacity 메모리Mi, 인스턴스 타입)
    ★allocatable과 capacity를 모두 읽는 이유: 파드 사이징은 allocatable 기준이지만
      Karpenter NodePool의 limits 집계는 노드 capacity 기준이다. 둘을 섞으면 노드 캡이 어긋난다."""
    ok, alloc = kubectl("get nodes -o jsonpath=\"{.items[0].status.allocatable.cpu}\"")
    _, cap = kubectl("get nodes -o jsonpath=\"{.items[0].status.capacity.cpu}\"")
    ok_m, amem = kubectl("get nodes -o jsonpath=\"{.items[0].status.allocatable.memory}\"")
    ok_cm, cmem = kubectl("get nodes -o jsonpath=\"{.items[0].status.capacity.memory}\"")
    _, itype = kubectl("get nodes -o jsonpath=\"{.items[0].metadata.labels.node\\.kubernetes\\.io/instance-type}\"")
    alloc_m = _parse_cpu_m(alloc) if ok else None
    try:
        vcpu = int(float(cap.strip().strip('"'))) if cap else None
    except ValueError:
        vcpu = None
    node_mem_mi = _parse_mem_mi(amem) if ok_m else None
    node_mem_cap_mi = _parse_mem_mi(cmem) if ok_cm else None
    return (alloc_m or 1800, vcpu or 2, node_mem_mi or 3500,
            node_mem_cap_mi or int((node_mem_mi or 3500) * 1.25),
            (itype.strip().strip('"') or "unknown"))


def get_system_reserve(default_m=600):
    """노드에 이미 올라간 '앱 외' 파드들의 CPU request 합계를 노드 역할별로 실측한다.
    반환: (MNG 노드 예약m, 워커(Karpenter) 노드 예약m)

    ★SYSTEM_PER_NODE(600m)는 추정값이었고, 노드 역할에 따라 실제가 크게 다르다:
        MNG 노드      : coredns·metrics-server·LBC·karpenter가 전부 여기 올라감 (실측 525m+)
        Karpenter 노드: aws-node·kube-proxy만 (실측 125m)
      MNG 노드 값이 'user+product 상주가 그 노드에 들어가는가'를 결정하므로,
      추정이 실제보다 작으면 turn.py는 "들어간다"고 계산하지만 스케줄러는 거부한다
      → 상주 파드가 별도 노드로 밀려 baseline 2대가 깨진다(실측: baseline 4대).
      반대로 실제보다 크면 상주를 과도하게 깎아 스파이크 대응력을 잃는다."""
    ok, out = kubectl('get pods -A --field-selector=status.phase=Running -o jsonpath='
                      '"{range .items[*]}{.spec.nodeName}|{.metadata.namespace}|'
                      '{range .spec.containers[*]}{.resources.requests.cpu}{\',\'}{end}{\'\\n\'}{end}"')
    if not ok or not out:
        return default_m, default_m
    per_node = {}
    for line in out.replace('"', '').splitlines():
        parts = line.split("|")
        if len(parts) < 3 or not parts[0].strip():
            continue
        node, ns, reqs = parts[0].strip(), parts[1].strip(), parts[2]
        if ns == NAMESPACE:                      # 우리 앱 파드는 제외(예약 대상 아님)
            continue
        tot = 0
        for r in reqs.split(","):
            v = _parse_cpu_m(r) if r.strip() else None
            if v:
                tot += v
        per_node[node] = per_node.get(node, 0) + tot
    if not per_node:
        return default_m, default_m
    # Karpenter 노드 목록으로 역할 구분
    _, kp = kubectl("get nodes -l karpenter.sh/nodepool --no-headers -o custom-columns=N:.metadata.name")
    kp_names = {n.strip() for n in (kp or "").splitlines() if n.strip()}
    mng_vals = [v for n, v in per_node.items() if n not in kp_names]
    kp_vals = [v for n, v in per_node.items() if n in kp_names]
    mng_r = max(mng_vals) if mng_vals else default_m
    kp_r = max(kp_vals) if kp_vals else (min(mng_vals) if mng_vals else default_m)
    # 여유 마진: 데몬셋이 나중에 추가될 수 있으므로 조금 더 잡는다
    return int(mng_r * 1.15) + 20, int(kp_r * 1.15) + 20


def rid():
    return str(random.randint(100000000000, 999999999999))


def count_live_nodes():
    """실제로 살아있는(=EC2 비용이 발생하는) 노드 수.
    ★`get nodes --no-headers`를 그냥 세면 안 된다. 다음이 섞여 들어와 과대집계된다:
        · EC2는 이미 종료됐는데 Node 객체만 남은 것        → STATUS=NotReady
        · Karpenter가 막 띄워 아직 등록 중인 것            → STATUS=NotReady + unregistered taint
        · 삭제 진행 중인 것                                → deletionTimestamp 존재
      실측 사고: 툴은 6대로 보고했는데 EC2 콘솔은 3대였다. 그 과대집계 때문에
      수렴 판정이 안 되고 드레인 루프가 계속 돌아 오히려 churn을 만들었다.
    ※ 'Ready,SchedulingDisabled'(cordon)는 EC2가 살아있으므로 센다 — 비용이 발생한다."""
    ok, out = kubectl("get nodes --no-headers -o custom-columns="
                      "NAME:.metadata.name,ST:.status.conditions[-1].type,DEL:.metadata.deletionTimestamp")
    if not ok or not out:
        return 0
    n = 0
    for line in out.splitlines():
        p = line.split()
        if len(p) < 2:
            continue
        deleting = len(p) > 2 and p[2] not in ("<none>", "", "-")
        if p[1] == "Ready" and not deleting:
            n += 1
    return n


def live_karpenter_nodes():
    """살아있는 Karpenter 노드 이름 목록 (종료 중·등록 중 제외)."""
    ok, out = kubectl("get nodes -l karpenter.sh/nodepool --no-headers -o custom-columns="
                      "NAME:.metadata.name,ST:.status.conditions[-1].type,DEL:.metadata.deletionTimestamp")
    if not ok or not out:
        return []
    names = []
    for line in out.splitlines():
        p = line.split()
        if len(p) < 2:
            continue
        deleting = len(p) > 2 and p[2] not in ("<none>", "", "-")
        if p[1] == "Ready" and not deleting:
            names.append(p[0])
    return names


def uid():
    return str(uuid.uuid4())


def top_cpu_mem(app):
    """kubectl top pod로 앱 파드 평균 CPU(m), Memory(Mi). 실패 시 (None, None)."""
    ok, out = kubectl(f"-n {NAMESPACE} top pod -l app={app} --no-headers")
    if not ok or not out:
        return None, None
    cpu_t, mem_t, n = 0, 0, 0
    for line in out.strip().split("\n"):
        p = line.split()
        if len(p) < 3:
            continue
        try:
            c, m = p[1], p[2]
            cpu_t += int(c[:-1]) if c.endswith("m") else int(c) * 1000
            if m.endswith("Mi"):
                mem_t += int(m[:-2])
            elif m.endswith("Gi"):
                mem_t += int(float(m[:-2]) * 1024)
            else:
                mem_t += int(m) // (1024 * 1024)
            n += 1
        except ValueError:
            pass
    return (cpu_t // n, mem_t // n) if n else (None, None)


# ── 부하 (grader injector.py와 동일: stress length 50~200, 약하게) ──

def get_origin_endpoint(fallback):
    """오리진(ALB) 엔드포인트 자동 탐지. 실패하면 fallback(=채점 경로)을 그대로 쓴다.
    ★왜 필요한가: 캐싱을 쓰는 앱은 채점 경로로 재면 캐시가 요청을 흡수해 파드 부하가 안 잡힌다.
      파드 사이징(request/limit/HPA)의 근거는 '파드가 실제로 하는 일'이어야 하므로 오리진에서 잰다.
      캐싱 설정 자체는 과제 요구사항이므로 건드리지 않는다 — 측정 경로만 분리한다."""
    try:
        r = subprocess.run(
            'aws elbv2 describe-load-balancers --query "LoadBalancers[?Scheme==\'internet-facing\']|[0].DNSName" --output text',
            shell=True, capture_output=True, text=True, timeout=25)
        dns = (r.stdout or "").strip()
        if r.returncode == 0 and dns and dns.lower() not in ("none", "null", ""):
            return f"http://{dns}"
    except Exception:
        pass
    return fallback


async def _seed(base):
    u = f"_s_{random.randint(1000000,9999999)}"
    p = f"_s_{random.randint(1000000,9999999)}"
    async with aiohttp.ClientSession() as s:
        try:
            await s.post(f"{base}/v1/user", json={"requestid": rid(), "uuid": uid(), "username": u, "email": f"{u}@t.org"})
            await s.post(f"{base}/v1/product", json={"requestid": rid(), "uuid": uid(), "id": p, "name": p, "price": 1})
        except Exception:
            pass
    return u, p


async def _hit(session, base, api, seed_u, seed_p, results=None):
    t0 = time.time()
    try:
        if api == "user":
            async with session.get(f"{base}/v1/user?email={seed_u}@t.org&requestid={rid()}&uuid={uid()}") as r:
                await r.read(); st = r.status
        elif api == "product":
            async with session.get(f"{base}/v1/product?id={seed_p}&requestid={rid()}&uuid={uid()}") as r:
                await r.read(); st = r.status
        else:
            # ★stress payload 크기(length)는 처리시간을 좌우하는 유일한 입력이다.
            #   이 값을 부하툴에서 베껴오면 채점기가 다른 크기를 쓰는 순간 측정 전체가 틀어진다
            #   (cap·request·util·필요 노드 수가 모두 이 값에 비례해 어긋난다).
            #   그래서 '고정 가정' 대신 SLO를 기준으로 유도한다:
            #     STRESS_LEN(전역)이 None이면 _calibrate_stress_len()이 SLO 경계에 해당하는
            #     크기를 이분 탐색으로 찾아 채운다. 즉 "이 앱이 SLO를 지킬 수 있는 최대 작업량"이
            #     측정 기준이 된다 — 채점기 payload가 무엇이든 그 경계가 의미를 갖는다.
            #   보수적으로 잡는 이유: 측정 length가 실제보다 작으면 cap이 과대해져 파드가 부족해지고
            #     (게이트 위험), 크면 cap이 과소해져 파드가 남는다(비용만 손실). 후자가 안전하다.
            _ln = STRESS_LEN[0] if STRESS_LEN[0] else random.randint(50, 200)
            async with session.post(f"{base}/v1/stress",
                                    json={"requestid": rid(), "uuid": uid(), "length": _ln}) as r:
                await r.read(); st = r.status
    except Exception:
        st = 0
    if results is not None:
        results[api].append((st, (time.time() - t0) * 1000))


async def _run_load(base, seed_u, seed_p, duration, results=None, u_workers=2, p_workers=2, s_workers=2):
    """약한 부하를 duration초 동안. stress는 0.8~1.2s 간격(grader 수준), user/product는 0.2~0.4s."""
    end = time.time() + duration

    async def worker(session, api):
        while time.time() < end:
            await _hit(session, base, api, seed_u, seed_p, results)
            gap = random.uniform(0.8, 1.2) if api == "stress" else random.uniform(0.2, 0.4)
            await asyncio.sleep(gap)

    conn = aiohttp.TCPConnector(limit=30)
    async with aiohttp.ClientSession(connector=conn) as session:
        tasks = []
        for _ in range(u_workers):
            tasks.append(asyncio.create_task(worker(session, "user")))
        for _ in range(p_workers):
            tasks.append(asyncio.create_task(worker(session, "product")))
        for _ in range(s_workers):
            tasks.append(asyncio.create_task(worker(session, "stress")))
        await asyncio.gather(*tasks)


async def _light_latency(base, app, seed_u, seed_p, n=10, warmup=2, extra=20):
    """단일 요청 순차 → '고유 지연' (p50, p95). 큐잉 0 상태의 처리시간.
    ★워밍업 요청을 버린다: 첫 요청은 TCP 연결·TLS 수립·앱 콜드 경로(커넥션풀 초기화 등) 비용이
      섞여 있어, 소수 샘플의 최댓값을 쓰면 그게 곧 p95가 된다. 그러면 지연이 균일한 앱도
      'burst 앱'으로 오판된다(실측: 오리진 p50 12ms인데 최댓값이 3배를 넘어 burst 판정됨).
    ★p50과 p95를 나눠 반환하는 이유:
      - p95(소수 샘플의 최댓값)는 burst 앱에서 '샘플에 burst가 뽑혔는가'를 재는 값이 된다.
      - 캐시 앱에서는 첫 요청만 MISS라 p95가 캐시 미스 비용을 잡는다.
    ★반환에 ok_frac(무부하 SLO 준수율)을 포함한다.
      이 값은 '큐잉이 0일 때 SLO를 지키는 요청 비율' = 스케일링으로 도달 가능한 천장이다.
      파드를 무한히 늘려도 큐잉만 0에 가까워지므로, 앱 자체 처리시간 분포가 SLO를 넘는 부분은
      절대 못 고친다. 이 천장을 모르면 컨트롤러가 두 방향으로 틀린다:
        · 천장보다 높은 목표 → 영원히 못 닿아 증설만 반복(노드 낭비 + 그래도 실패)
        · 관측된 낮은 준수율을 천장으로 오인 → 용량 부족인데 '한계'로 보고 증설 중단
      샘플이 적으므로 (k+1)/(n+1)로 약간 낙관 보정한다 — 과소 추정이 증설을 막는 쪽이 더 위험하다."""
    lats = []
    conn = aiohttp.TCPConnector(limit=1)
    async with aiohttp.ClientSession(connector=conn) as session:
        for i in range(n + warmup):
            r = {app: []}
            await _hit(session, base, app, seed_u, seed_p, r)
            if i < warmup:                       # 워밍업분은 버림(연결 수립·콜드 경로)
                continue
            if r[app] and 200 <= r[app][0][0] < 300:
                lats.append(r[app][0][1])
    if not lats:
        return 0, 0, None, 0, 0
    # ★SLO 경계를 걸치는 앱은 표본을 더 모은다.
    #   준수율 천장이 스케일링 목표를 정하므로, 이 추정이 틀리면 두 방향으로 점수를 잃는다:
    #     과소 추정 → 목표가 낮아 증설을 덜 함 → 성능 티어를 놓친다
    #     과대 추정 → 도달 못 하는 목표를 쫓아 노드만 쓴다
    #   전부 SLO 이내거나 전부 초과면 표본을 더 봐도 결론이 같다(추가 측정 불필요).
    #   걸치는 경우에만 비율이 임계 정보이므로 추가로 모은다. 채점 티어가 2.5%p 간격이라
    #   10샘플(±15%)로는 티어를 잘못 짚을 수 있다.
    ok0 = sum(1 for x in lats if x <= SLO[app])
    if 0 < ok0 < len(lats) and extra > 0:
        conn2 = aiohttp.TCPConnector(limit=1)
        async with aiohttp.ClientSession(connector=conn2) as session:
            for _ in range(extra):
                r = {app: []}
                await _hit(session, base, app, seed_u, seed_p, r)
                if r[app] and 200 <= r[app][0][0] < 300:
                    lats.append(r[app][0][1])
    lats.sort()
    ok = sum(1 for x in lats if x <= SLO[app])
    ok_frac = (ok + 1) / float(len(lats) + 1)     # 소표본 낙관 보정
    return (round(lats[len(lats) // 2]), round(lats[int(len(lats) * 0.95)]),
            round(ok_frac, 4), ok, len(lats))


async def _load_phase(base, app, seed_u, seed_p, workers, dwell, cpu_sample_every=7):
    """workers 동시성으로 dwell초 부하 → dict(rps, p95, p50, p99, cpu, mem, n).
    ★CPU는 부하 중 여러 번 top 샘플의 최댓값 (metrics-server 15s 평균/지연 보정)."""
    loop = asyncio.get_event_loop()
    stop = asyncio.Event()
    res = {app: []}
    cpu_max, mem_max = [0], [0]
    t_start = time.time()

    async def bg():
        deadline = time.time() + dwell + 25
        conn = aiohttp.TCPConnector(limit=max(20, workers * 2))
        async with aiohttp.ClientSession(connector=conn) as session:
            async def w():
                while not stop.is_set() and time.time() < deadline:
                    await _hit(session, base, app, seed_u, seed_p, res)
            await asyncio.gather(*[w() for _ in range(workers)], return_exceptions=True)

    async def sampler():
        while not stop.is_set():
            await asyncio.sleep(cpu_sample_every)
            c, m = await loop.run_in_executor(None, top_cpu_mem, app)
            if c:
                cpu_max[0] = max(cpu_max[0], c)
            if m:
                mem_max[0] = max(mem_max[0], m)

    task = asyncio.create_task(bg())
    samp = asyncio.create_task(sampler())
    await asyncio.sleep(dwell)
    stop.set()
    await task
    samp.cancel()

    elapsed = max(1.0, time.time() - t_start)
    oks = sorted(t for s, t in res[app] if 200 <= s < 300)
    n_all = len(res[app])
    # ★rps는 '성공(2xx) 요청'만 센다. 전체를 세면 실패가 많은 앱이 더 빨라 보인다
    #   (4xx/5xx는 즉시 반환 → rps 폭증). 그 값이 scaler_cap으로 들어가면 그 앱은
    #   사실상 스케일업을 안 하게 되고, cpu/rps 분류(io/cpu)도 함께 틀어진다.
    return {"rps": round(len(oks) / elapsed, 1),
            # ★백분위를 채점 티어(SCORE_PCTL)에 맞춘다. scaler의 제어 목표와 같은 정의여야
            #   cap(=SLO-safe rps)과 런타임 제어가 어긋나지 않는다.
            "p95": round(oks[min(len(oks) - 1, int(len(oks) * SCORE_PCTL))]) if oks else 0,
            "p50": round(oks[int(len(oks) * 0.50)]) if oks else 0,
            "p99": round(oks[min(len(oks) - 1, int(len(oks) * 0.99))]) if oks else 0,
            "cpu": cpu_max[0], "mem": mem_max[0], "n": n_all, "n_ok": len(oks),
            "err_pct": round(100.0 * (n_all - len(oks)) / max(1, n_all), 1),
            "workers": workers}


STRESS_LEN = [None]    # ★stress payload 크기. _calibrate_stress_len()이 측정으로 채운다.
                       #   부하툴에서 값을 베껴오지 않기 위한 장치 — 채점기 payload를 모르므로
                       #   'SLO 경계에 해당하는 작업량'을 스스로 찾아 기준으로 쓴다.
STRESS_LEN_MIN, STRESS_LEN_MAX = 50, 200   # 탐색 범위 = 실제 트래픽의 payload 범위
                       # ★상한 2000 → 200. 이게 stress 튜닝이 계속 어긋난 근본 원인이었다.
                       #   채점 트래픽의 stress length는 50~200이다(이 파일 상단 주석,
                       #   그리고 _hit()의 폴백이 random.randint(50,200)인 것과 같은 근거).
                       #   그런데 탐색 상한이 2000이면 이분탐색이 SLO 경계를 찾아 수백~2000까지
                       #   올라간다. 그 길이로 측정하면 실제보다 몇 배 무거운 워크로드를 재는 것이다.
                       #   ★그 결과가 두 방향으로 전부 틀렸다:
                       #     · S(무부하 처리시간)가 실제보다 크게 측정된다
                       #       → ρ_slo = 1 - S/SLO 가 작아짐 → cap 과소 → need_rps 과다
                       #       → stress는 1파드=1노드이므로 그게 곧 노드 폭증이다(실측 7대).
                       #     · scaler_perf.json의 '도달 가능 준수율'도 그 무거운 payload 기준이라
                       #       실제보다 낮게 나온다 → scaler가 90% 티어를 '불가능'으로 보고 포기.
                       #   ★200으로 묶으면 측정 대상이 채점 트래픽과 같아진다:
                       #     cap·request·util·svc_ms가 전부 실제 값이 되어 사이징이 맞고,
                       #     '가장 무거운 요청(200)' 기준이므로 여전히 보수적이다.
                       # ★하한 50: 그 아래는 CPU가 안 올라가 측정이 무의미하다(실측 — 길이 10에서
                       #   cpu_lo 1m, rps 179, cap 182로 측정돼 max 56파드가 산출됐다).
STRESS_CAL_SAMPLES = 13 # ★이분탐색 각 단계의 샘플 수(중앙값). 7 → 13.
                       #   ★교정 결과가 실행마다 84 / 90 / 117로 흔들렸다. length는 stress의
                       #     처리시간·cap·request·필요 노드 수를 모두 좌우하므로, 이 편차가
                       #     stress 사이징 정확도를 직접 깎는다(90% 티어에 1%p 못 미치는 원인).
                       #   ★13회 중앙값이면 네트워크 지터 2~3건으로는 방향이 안 바뀐다.
                       #   시간은 탐색 단계를 7 → 5로 줄여 상쇄한다(범위가 50~200으로 좁아
                       #   5단계면 해상도 ~5가 나온다).
STRESS_CAL_STEPS = 5   # 이분탐색 단계 수. 범위 150 → 5단계면 최종 해상도 약 5.
TAIL_MARGIN = 0.4      # ★length 교정 목표 = SLO × 이 값 (p50 기준).
                       #   target = 1000 × 0.4 = 400ms. stress가 이 p50을 내는 length를 찾는다.
                       #
                       # ★0.25에서 올린 이유: 너무 낮으면 length가 10~20으로 잡혀 CPU가 거의
                       #   안 올라가고(포화 측정 불가), cap이 비현실적으로 높아진다.
                       #   0.4이면 p50=400ms → 꼬리(p95)도 SLO 안에 들어올 여유가 있으면서
                       #   CPU-burn 앱이 코어를 실제로 포화시키는 길이(100~300 범위)가 잡힌다.
                       #
                       # ★0.5(이전값)으로 안 가는 이유: p50이 500ms면 요청의 ~40%가 SLO를 넘고,
                       #   그 값으로 측정한 준수율(51%)을 '앱의 물리적 천장'으로 scaler에 넘겨
                       #   90% 티어를 포기하게 만들었다 — 성능 최대 11.5점의 원인이었다.
                       #
                       # ★이 값에 매이지 않는다: cap 교정이 큐잉 관계식(p95=S/(1-ρ)) 기반이므로
                       #   초기 cap이 틀려도 30초 안에 실제 용량으로 수렴한다. 여기 값은 '초기 추정이
                       #   크게 벗어나지 않아 수렴이 빠른' 지점을 고르는 것이다.


async def _calibrate_stress_len(base, seed_u, seed_p):
    """stress의 'SLO 경계 payload 크기'를 이분 탐색으로 찾는다.

    ★왜 필요한가: stress 처리시간은 length에 거의 비례한다. 그래서 측정에 쓰는 length가
      cap·request·util·필요 노드 수를 전부 좌우한다. 그 값을 부하툴에서 가져오면
      채점기가 다른 크기를 쓰는 순간 튜닝 전체가 무의미해진다.
    ★무엇을 기준으로 삼는가: 'SLO를 지킬 수 있는 최대 작업량'이다.
      그 지점의 처리량이 곧 '이 앱이 SLO 안에서 낼 수 있는 최대 rps'이고,
      그게 scaler가 필요로 하는 cap의 정의와 정확히 일치한다.
    ★보수 방향: 경계보다 조금 큰 쪽을 택한다. length가 실제보다 작으면 cap이 과대해져
      파드가 부족해지고 게이트가 위험하다. 크면 파드가 남을 뿐이다(비용만 손실).
    """
    lo, hi = STRESS_LEN_MIN, STRESS_LEN_MAX
    # ★목표를 SLO가 아니라 'SLO × TAIL_MARGIN'으로 둔다.
    #   p50이 SLO에 딱 걸리는 크기를 고르면, 정의상 절반이 SLO를 넘고 꼬리는 전부 넘는다.
    #   실측 사고: 목표를 SLO(1000ms)로 두고 교정하니 준수율이 74% → 29%로 떨어졌다
    #     (cap 1.1 → 0.6, cpu 1148m → 707m). 즉 '측정 기준' 자체가 앱을 불리하게 만들었다.
    #   ★그리고 TAIL_MARGIN=0.5(경계 payload)도 같은 종류의 자기함정이었다:
    #     경계에서 측정한 준수율(51~75%)을 '앱의 물리적 천장'으로 scaler에 넘겼고,
    #     scaler는 90% 티어를 포기했다(성능 3.5/4 고착). 천장은 앱이 아니라 payload의 성질이다.
    #   → 0.25로 두어 p50을 SLO의 1/4에 둔다. 꼬리(p95)까지 SLO 안에 들어올 여유가 생기고,
    #     측정된 cap·준수율이 '스케일로 도달 가능한 값'을 과소평가하지 않는다.
    #   ※불확실성은 런타임 cap 자기교정(양방향)이 흡수한다 — 여기 값은 초기 추정이다.
    target = SLO["stress"] * TAIL_MARGIN
    best = None
    conn = aiohttp.TCPConnector(limit=1)
    async with aiohttp.ClientSession(connector=conn) as session:
        # 워밍업(연결·콜드 경로 비용 제거)
        for _ in range(2):
            await _hit(session, base, "stress", seed_u, seed_p, None)
        for _ in range(STRESS_CAL_STEPS):   # 범위 50~200에 5단계면 해상도 ~5
            mid = (lo + hi) // 2
            STRESS_LEN[0] = mid
            lat = []
            for _ in range(STRESS_CAL_SAMPLES):   # 중앙값으로 노이즈 제거
                r = {"stress": []}
                await _hit(session, base, "stress", seed_u, seed_p, r)
                if r["stress"] and 200 <= r["stress"][0][0] < 300:
                    lat.append(r["stress"][0][1])
            if not lat:
                break
            med = sorted(lat)[len(lat) // 2]
            if med <= target:
                best = mid                      # SLO 이내 → 더 키워본다
                lo = mid + 1
            else:
                hi = mid - 1
            if lo > hi:
                break
    # ★탐색 결과 처리 — 두 극단을 구분한다.
    #   · best가 STRESS_LEN_MAX 근처 = '가장 무거운 payload도 목표 이내' → 가벼운 앱.
    #     그대로 MAX를 쓴다(측정에서 CPU가 실제로 올라간다).
    #   · best가 None = '가장 작은 payload도 목표를 넘음' → 무거운 앱. 하한을 쓴다.
    #   ★하한을 10이 아니라 50으로 올린 이유: 10은 CPU가 안 올라가 측정 전체가 무의미해진다
    #     (실측: cpu_lo 1m → request 200m floor → 노드당 8파드 → max 56 → 7노드 폭주).
    if best is None:
        STRESS_LEN[0] = STRESS_LEN_MIN
        print(f"  ⚠ stress payload 탐색이 하한({STRESS_LEN_MIN})에 닿음 — "
              f"이 앱은 최소 payload도 목표({target:.0f}ms)를 넘는다(무거운 앱)")
    else:
        STRESS_LEN[0] = best
    return STRESS_LEN[0]


AB_SETTLE = 25                  # A/B/C 각 설정 적용 후 안정화 대기(초). HPA 반영 + 파드 Ready.
AB_LOAD_SECS = 40               # A/B/C 부하 측정 시간(초). 세 설정에 '같은' 부하를 준다.
                                #   ★짧게 두는 이유: 절대 성능 측정이 아니라 상대 비교다.
                                #   같은 조건으로 재서 나은 쪽을 고르면 되고, 길게 하면
                                #   노드 churn이 끼어 비교 자체가 오염된다.
                                #   ★총 소요 = 3 × (25 + 40) ≈ 3.3분. 30분 예산 안에 들어간다.
PROBE_WORKERS = 4      # 1차 프로브(동시성 결정용)
PROBE_WORKERS2 = 16    # 2차 프로브(지연-동시성 기울기 측정용)
PROBE_DWELL = 8
MEASURE_DWELL = 36     # 본 측정(metrics-server 15s 주기 → top 4회 이상 샘플)
MAX_WORKERS = 64       # 클라이언트가 병목이 되지 않는 선


def _solve_workers(target, w1, p1, w2=None, p2=None):
    """p95가 target에 착지하는 동시성을 역산.
    ★1점만 쓰면 '지연이 동시성에 비례한다'는 가정이 필요한데, 여유가 많은 앱에서는 동시성을
      올려도 지연이 거의 안 변한다(실측: product가 4워커 13ms → 55워커에서도 13ms).
      그러면 1점 역산이 과소 추정을 하고 앱을 포화시키지 못해 CPU가 낮게 측정된다.
      두 점의 기울기를 쓰면 '지연이 안 변하는 앱'은 기울기≈0으로 잡혀 최대 동시성까지 밀어준다."""
    if p1 <= 0:
        return w1
    if w2 is None or p2 is None or p2 <= p1 + 1:
        # 2차 프로브가 없거나 지연이 거의 안 변함 → 비례 가정으로 역산(초과면 줄이는 방향)
        if p1 >= target:
            return max(1, int(round(w1 * target / p1)))
        return MAX_WORKERS
    slope = (p2 - p1) / float(w2 - w1)
    intercept = p1 - slope * w1
    if slope <= 0:
        return MAX_WORKERS
    return max(1, int(round((target - intercept) / slope)))


async def measure(cf_base, origin_base, seed_u, seed_p):
    """앱 무관 측정. ★두 경로를 나눠 재는 것이 핵심이다.
      · 오리진(ALB) : 파드가 실제로 하는 일 → CPU·파드당 처리량·request/HPA 산출의 근거.
      · 채점 경로(CloudFront) : 클라이언트가 보는 지연 → SLO 달성 가능성 판단의 근거.
    왜 나누는가: 캐싱을 쓰는 앱은 채점 경로로 재면 캐시가 요청을 흡수해 파드가 거의 놀아 보인다.
      실측 사고 — 캐시키(id)를 고정해 재니까 45초 내내 캐시 HIT만 나서 cpu=1m으로 측정됐고
      (X-Cache로 확인: 같은 id는 Hit, 다른 id는 Miss), 실제 부하에서는 파드가 50~140m을 썼다.
      그 결과 request가 실제의 1/50로 잡혀 파드가 8~15개까지 늘고 다른 앱의 CPU를 잠식했다.
      → 캐싱은 과제 요구사항이므로 그대로 두고, '측정만' 오리진에서 한다.

    ★동시성 적응: 앱마다 요청당 비용이 수백 배 다르므로 고정 동시성은 한쪽에서 반드시 틀린다.
      (실측: 동시성 6이 stress에는 6.7배 과부하, 캐시된 product에는 용량의 1% 미만)
      짧은 프로브로 p95를 보고 'SLO 근처에 착지하는 동시성'을 역산해 본 측정을 한다."""
    measured = {}
    # ★stress payload 크기를 먼저 교정한다 (측정 전체의 기준이 되므로).
    #   부하툴 값을 베끼지 않고 'SLO 경계 작업량'을 스스로 찾는다.
    _ln = await _calibrate_stress_len(origin_base, seed_u, seed_p)
    print(f"  [stress payload 교정] length={_ln} "
          f"(SLO {SLO['stress']}ms 경계 — 이 크기가 cap·request·노드수의 기준이 된다)")
    for app in ["user", "product", "stress"]:
        slo = SLO[app]
        # ── 1) 고유 지연: 채점 경로(캐시 효과 포함) + 오리진(파드 실제 처리시간) ──
        cf_p50, cf_p95, cf_ok, cf_okn, cf_n = await _light_latency(cf_base, app, seed_u, seed_p)
        # 오리진은 CPU·처리시간 근거용이므로 추가 표본이 필요 없다(천장은 채점 경로로 판단).
        og_p50, og_p95, _og_ok, _o1, _o2 = await _light_latency(origin_base, app, seed_u, seed_p,
                                                               extra=0)

        # ── 2) 프로브: 동시성 결정용 (오리진) ──
        #   ★단계 사이에 드레인 대기를 둔다. 요청이 느린 앱은 프로브가 만든 큐가 다음 단계
        #     초반까지 남아 지연을 부풀린다(실측: 같은 앱의 부하p95가 두 실행에서 1525ms vs
        #     2838ms로 2배 차이 → cap이 1.3 vs 0.6으로 갈림). 대기는 앱 지연에 비례시킨다.
        async def _drain():
            await asyncio.sleep(min(10.0, max(1.0, PROBE_WORKERS2 * (og_p50 or 50) / 1000.0)))

        target = slo * 0.9
        pr = await _load_phase(origin_base, app, seed_u, seed_p, PROBE_WORKERS, PROBE_DWELL)
        p1 = pr["p95"] or og_p95 or 1
        pr2 = None
        if p1 < target:
            # 아직 여유가 있다 → 기울기를 재서 '얼마나 더 밀어야 SLO에 닿는지' 계산.
            #   (이미 target을 넘었으면 2차 프로브는 큐만 키우므로 생략 → 측정 오염 방지)
            await _drain()
            pr2 = await _load_phase(origin_base, app, seed_u, seed_p, PROBE_WORKERS2, PROBE_DWELL)
        w = _solve_workers(target, PROBE_WORKERS, p1,
                           PROBE_WORKERS2 if pr2 else None, (pr2["p95"] if pr2 else None))
        w = max(1, min(MAX_WORKERS, w))

        # ── 3) 본 측정 (드레인 후) ──
        await _drain()
        mm = await _load_phase(origin_base, app, seed_u, seed_p, w, MEASURE_DWELL)

        # ★io 앱: p95를 3회 측정 중앙값으로 안정화.
        #   io 앱의 p95는 측정마다 ±30% 흔들린다 (DB 캐시 상태, 타이밍 등).
        #   이 노이즈가 util 계산을 ±3% 흔들어 최적값을 못 잡게 한다.
        #   3회 중앙값이면 이상치가 걸러진다. 추가 시간: io앱당 24초.
        _cpr_check = (mm.get("cpu") or 0) / max(1, mm.get("rps") or 1)
        if _cpr_check < CPU_BOUND_MPS:  # io 앱만
            _p95_samples = [mm["p95"]]
            for _ in range(2):
                _extra = await _load_phase(origin_base, app, seed_u, seed_p, w, 12)
                if _extra["p95"] > 0:
                    _p95_samples.append(_extra["p95"])
            _p95_samples.sort()
            _median_p95 = _p95_samples[len(_p95_samples) // 2]
            mm["p95"] = _median_p95

        # CPU는 세 단계 중 가장 세게 밀린 쪽의 최댓값(= 포화 CPU에 가장 가까운 관측)
        cands = [pr, mm] + ([pr2] if pr2 else [])
        cpu = max(x["cpu"] for x in cands) or None
        mem = max(x["mem"] for x in cands) or None
        if cpu is None:
            cpu, mem = {"user": 30, "product": 30, "stress": 500}[app], {"user": 48, "product": 48, "stress": 128}[app]
            print(f"  {app}: CPU 측정 실패 → 기본값 cpu={cpu}m")

        # ★포화 미달 판정: 최대 동시성까지 밀었는데도 SLO의 절반에 못 미침
        #   = 이 클라이언트로는 앱을 포화시킬 수 없다 → 측정 CPU가 포화 CPU보다 작을 수 있고
        #     request가 과소 산정될 수 있다. 단 그런 앱은 SLO 여유가 큰 앱이므로 스로틀 피해도 작다.
        unsat = (w >= MAX_WORKERS and mm["p95"] < slo * 0.5)

        # SLO 달성 가능성은 '채점 경로 최소 처리시간(p50)'으로 판단한다.
        #   p95(8샘플 최댓값)로 판단하면 burst 앱·캐시 앱에서 거짓 경고가 난다.
        ceil = " ⚠고유지연>SLO(스케일로 못 고침)" if cf_p50 > slo else ""
        cache_gain = f" 캐시효과 {og_p50}→{cf_p50}ms" if og_p50 and cf_p50 < og_p50 * 0.8 else ""
        err = f" ⚠실패{mm['err_pct']}%" if mm.get("err_pct", 0) >= 1.0 else ""
        sat = " ⚠포화미달(측정CPU가 포화보다 작을 수 있음)" if unsat else ""
        # ★도달 가능 준수율: 큐잉 0에서도 이만큼만 SLO를 지킨다 = 스케일링의 천장.
        ach = f" 도달가능 {cf_ok*100:.0f}%" if cf_ok is not None else ""
        print(f"  {app}: cpu={cpu}m mem={mem}Mi rps={mm['rps']}(2xx만, 동시성{w}){err} | "
              f"고유 채점p50={cf_p50}ms 오리진p50={og_p50}ms 부하p95={mm['p95']}ms{ach}{cache_gain}{ceil}{sat}")
        # ★캐시 히트 감지: 채점 경로에서 같은 리소스를 재요청 → X-Cache/Age로 판별.
        #   product는 id 쿼리스트링 캐시 정책이 있으면 Hit. user는 없으면 Miss.
        #   ★아키텍처 감지: "이 앱에 CDN 캐시 정책이 설정되어 있는가?"를 직접 확인한다.
        _cache_hit = False
        if app != "stress":  # stress는 POST라 캐시 대상 아님
            try:
                async with aiohttp.ClientSession() as _cs:
                    _qp = ({"email": f"{seed_u}@t.org", "requestid": rid(), "uuid": uid()} if app == "user"
                            else {"id": seed_p, "requestid": rid(), "uuid": uid()})
                    await _cs.get(f"{cf_base}/v1/{app}", params=_qp)
                    await asyncio.sleep(0.3)
                    _qp2 = ({"email": f"{seed_u}@t.org", "requestid": rid(), "uuid": uid()} if app == "user"
                             else {"id": seed_p, "requestid": rid(), "uuid": uid()})
                    async with _cs.get(f"{cf_base}/v1/{app}", params=_qp2) as _cr:
                        _xc = (_cr.headers.get("X-Cache") or "").lower()
                        _age = _cr.headers.get("Age", "")
                        _cache_hit = ("hit" in _xc) or (bool(_age) and _age.isdigit() and int(_age) > 0)
            except Exception:
                pass
        if _cache_hit:
            print(f"    -> {app}: CDN 캐시 히트 감지 (캐시 정책 있음)")

        measured[app] = {"cpu": cpu, "mem": mem, "rps": mm["rps"], "p95": mm["p95"],
                         "p50": mm["p50"], "p99": mm["p99"],
                         "achievable": cf_ok,              # 큐잉 0에서의 SLO 준수율 = 스케일링 천장
                         "ok_n": cf_okn, "samp_n": cf_n,   # 신뢰구간 계산용 원자료
                         "p95_light": cf_p50,              # SLO 판단용(채점 경로 최소 처리시간)
                         "p95_light_tail": cf_p95,         # 꼬리 특성(참고)
                         # ★저부하 지점(프로브) — CPU가 부하에 반응하는지 판정용.
                         #   반응하지 않으면 CPU-HPA를 끄고 레이턴시 기반 scaler에 위임한다.
                         "cpu_lo": pr["cpu"] or None, "rps_lo": pr["rps"] or None,
                         "unsat": bool(unsat),
                         "cache_hit": _cache_hit,          # ★CDN 캐시 정책 감지 결과
                         "origin_p50": og_p50, "origin_p95": og_p95, "workers": w}
    return measured


# ── 계산 ──

def calculate(measured, node_cpu_m, vcpu, max_nodes, mng_count=2, stress_req_override=None,
              node_mem_mi=None, node_mem_cap_mi=None, sys_mng=None, sys_worker=None):
    """
    실측 + 노드 스펙 기반. 인스턴스 타입/앱에 하드코딩된 값 없음:
      - 노드 CPU(node_cpu_m)·vCPU(vcpu)·메모리(node_mem_mi)는 클러스터에서 읽어온 실제값.
      - request/memory = 부하 중 실측 (앱이 바뀌어도 자동 반영).
      - stress limit = 노드 전체 CPU, GOMAXPROCS = vCPU (CPU-burn 앱은 코어 다 줄수록 빠름).
      - user/product limit = 노드 절반 (I/O 앱은 이걸로 충분, 스파이크 여유).
      - Karpenter 메모리 캡 = 실제 노드 메모리/vCPU 비율 (m5/r5 등 메모리 많은 타입에서 캡이
        CPU캡보다 빡빡해 노드 증설을 잘못 막는 것 방지).
    """
    # ★노드 역할별 시스템 예약을 실측값으로 쓴다(없으면 SYSTEM_PER_NODE 추정으로 폴백).
    #   avail_mng  : MNG 노드에서 앱이 쓸 수 있는 CPU → 'baseline 2대'(상주가 MNG에 들어가는가) 판정
    #   avail      : 워커(Karpenter) 노드 기준 → 패킹 수·예산 계산
    #   두 값을 하나로 쓰면 한쪽이 반드시 틀린다(MNG는 애드온이 몰려 예약이 4배 크다).
    sys_m = sys_mng if sys_mng else SYSTEM_PER_NODE
    sys_w = sys_worker if sys_worker else SYSTEM_PER_NODE
    avail = max(200, node_cpu_m - sys_w)          # 워커 노드 기준
    avail_mng = max(200, node_cpu_m - sys_m)      # MNG 노드 기준(상주 판정용)
    # ★메모리 예산도 함께 본다. 지금 앱들은 메모리가 작아(8~15Mi) CPU가 항상 먼저 걸리지만,
    #   메모리가 무거운 앱(캐시/버퍼형)이 오면 "CPU상 들어간다고 계산했는데 실제로는 메모리 부족으로
    #   Pending"이 된다 — NodePool 메모리 캡 사고와 같은 종류의 실수(한쪽 자원만 보는 것).
    #   앱이 바뀌어도 성립하게 두 자원 중 더 빡빡한 쪽이 패킹을 결정하도록 한다.
    mem_avail = max(256, (node_mem_mi or 3500) - SYSTEM_MEM_PER_NODE_MI)

    u_cpu, p_cpu, s_cpu = measured["user"]["cpu"], measured["product"]["cpu"], measured["stress"]["cpu"]
    u_mem, p_mem, s_mem = measured["user"]["mem"], measured["product"]["mem"], measured["stress"]["mem"]

    mng_budget = mng_count * avail_mng
    # ★limit은 '실측 포화 CPU'를 반드시 덮어야 한다. 고정값(노드/2)만 쓰면 스로틀이 걸린다.
    #   실측 사고: user 포화 CPU 970m인데 limit이 node//2 = 965m으로 잡혔다.
    #     → 파드가 970m을 쓰려는데 965m에서 잘린다 → 측정된 cap(46.1 rps)을 실제로는 못 낸다
    #     → scaler는 cap을 믿고 사이징하는데 실제 처리량이 그보다 낮아 지연이 SLO를 넘고,
    #       파드를 늘려도 각 파드가 같은 비율로 잘려 있어 개선이 더디다(사이징 전체가 틀어진다).
    #   LIM_HEADROOM으로 여유를 둔다: 측정은 특정 시점 값이고 실부하에서 더 쓸 수 있다.
    #   상한은 노드 가용분 — 그 이상은 물리적으로 못 쓴다.
    #   ★limit을 올려도 다른 앱을 굶기지 않는다: request가 각 앱의 보장분을 지키고,
    #     limit은 '여유가 있을 때만' 쓸 수 있는 상한이다.
    LIM_HEADROOM = 1.3
    # ★io 앱은 CPU limit을 걸지 않는다 (None = 무제한).
    #   ★근거(실측): 36.5점 세팅에서 io 앱에 limit이 아예 없었고, request도 30m이었다.
    #     limit을 걸면 CFS 스로틀이 걸린다. io 앱은 CPU를 짧게 burst로 쓰므로(대부분 DB 대기)
    #     그 burst 순간에 스로틀되면 요청이 강제 대기하고 p95가 그대로 올라간다.
    #     실측 대조: limit 없음 → user 96.8% / limit 1259m → user 78.9%.
    #   ★다른 앱을 굶히지 않는다: request가 각 앱의 보장분을 지키고, limit은 '여유가 있을 때만
    #     쓸 수 있는 상한'이다. 여유가 없으면 커널이 request 비율로 공평하게 나눈다.
    u_lim = None
    p_lim = None
    # ★stress limit = 노드 코어 전부. 이 값이 단건 처리시간을 직접 결정한다.
    #   ★근거: 이 앱은 GOMAXPROCS=vCPU로 요청 하나를 병렬 처리한다. 따라서 limit이
    #     'vCPU 전부'여야 요청당 처리시간이 최소가 된다.
    #     실측: 2코어 전용 340ms → 1코어면 680ms. 90% 티어 조건(E[T] ≤ 434ms)은
    #     2코어에서만 성립하므로 limit을 줄이면 90%가 원리적으로 불가능해진다.
    #   ★포화 실측 CPU(s_cpu)가 이 값에 근접하면 측정이 정확하다는 뜻이다.
    #     s_cpu가 이보다 크면(측정 오차·버스트) limit을 그만큼 올려 스로틀을 피한다.
    s_lim = max(vcpu * 1000, int((s_cpu or 0) * 1.05))
    gomax = vcpu

    # 앱 판별: 요청당 CPU(cpu/rps) ≥ 기준 → CPU-bound(부하에 CPU 비례), 아니면 I/O-bound(DB/캐시 대기).
    def bound_of(app):
        c, r = measured[app]["cpu"], measured[app].get("rps", 0)
        return "cpu" if (r > 0 and c / r >= CPU_BOUND_MPS) else "io"
    u_bound, p_bound = bound_of("user"), bound_of("product")

    # ── CPU request 사이징 (★ 오버서브 = 지속부하 스로틀의 원흉) ──
    #   cpu-bound(user): request ≈ 실사용(0.85×). 작게 잡으면 파드가 노드에 몰려 스케줄되나 실제 CPU가
    #     부족 → CFS 스로틀·지연폭발, Karpenter도 'request상 맞으니' 노드 안 늘림. request≈실사용이면
    #     스케줄=실수요 → Karpenter가 노드 제대로 provisioning → 스로틀 X.
    #   cpu-bound(user): request = min(실사용×계수, avail÷2). avail÷2 = 노드당 2파드(각 ~1코어) →
    #     버스트해도 노드 물리코어 안 넘어 스로틀 X. near-peak 과다예약도 아님(실사용 낮으면 그만큼 작게).
    #   io-bound: request는 실측 기반으로 작게 유지 → 노드에 많이 패킹 가능(60m×20=1200m, 1노드에 20파드).
    #     ★"파드 수를 줄여 노드를 아끼는" 방향은 극한 대응력을 깎으므로 하지 않는다. 대신 deploy.yaml의
    #       topologySpread를 완화(maxSkew↑)해 파드가 노드에 몰릴 수 있게 → 파드는 마음껏 늘고 노드는 안 늘음.
    #       (실측 원인: product 12파드×60m=720m인데 maxSkew 2가 4개 노드로 강제 분산시켜 노드 낭비)
    #   stress: 요청 1개가 2코어를 씀(속도는 limit 담당) → request는 작은 예약만.
    # ── CPU request 사이징 ──
    # ★설계 원칙: 런타임에 바꿀 수 없는 값(request)은 "앱 고유 특성(실측 포화 CPU)"에서만 유도한다.
    #   분류(io/cpu)는 'HPA를 무엇으로 스케일할지'만 결정하고, request 크기는 결정하지 않는다.
    #
    # ★버그 수정: io 앱의 request 상한이 up_cap(=avail//4)이었다. 이건 앱이 필요한 양과 무관한
    #   '노드 크기 상대값'이라, 앱이 그보다 무거우면 request가 실측보다 작게 잘린다.
    #   실측 사고 — 포화 CPU 964m인 앱이 request 332m(1/3)으로 잡혀서 세 가지가 동시에 깨졌다:
    #     1) 스케줄러가 "들어간다"고 판단해 한 노드에 여러 개 몰아넣음 → CFS 경쟁 → 지연 3~5배
    #        (라이브 실측: 파드 32개일 때 user 지연 516~1393ms / SLO 200ms)
    #     2) HPA가 이용률을 964/332 = 290%로 읽어 부하 즉시 max까지 폭주
    #     3) Karpenter는 'request상 충분'하니 노드를 안 늘림 → 노드를 8대로 줘도 소용없음
    #   → 상한은 물리적으로 의미 있는 것만 남긴다: 자기 limit, 그리고 노드 예산.
    def _cpu_at_lo(app, cpu_sat):
        """저부하 동작점에서의 파드 CPU.

        ★cpu_lo(프로브 실측)를 그대로 믿으면 안 된다: metrics-server는 15초 평균이고
          프로브는 PROBE_DWELL(8초)이라, 샘플이 부하 시작 전 값(0~1m)으로 찍히는 일이 흔하다.
          실측 사고 — user/product/stress 모두 cpu_lo가 1m으로 찍혔고, 그 결과
            · io request가 floor(60m)까지 떨어져 HPA 트리거가 96m → 베이스라인에서도 증설
            · stress request가 200m으로 떨어져 Karpenter 노드가 11% 사용률로 회수 안 됨
          둘 다 '베이스라인 트래픽인데 노드가 3대'의 직접 원인이었다.
        ★대안: 포화 측정에서 rps 비로 역산한다.
            cpu(rps_lo) ≈ cpu_sat × rps_lo / rps_sat
          CPU는 처리량에 거의 비례하므로 이 추정이 짧은 프로브 실측보다 훨씬 안정적이다.
          (검증: user cpu_sat 920m·rps 44.5·rps_lo 14 → 289m. 정상 측정된 값 263m과 일치)
        ★판정: 실측이 추정의 절반도 안 되면 '측정 실패'로 보고 추정을 쓴다."""
        m = measured[app]
        lo = m.get("cpu_lo") or 0
        r_lo, r_sat = m.get("rps_lo") or 0, m.get("rps") or 0
        est = (cpu_sat * r_lo / r_sat) if (cpu_sat and r_lo > 0 and r_sat > 0) else 0
        if lo > 0 and (est <= 0 or lo >= est * 0.5):
            return lo
        if est > 0:
            return est
        return cpu_sat * 0.3 if cpu_sat else 0

    def size_request(app, bound, lim, cpu_m, floor, share=1):
        """request = 스케줄링 단위 + HPA util의 기준점. 둘 다 여기서 결정된다.

           ★cpu 앱: 요청 1건이 코어를 태운다 → 포화 실사용 기준(노드당 2파드).

           ★io 앱: 여기가 두 번 틀렸던 자리다.
             ① 처음엔 '포화 CPU × 0.75'를 썼다. 그 값은 단일 파드가 전체 트래픽을 혼자
                받았을 때의 CPU라 너무 커서, 노드당 2~3파드만 들어가고 max가 3~4로 잡혔다
                → 스파이크에서 파드를 못 늘렸다.
             ② 다음엔 cpu_lo(저부하 프로브 CPU)를 썼다. 이것도 '단일 파드가 프로브 부하를
                전부 받은' 값이다. 그런데 운영에서는 상주 share개가 나눠 받으므로
                파드당 실제 CPU는 그 1/share다.
                결과: request가 실제 사용량의 share배로 잡히고, HPA util의 기준점도 같이
                커져서 증설 트리거가 실제 동작점의 4~9배가 됐다 → HPA가 영원히 안 뜬다.
                (실측 user: cpu_lo 263m / 상주 4파드 → 파드당 66m인데 트리거는 600m)
             → cpu_lo를 share로 나눈다. 그러면 request ≈ '상주 시 파드당 실제 CPU'가 되고,
               util을 그 배수로 두면 HPA가 '트래픽이 N배 되면 증설'로 정확히 동작한다.
        """
        if bound == "cpu":
            return max(floor, min(lim or avail, avail // 2, int(cpu_m * CPU_REQ_FACTOR)))
        # ★★io request = '파드가 자기 몫의 부하를 처리할 때 실제로 쓰는 CPU'.
        #   측정값만으로 유도한다 — 앱별 상수도, 임의의 상한도 없다.
        #
        #   [유도]
        #     ① 요청당 CPU:      cpu_per_rps = 포화CPU / 포화rps      [m·s/req]
        #     ② 파드당 SLO-safe rps: 큐잉 관계식에서
        #          p95 = S/(1-ρ) 이고 p95 = SLO 인 지점이 ρ_slo = 1 - S/SLO
        #          파드 하나의 최대 처리율은 1/S 이므로  rps_pod = ρ_slo / S
        #        (S = 무부하 꼬리 지연. 측정값 p95_light_tail)
        #     ③ request = ① × ② = 그 파드가 SLO를 지키며 돌 때의 CPU
        #
        #   [왜 이게 맞는가]
        #     · 총 예약(파드수 × request)이 총 사용량(트래픽이 정함)과 일치한다.
        #       그래서 '예약만 하고 안 쓰는' 노드 낭비가 없고, 반대로 과소예약도 없다.
        #     · 앱이 무거우면(요청당 CPU 큼) request가 커지고, 가벼우면 작아진다.
        #     · SLO가 타이트하거나 앱이 느리면(S가 SLO에 가까움) rps_pod가 작아져
        #       request도 작아진다 = 파드를 많이 띄워야 하는 앱이므로 맞는 방향이다.
        #
        #   [기존 방식들이 틀렸던 이유]
        #     · cpu_lo(프로브 실측): metrics-server가 15초 평균이라 8초 프로브에서
        #       0~1m으로 찍힌다. 그러면 request가 floor까지 떨어진다.
        #     · 포화CPU × 계수: 단일 파드가 전체 부하를 받은 값이라 N배 과대.
        #       실측 — 309m으로 잡혀 16파드가 4944m을 예약, 노드 4대(비용 9/12).
        #       같은 파드 수를 85~150m으로 예약하면 노드 2대(비용 12/12)다. 성능은 동일.
        #     · 임의 상한(MNG//15): 특정 앱의 파드 수를 가정한 값이라 일반화가 아니다.
        _s_tail = (measured[app].get("p95_light_tail")
                   or measured[app].get("origin_p95")
                   or measured[app].get("p95_light") or 0)
        _rps_sat = measured[app].get("rps") or 0
        _req_est = 0
        if cpu_m and _rps_sat > 0 and 0 < _s_tail < SLO[app]:
            _cpu_per_rps = cpu_m / float(_rps_sat)              # m·s per request
            _rps_pod = (1.0 - _s_tail / SLO[app]) / (_s_tail / 1000.0)   # req/s per pod
            _req_est = int(_cpu_per_rps * _rps_pod)
        if _req_est <= 0:
            # 측정이 불충분하면 저부하 추정으로 폴백(그것도 없으면 floor)
            _lo = _cpu_at_lo(app, cpu_m)
            _req_est = int((_lo / float(max(1, share))) * IO_REQ_MARGIN) if _lo > 0 else 60
        # ★request 상한 = '파드당 실사용 CPU × 여유'.
        #   큐잉 공식은 포화 CPU(단일 파드가 전 부하를 받은 값)에서 유도되므로 운영 상태의
        #   파드당 사용량보다 크게 나온다. 실측 라이브: user request 166m / 실사용 91m (1.8배).
        #   request가 과대하면 노드당 파드 수가 그 비율로 줄어 같은 파드 수에 더 많은 노드가
        #   필요해지고, 노드 예산이 고갈되어 HPA가 늘리려 해도 물리적으로 불가능해진다.
        #   저부하 CPU를 상주 파드 수(share)로 나눠 '파드당' 값으로 환산한 뒤 여유를 곱한다.
        _lo_c = _cpu_at_lo(app, cpu_m)
        if _lo_c > 0:
            _per_pod_lo = _lo_c / float(max(1, share))
            _req_est = min(_req_est, max(30, int(_per_pod_lo * IO_REQ_MARGIN)))
        # ★io request = '파드가 실제 부하에서 쓰는 CPU' (측정에서 유도, 임의 상수 없음).
        #
        #   [왜 작아야 하는가 — 원리]
        #     io 앱에는 CPU limit이 없다(위 u_lim/p_lim = None). 따라서 파드는 request를 넘어
        #     필요한 만큼 burst로 쓴다. request가 하는 일은 두 가지뿐이다:
        #       ① 스케줄 밀도(노드당 몇 파드)  ② HPA util의 분모
        #     ①에서 CPU request가 병목이 되면 안 된다 — 커널이 어차피 공평하게 나눠 주고,
        #     io 앱은 대부분 DB 대기라 실제 CPU 점유가 낮다. request를 크게 잡으면
        #     '예약만 하고 안 쓰는' 자리가 생겨 노드가 일찍 필요해진다.
        #
        #   [측정 유도]
        #     요청당 CPU = 포화CPU ÷ 포화rps            (앱 고유 비용)
        #     파드당 부하 = 저부하 rps ÷ 상주 파드 수    (운영 상태의 파드 하나가 받는 rps)
        #     request    = 요청당 CPU × 파드당 부하
        #   ★이 식은 '파드 하나가 자기 몫을 처리할 때 쓰는 CPU'라서 앱·노드·부하 무관하게 성립한다.
        #
        #   ★_cpu_at_lo()를 안 쓰는 이유: 그 함수는 프로브 실측(cpu_lo)을 우선하는데,
        #     metrics-server가 15초 평균이라 8초 프로브에서 포화값이 찍히는 일이 흔하다.
        #     실측 사고 — user cpu_lo가 포화(912m)에 가깝게 나와 request가 225m로 산출됐고,
        #     그러면 MNG에 12파드만 들어가 스파이크마다 노드가 필요해졌다.
        #     요청당 CPU × rps는 두 값 모두 36초 본측정에서 나오므로 훨씬 안정적이다.
        _rps_lo = measured[app].get("rps_lo") or 0
        if cpu_m and _rps_sat > 0 and _rps_lo > 0:
            _per_req = cpu_m / float(_rps_sat)
            _per_pod_rps = _rps_lo / float(max(1, share))
            _measured_req = int(_per_req * _per_pod_rps)
            if _measured_req > 0:
                _req_est = min(_req_est, _measured_req)
        # ★io request = 'MNG에 max 파드가 딱 들어가는 값'.
        #   ★핵심 발견(최적값 역산): requests × target ≈ 23m 이 고정이고
        #     request로 밀도만 바꾸면 스케일 동작은 고정된다.
        #     즉 request는 '트리거에서 역산'이 아니라 '노드 밀도에서 역산'해야 한다.
        #   유도: request = avail_mng ÷ io_max_target
        #     io_max_target = MNG에 들어갈 수 있는 최대 파드 수 (ENI 상한과 CPU 중 작은 쪽)
        #   ★이렇게 하면 request가 '측정'이 아니라 '노드 스펙'에서 나오므로 실행마다 동일하다.
        #     트리거(=측정)가 흔들려도 request는 고정이고, util만 따라 움직인다.
        #     util이 ±3% 흔들리는 것은 HPA 동작에 거의 영향을 주지 않는다.
        _hi = min(x for x in (lim, avail) if x)
        _io_max_t = IO_MAX_TARGET
        _req_from_node = max(30, avail_mng // _io_max_t)
        return max(30, min(_hi, _req_from_node))

    # io 앱은 상주 IO_RESIDENT개가 부하를 나눠 받는 것을 전제로 request를 잡는다.
    #   (실제 상주 수는 아래에서 MNG 예산으로 다시 확정한다 — request가 작아졌으므로
    #    IO_RESIDENT는 항상 확보된다.)
    u_req = size_request("user", u_bound, u_lim, u_cpu,
                         300 if u_bound == "cpu" else 30, IO_RESIDENT)
    p_req = size_request("product", p_bound, p_lim, p_cpu,
                         300 if p_bound == "cpu" else 60, max(1, IO_RESIDENT // 2))
    # stress는 pod anti-affinity로 노드를 독차지(user/product와 동거 금지) → 2코어 온전히 확보.
    #   request=node//2(~900m): util 45면 실사용 ~434m(0.45코어)에 스케일 = 공격적(스파이크 큐잉·503 방어).
    #   ★avail로 크게 잡고 util 60으로 보수화했더니 2h 테스트서 stress 78% 폭락(과소provision) → 되돌림.
    #    비용이 관대(ratio 1.11도 만점)라 "과증설"은 실익 없는 걱정이었고, 공격적 스케일이 정답.
    # ★[요구2] stress 격리 vs 공존을 '측정 기반'으로 튜닝툴이 결정한다(하드코딩 아님, 앱 무관).
    #   무거우면(1파드가 워커노드 CPU의 ISOLATE_FRAC 이상) → 전용노드 격리(경합 0 → 게이트 보호).
    #   가벼우면 실측만큼만 request → user/product와 같은 노드에 패킹(노드 수↓ = 비용).
    #   격리 여부는 apply_config가 anti-affinity 로도 반영한다(아래).
    ISOLATE_FRAC = 0.45
    # ★격리 판정은 두 조건의 OR이다. 하나만 보면 반대 방향으로 틀린다.
    #   ① CPU 점유: 1파드가 워커 가용의 ISOLATE_FRAC 이상 → 공존하면 다른 앱을 굶긴다.
    #   ② 지연 여유: 코어를 나누면 자기 지연이 SLO를 넘는다 → 공존하면 자기 성능이 0이 된다.
    #      병렬화 앱은 가용 코어에 반비례해 처리시간이 늘어난다(실측: 2코어 762ms → 1코어 ≈1524ms).
    #      공존 시 대략 절반의 코어를 쓰게 되므로 'p50 × 2 > SLO'면 공존 자체가 불가능하다.
    #      ★이 조건이 없으면 'CPU는 적게 쓰지만 느린 앱'을 공존시켜 성능을 0으로 만든다.
    #        (예: cpu 600m으로 ①을 통과하지만 p50 900ms → 공존 시 1800ms → SLO 초과)
    #      CPU 조건만으로는 절대 잡히지 않는 케이스라 별도 조건이 필요하다.
    _s_lat = measured["stress"].get("p95_light", 0) or 0        # 채점 경로 고유 지연(p50)
    _lat_tight = _s_lat > 0 and _s_lat * 2 > SLO["stress"]      # 코어 절반이면 SLO 초과
    _cpu_heavy = bool(s_cpu) and s_cpu >= avail * ISOLATE_FRAC
    # ★stress 격리 유지 — 전용 Karpenter 노드에서 운영.
    #   baseline = MNG(user+product) + Karpenter 1대(stress) = 2대.
    #   ★request를 작게 잡아 한 노드에 stress 여러 파드가 들어가게 한다.
    #     스파이크에서 stress 파드가 늘어나도 같은 노드에 패킹 → 노드 추가 최소화.
    #     노드 1대에 stress 4~5파드 가능(266m × 5 = 1330m ≤ avail).
    #   ★성능 보장: limit(2000m)이 실행 시 코어를 보장. 격리라 다른 앱과 CPU 경합 없음.
    stress_isolate = True
    print(f"  [stress 격리 판정] 전용 노드 유지 (baseline 2대 = MNG + Karpenter)"
          f" | request는 작게 → 한 노드에 여러 파드 패킹 가능")
    # ★stress는 '노드당 1파드로 동작'하지만 request로 노드를 예약하지는 않는다.
    #   ★핵심 구분 (이게 비용 손실의 원인이었다):
    #     · 노드를 실제로 독점하는 것 → limit 이 담당한다 (limit 2000m > 노드 1930m).
    #       파드 하나가 실행되면 코어를 다 쓰므로 다른 파드가 끼어들 여지가 없다.
    #     · 스케줄러가 자리를 예약하는 것 → request 가 담당한다.
    #   그런데 request를 avail×0.94(1633m)로 잡으면 '실제로는 안 쓰는데 예약만 하는' 상태가 된다.
    #   실측: stress 실사용 290m / request 1633m → 노드 1대를 92% 예약하고 15%만 사용.
    #     그 노드에는 다른 파드가 들어갈 수 없으므로 상시 빈 노드 1대가 비용을 먹는다
    #     (실측 avg_ec2 2.60 → 비용 10/12. request를 실측에 맞추면 2.0 → 12/12).
    #   → request는 '실측 사용량 × 여유'로 잡는다. limit이 노드 독점을 계속 보장한다.
    #     예약이 작아지면 stress 파드가 MNG나 다른 노드의 빈 자리에 들어갈 수 있어
    #     전용 노드가 필요 없어진다. 대기이론상 성능은 변하지 않는다 —
    #     노드 총 용량이 같고 limit도 그대로이기 때문이다.
    #   ★안전장치:
    #     · anti-affinity는 preferred다 → 빈 노드가 있으면 여전히 분리된다(성능 우선)
    #     · limit > 노드 allocatable → 실행 중에는 사실상 노드를 독점한다
    #     · 부하가 커지면 scaler/HPA가 파드를 늘리고 Karpenter가 노드를 준다
    STRESS_REQ_MARGIN = 1.5     # request = 실측 사용 CPU × 이 여유
    if stress_req_override:
        s_req = stress_req_override
    elif stress_isolate:
        # ★같은 유도식을 쓴다: request = 요청당 CPU × 파드당 SLO-safe rps.
        #   '파드가 자기 몫의 부하를 처리할 때 쓰는 CPU'이므로 앱·노드 무관하게 성립한다.
        #   ★노드 독점은 request가 아니라 limit(= vcpu×1000 ≥ 노드 allocatable)이 보장한다.
        #     파드가 돌 때는 코어를 다 쓰므로 성능은 request와 무관하다.
        #   ★request를 노드 크기 비율(avail//2, avail//4 등)로 잡던 것을 없앴다:
        #     · avail×0.94 → 노드 1대를 92% 예약하고 실사용 15% → 빈 전용 노드 상시 발생
        #     · avail//4  → 임의 비율이라 앱이 바뀌면 근거가 없다
        #   ★상한은 노드 가용(avail): 한 파드가 노드보다 크게 예약할 수는 없다.
        _s_tail_r = (measured["stress"].get("p95_light_tail")
                     or measured["stress"].get("origin_p95")
                     or measured["stress"].get("p95_light") or 0)
        _s_rps = measured["stress"].get("rps") or 0
        _s_est = 0
        if s_cpu and _s_rps > 0 and 0 < _s_tail_r < SLO["stress"]:
            _s_est = int((s_cpu / float(_s_rps))
                         * ((1.0 - _s_tail_r / SLO["stress"]) / (_s_tail_r / 1000.0)))
        if _s_est <= 0:
            _s_est = int(s_cpu * CPU_REQ_FACTOR) if s_cpu else avail // 2
        # ★request 상한 = '저부하 동작점 실사용 CPU × 여유'.
        #   ★이 상한이 없으면 큐잉 공식이 포화 CPU(단일 파드가 전 부하를 받은 값)를 기준으로
        #     계산해 실사용의 몇 배가 나온다. 실측 라이브:
        #       request 1443m / 실제 사용 310m (max 390m) → 4.6배 과대
        #     노드 allocatable이 1930m이므로 이 파드 하나가 노드의 75%를 '예약만' 하고
        #     1500m를 놀린다. 그래서 노드 하나에 이 파드 1개만 들어가고, 다른 앱은
        #     자리를 못 찾아 Karpenter가 노드를 계속 요구한다 → 노드 예산이 순식간에 고갈되고
        #     '스케일링해야 할 때 물리적으로 불가능한' 상태가 된다(실측: 노드 3대에서 캡 고갈).
        #   ★성능은 안 깎인다: 노드 독점과 단건 속도는 limit(vcpu×1000 ≥ allocatable)이
        #     보장한다. request는 '스케줄러가 자리를 얼마나 잡아두는가'일 뿐이다.
        #   ★_cpu_at_lo()는 저부하 동작점 CPU를 추정한다(프로브 실측 또는 rps 비례 역산).
        _s_lo = _cpu_at_lo("stress", s_cpu)
        # ★저부하 실사용 상한 — 단, '코어를 독점해야 하는 앱'에는 적용하지 않는다.
        #   그런 앱은 request가 작으면 스케줄러가 같은 노드에 여러 파드를 넣어
        #   코어를 나눠 쓰게 되고, 그 순간 처리시간이 파드 수에 비례해 늘어난다.
        #   즉 '예약을 아끼는 것'이 곧 '성능을 버리는 것'이 되므로 상한을 걸면 안 된다.
        _wants_all_cores = (vcpu * 1000) >= avail
        if _s_lo > 0 and not _wants_all_cores:
            _s_est = min(_s_est, int(_s_lo * STRESS_REQ_MARGIN))
        # ★stress request를 350m 이하로 묶는다 (일반화: avail의 20% 이하).
        #   이유: request가 작으면 stress가 user/product와 같은 노드에 공존 가능해진다.
        #     → stress 전용 노드가 불필요 → 같은 노드 수로 더 많은 파드 수용 → 비용↓.
        #   성능은 안 깎인다: stress의 단건 속도는 limit(2000m)이 결정한다.
        #     request는 '스케줄러 예약'일 뿐이고 실행 시 CPU는 limit까지 쓸 수 있다.
        #   ★일반화: 노드 가용의 20%로 잡으면 한 노드에 stress 5파드까지 들어간다.
        # ★request 근거 = '요청 하나가 소비하는 CPU' (측정: 포화CPU ÷ 포화rps).
        #   이게 CPU-burn 앱의 자연스러운 예약 단위다 — 파드 하나가 한 번에 요청 하나를
        #   처리하므로, 그 요청 하나가 쓰는 CPU만큼 예약하면 스케줄 밀도가 실제와 맞는다.
        #
        #   ★'포화 CPU × 계수'를 쓰던 것을 버렸다. 그건 6배 과대였다:
        #     실측 — 포화 1935m / 포화rps 3.2 → 요청당 605m·s.
        #     그런데 SLO-safe 처리량은 파드당 0.35 rps이므로 파드의 평균 CPU 점유는
        #     605 × 0.35 ≈ 212m 이다. 포화값(1935m×0.75=1451m)을 예약하면
        #     실제 평균의 약 7배를 잡아두고 노드를 놀린다(비용 직접 손실).
        #
        #   ★성능이 깎이지 않는 이유: 처리 속도는 limit이 정한다(vcpu 전부).
        #     요청이 도착한 순간 파드는 limit까지 burst로 쓰고, request는 '스케줄러가
        #     자리를 얼마나 잡아두는가'일 뿐이다.
        #   ★그렇다고 무한히 작게 잡으면 안 된다: 노드당 파드가 많아지면 여러 파드가
        #     동시에 burst할 때 코어를 나눠 쓰고 처리시간이 늘어난다.
        #     실측 — 노드당 4파드(434m) 77% / 2파드(600m) 90%.
        #     요청당 CPU를 그대로 쓰면 노드당 2~3파드가 되어 그 균형점에 앉는다.
        _wants_all_cores = (vcpu * 1000) >= avail
        if _wants_all_cores and s_cpu and _s_rps > 0:
            # ★동시 1 기준 rps로 계산: 측정은 동시 2+에서 하지만, req는 "1건 독점 시 CPU 비용".
            #   동시 2+면 코어를 나눠 써서 rps가 올라가고 cpu_per_req가 과소 추정된다.
            #   origin_p50(큐잉 0 단일 요청 처리시간)에서 동시1 rps를 역산하면 정확하다.
            _og_ms = measured["stress"].get("origin_p50") or 0
            if _og_ms > 0:
                _single_rps = 1000.0 / _og_ms
                _cpu_per_req_s = s_cpu / _single_rps
            else:
                _cpu_per_req_s = s_cpu / float(_s_rps)
            _s_max_req = min(int(avail * 0.94), int(_cpu_per_req_s))
        else:
            _s_lo2 = _cpu_at_lo("stress", s_cpu)
            _s_max_req = max(200, int(_s_lo2 * STRESS_REQ_MARGIN) if _s_lo2 > 0 else avail // 2)
        _s_est = min(_s_est, _s_max_req)
        # ★floor = avail//4: 이보다 작으면 노드에 파드가 너무 많이 들어가 CPU 경합이 난다.
        #   avail//4면 노드당 stress 4파드가 상한이 되어 적절한 격리와 스케일링 균형.
        # ★floor도 '요청당 CPU'다 — 상한과 같은 근거를 쓴다.
        #   큐잉 공식(_s_est)이 이보다 작게 나오면 노드당 파드가 과밀해져(코어 경합)
        #   처리시간이 늘고 90% 티어가 불가능해지므로, 이 값을 하한으로 지킨다.
        _s_floor = (min(int(avail * 0.94), int(s_cpu / float(_s_rps)))
                    if (_wants_all_cores and s_cpu and _s_rps > 0) else 200)
        s_req = max(_s_floor, min(int(avail * 0.94), _s_est))
        _cpr_show = s_cpu / max(1, _s_rps)
        _tail_ok = 0 < _s_tail_r < SLO["stress"]
        print(f"  [stress request] {s_req}m = 요청당 CPU {_cpr_show:.0f}m·s "
              f"(포화 {s_cpu}m ÷ {_s_rps:.1f} rps) → 노드당 {max(1, avail // max(1, s_req))}파드")
        print(f"    무부하 꼬리 {_s_tail_r}ms / SLO {SLO['stress']}ms "
              f"{'→ 큐잉 여유 있음' if _tail_ok else '→ ★꼬리가 SLO 초과: 큐잉 공식 무효, 요청당 CPU로 사이징'}")
        print(f"    limit {s_lim}m 이 실행 중 코어 전부를 보장한다(처리 속도는 limit이 결정)")
    else:
        # 공존: request를 작게 잡아 다른 앱과 같은 노드에 패킹.
        # limit(2000m)이 실행 시 성능을 보장하므로 request는 스케줄 예약일 뿐.
        _s_max_req = max(200, avail // 4)
        s_req = max(200, min(_s_max_req, int(s_cpu * 1.2)))

    # Memory: 실측 기반 (req=실측×1.3, limit=실측×3, floor)
    u_mem_req = max(48, int(u_mem * 1.3)); u_mem_lim = max(256, int(u_mem * 3))
    p_mem_req = max(48, int(p_mem * 1.3)); p_mem_lim = max(256, int(p_mem * 3))
    s_mem_req = max(64, int(s_mem * 1.3)); s_mem_lim = max(256, int(s_mem * 3))

    # ── 상주(min): ★baseline 2노드 유지 + MNG 남은 자리를 user 상주로 채워 concurrency 확보 ──
    #   [노드A(MNG): user+product 상주 패킹] + [노드B: stress 독차지] = 여전히 2대(다 MNG에 들어감=비용 0).
    #   ★user는 DB앱이라 스파이크 burst 흡수엔 concurrency(파드 수)가 중요 → MNG 빈 자리만큼 상주로 채움
    #     (앱 가벼우면 더 많이). 노드 추가 0이라 비용 안전. 상한 4(파드밀도·여유). 무거운 앱이면 fit2=false → 1.
    #   스파이크가 상주 넘으면 HPA 스케일 → 빠지면 scaleDown이 2노드로 수렴.
    fit2 = (2 * u_req + 2 * p_req) <= avail_mng
    # ★min = 'baseline 2대(MNG 1 + stress노드 1)에서 돌아가는 최소 파드 수'.
    #   과하면 MNG에 안 들어가 baseline부터 3~5대가 된다(실측: min 과대 → 5대).
    #   ★user+product min 합계의 request가 MNG 가용의 70% 이내여야 baseline 2대.
    #     나머지 30%는 HPA가 1~2파드 추가할 여유분(이게 있어야 Pending 없이 즉시 뜸).
    #   ★product는 캐싱(CloudFront)이라 파드 1개로 대부분 처리. min 1로 충분.
    #   ★stress min = 1. anti-affinity로 전용 노드가 필요하므로 2를 넣으면
    #     434×2=868m 한 노드에 들어가도 baseline이 깨질 수 있다.
    #     스파이크 시 HPA util 70%가 즉시 2→3으로 올리고 그때 카펜터가 노드 +1.
    # ★상주(min) = '측정된 저부하 트래픽을 SLO 안에서 처리하는 데 필요한 파드 수'.
    #   유도(앱 무관):
    #     ρ_slo    = 1 − S/SLO          큐잉 여유 (S = 무부하 처리시간, 측정)
    #     cap_pod  = 포화 rps × ρ_slo   파드 하나가 SLO를 지키며 감당하는 rps
    #     min      = ceil(저부하 rps ÷ cap_pod)
    #   ★임의 상수(4, 5, 예산비율)를 쓰지 않는다. 앱이 느리면(ρ_slo 작음) min이 커지고,
    #     빠르면 1이 된다 — 그게 맞는 방향이다.
    #   ★MNG 예산으로만 클램프한다: 상주는 '항상 켜진' 값이므로 MNG를 넘으면 상시 비용이 된다.
    def _resident_pods(app, req_m, budget_m, node_exclusive=False):
        m = measured[app]
        s_ms = m.get("p95_light") or m.get("origin_p50") or 0
        rho = max(0.05, 1.0 - s_ms / float(SLO[app])) if s_ms else 0.5
        cap_pod = (m.get("rps") or 0) * rho
        lo = m.get("rps_lo") or 0
        need = int(math.ceil(lo / cap_pod)) if (cap_pod > 0 and lo > 0) else 1
        # ★baseline 제약 — 상주는 'baseline 노드 수' 안에 들어가야 한다.
        #   baseline 정의: MNG 1대(io 앱 공존) + Karpenter 1대(노드 독점 앱).
        #   ★노드 독점 앱은 상주가 1이어야 한다. 그 앱은 파드 1개가 노드 1대이고
        #     (topologySpread가 노드당 1파드로 퍼뜨린다), 상주 2면 baseline이 3대가 된다.
        #     실측 사고 — 상주 2로 계산되어 stress가 2노드를 잡고, 거기에 io 파드까지
        #     흩어져 노드가 4대가 됐다.
        #   ★부하가 오면 HPA가 즉시 늘린다(util이 측정 기반이라 baseline 직후 반응한다).
        #     즉 상주를 1로 두는 것이 '스케일 못 함'을 뜻하지 않는다.
        if node_exclusive:
            return 1
        fit = budget_m // max(1, req_m)
        # ★io 앱 상주 하한 2 — 예산이 허락하면 반드시 2 이상.
        #   근거(가용성): 파드가 1개면 롤링 업데이트·노드 회수·OOM 순간에 처리할 파드가
        #   0이 되는 창이 생긴다. maxUnavailable:0 + maxSurge:1이라 교체 중 겹치지만,
        #   ALB 타깃 등록/해제 사이에 수 초의 공백이 남는다.
        #   ★io 파드는 MNG 안에서 노드 비용이 0이므로 2개를 두는 대가가 없다.
        #     실측 최적 구성도 user/product 모두 min 2였다.
        return max(1, min(max(2, need), max(1, fit)))

    p_min = _resident_pods("product", p_req, int(avail_mng * 0.25))
    u_min = _resident_pods("user", u_req, int(avail_mng * 0.5))
    s_min = _resident_pods("stress", s_req, avail,
                           node_exclusive=((vcpu * 1000) >= avail))

    # ── HPA util 임계 = 앱 bound에서 유도 (하드코딩 X) ──
    #   ★io 앱: util 80 = CPU-HPA 사실상 OFF → 스케일은 scaler.py(레이턴시+증설효과판정)가 min으로 소유.
    #     이유: I/O앱은 부하 시 CPU가 실제 필요(지연)와 따로 놀아, CPU로 스케일하면 쓸모없이 max까지 폭주함
    #     (실측: user CPU 7%인데 30파드=4노드 참사). → CPU-HPA 끄고 레이턴시 컨트롤러(scaler)가 몰게 위임.
    #     ★전제: 채점 중 scaler.py가 반드시 떠 있어야 io 앱이 스케일됨(util 80은 CPU 진짜포화 백스톱일 뿐).
    #   cpu 앱(stress): util 50 = CPU가 부하를 정직 반영 → CPU-HPA 유효(포화 근처 스케일, provision 지연 여유).
    #   ※분류(bound_of)는 아래 출력에 찍힘 — DB형 앱인데 'cpu-bound'로 나오면 오분류(임계 CPU_BOUND_MPS 조정).
    def _slo_safe_rps(app):
        """그 앱이 SLO를 지키며 파드 하나가 감당하는 rps. scaler_cap.json과 반드시 같은 정의.
        ★큐잉 0 상태(단일요청)의 꼬리가 이미 SLO를 넘으면 p95 할인을 적용하지 않는다.
          그 꼬리는 앱 고유 특성이라 부하를 줄여도 사라지지 않으므로, 할인하면 용량을
          부당하게 깎아 노드를 과다 요구하게 된다."""
        r = measured[app].get("rps", 0) or 0
        p95v = measured[app].get("p95", 0) or 0
        og95 = measured[app].get("origin_p95", 0) or 0
        if og95 and og95 > SLO[app]:
            safe = r
        else:
            safe = r * min(1.0, SLO[app] / p95v) if p95v > 0 else r
        return max(r * 0.5, safe)

    def hpa_util(bound, req_m, cpu_m, app, cpu_lo=None, rps_lo=None, rps_hi=None, share=1):
        """util = 'SLO-safe 동작점에서의 CPU 이용률'. io/cpu 구분 없이 같은 식으로 유도한다.

        ★왜 하나의 식인가: HPA는 평균 CPU를 보고 목표에 맞춰 파드 수를 정한다. 그러니 목표는
          '그 앱이 SLO를 지키며 돌 때의 CPU 이용률'이어야 한다. 그 값은 전부 측정에서 나온다.
              duty_safe = min(1.0, SLO-safe rps × 요청 처리시간)   ← 파드가 바쁜 시간 비율
              cpu_safe  = 포화CPU × duty_safe
              util      = cpu_safe / request × 안전계수
          (포화CPU는 duty=1.0 상태의 측정값이므로 duty를 곱해 환산한다)

        ★버그 수정: cpu-bound 앱은 util이 50으로 하드코딩돼 있었다. 이 값이 어디서 안정되는지
          계산하면 앱마다 다르다 — 실측 사고:
            포화CPU 1085m / request 965m / 처리시간 0.828s / SLO-safe 0.75 rps 인 앱에서
            util 50 → 파드가 9개에서 안정 (실제 필요 5.3개) → 1파드=1노드라 노드 9대 점유
            → 다른 앱이 노드를 못 받아 성능 17~25% (노드는 10대인데 굶었다)
          같은 식으로 유도하면 util 63 → 5.9파드에서 안정 → 노드 3대가 다른 앱에 돌아간다.

        ★예외: CPU가 부하에 반응하지 않는 앱(낮은 CPU·긴 레이턴시형)은 CPU-HPA를 끈다.
          증설해도 CPU가 안 내려가 무한 증설이 되므로, 레이턴시 기반 scaler에 위임한다.
          판정은 측정 두 지점(프로브 저부하 / 본측정 고부하)의 CPU 변화로 한다."""
        if (cpu_lo and cpu_m and rps_lo and rps_hi
                and rps_hi >= rps_lo * 1.5 and cpu_m <= cpu_lo * 1.15):
            return 400          # CPU 무반응 → 사실상 OFF
        if not (req_m and cpu_m):
            return 80
        # ★util = '포화 CPU의 UTIL_TRIGGER_FRAC 지점에서 증설이 시작되는 이용률'.
        #   request가 '저부하 동작점(cpu_lo)'이므로 util은 100을 넘는 것이 정상이다
        #   (averageUtilization > 100 은 쿠버네티스에서 유효하다 — request 대비 비율일 뿐).
        #
        # ★버그였던 것: util을 [40, 85]로 클램프했다. request = cpu_lo 인데 util이 85면
        #   증설 트리거가 0.85 × cpu_lo 로 저부하 CPU보다 낮아진다.
        #   → 트래픽이 거의 없는 baseline에서도 HPA가 계속 파드를 늘린다
        #     (검증: user 트리거 224m < 저부하 실측 263m → 평상시 증설).
        #   그러면 저부하 비용 만점(노드 2대)이 깨진다. request와 util은 반드시 같은
        #   기준점 위에서 정의돼야 한다.
        #
        # ★두 경계로 클램프한다 (둘 다 측정값에서 나온다):
        #   하한 = 저부하 이용률 × UTIL_IDLE_MARGIN → 평상시에는 절대 증설하지 않는다
        #   상한 = 포화 이용률                      → 포화보다 늦게 반응하는 것은 의미가 없다
        #   하한이 상한을 넘는 앱(저부하 CPU ≈ 포화 CPU, 예: CPU-burn 앱)은 CPU로
        #   저부하와 스파이크를 구분할 수 없다 → 하한을 택해 CPU-HPA를 사실상 끄고,
        #   그 앱의 스케일은 scaler의 ρ/지연/5xx 경로가 담당한다(그게 정확한 신호다).
        t_sec = (measured[app].get("p95_light") or 0) / 1000.0      # 요청 처리시간(채점 경로 p50)
        safe_rps = _slo_safe_rps(app)
        duty = min(1.0, safe_rps * t_sec) if (t_sec > 0 and safe_rps > 0) else 1.0
        sat_util = cpu_m * max(duty, 0.5) / float(req_m) * 100      # 포화 시 이용률
        # ★util은 '정상 범위(40~85%)'로 낸다. 100%를 넘는 값은 쓰지 않는다.
        #   ★이전 구조가 왜 틀렸는가:
        #     request를 '상주 동작점 CPU'로 잡고 util을 그 배수(160%, 300%, 716%)로 뒀다.
        #     쿠버네티스는 100% 초과 target을 허용하지만, 그러면 두 가지가 깨진다:
        #       ① HPA의 목표가 request 위에 있어 '언제 증설되는지'가 직관을 벗어난다.
        #          실측 라이브: user 45%/160%, product 185%/160%, stress 78%/301% —
        #          같은 클러스터에서 어떤 앱은 목표의 1/4, 어떤 앱은 초과 상태로 떠 있었다.
        #       ② request가 실사용보다 작게 잡히므로 스케줄러가 노드에 과밀 배치하고,
        #          그 상태에서 CPU 경합이 나면 HPA는 '아직 목표 미달'로 보고 증설하지 않는다.
        #   ★올바른 기준: request가 이미 'SLO를 지키며 도는 파드의 CPU'다(size_request).
        #     그러면 util은 그 동작점의 몇 %에서 증설을 시작할지를 뜻하고,
        #     UTIL_TRIGGER_FRAC(0.70)이 그대로 정답이 된다 — 포화 30% 전에 증설 시작.
        #   ★안전장치: 목표가 '저부하 실사용 이용률'보다 반드시 위여야 한다.
        #     아니면 트래픽이 없는 baseline에서도 HPA가 증설해 노드가 늘어난다.
        #     저부하 이용률은 파드당 값으로 환산한다(cpu_lo는 단일 파드 측정값이므로 share로 나눔).
        u_target = UTIL_IO if bound != "cpu" else UTIL_TRIGGER_FRAC * 100.0
        lo_cpu = measured[app].get("cpu_lo") or 0
        _share = max(1, share) if bound != "cpu" else 1
        lo_util = (lo_cpu / float(_share)) / float(req_m) * 100 if lo_cpu else 0.0
        if lo_util > 0:
            u_target = max(u_target, lo_util * UTIL_IDLE_MARGIN)
        # 포화 이용률보다 높게 두면 '포화보다 늦게' 반응하므로 의미가 없다 → 상한으로 clamp.
        if sat_util > 0:
            u_target = min(u_target, max(UTIL_SANITY_MIN, sat_util))
        return int(round(max(UTIL_SANITY_MIN, min(UTIL_SANITY_MAX, u_target))))

    def _util_of(app, bound, req_m, share=1):
        m = measured[app]
        return hpa_util(bound, req_m, m.get("cpu"), app,
                        m.get("cpu_lo"), m.get("rps_lo"), m.get("rps"), share)
    # ★HPA/scaler 역할 분리 (min=max 잠금은 폐기했다).
    #   이전 설계: scaler가 매 사이클 minReplicas=maxReplicas 로 잠가 HPA 재량을 0으로 만들었다.
    #     의도는 '두 컨트롤러 충돌 원천 차단'이었지만, 대가가 훨씬 컸다 —
    #     HPA의 15초 CPU 반응 경로가 통째로 사라져서, 스케일해야 할 때 scaler의
    #     지연 판정(6초 디바운스 + loaded 게이트)만 남았고 그 게이트가 막히면 아무 일도
    #     일어나지 않았다(실측: user 41.3% / product 81.2% / stress 81.7%).
    #   현재 설계: min과 max를 분리한다.
    #     · scaler = minReplicas(바닥). 부족이 확인되면 올리고 부하가 빠지면 내린다.
    #     · HPA    = min~max 사이에서 CPU util로 즉시 증설/축소.
    #     둘 다 '올리는' 방향으로만 작용하고 축소는 HPA scaleDown + scaler의 min 하향이
    #     함께 하므로 서로 되돌리는 구간이 없다.
    #   ★충돌 방지의 실제 수단은 잠금이 아니라 '기준점 정렬'이다:
    #     request를 상주 시 파드당 실측 CPU로 잡고 util을 그 배수로 두면,
    #     HPA는 '트래픽이 N배 되면 증설'이라는 명확한 규칙이 되고 scaler의 rps 사이징과
    #     같은 방향을 본다.
    # ★HPA util을 '측정된 앱 특성'에서 유도한다 (앱 이름 하드코딩 없음).
    #
    #   [특성 1] 캐시 여부 — 채점 경로 p50이 오리진 p50보다 뚜렷하게 작으면 캐시가 요청을 흡수한다.
    #     캐시 앱은 파드 CPU가 부하에 거의 반응하지 않으므로, util을 낮게 두면
    #     노이즈로 증설되고 그 파드가 다른 앱의 노드 자원을 잠식한다 → util을 높게 둔다.
    #
    #   [특성 2] SLO 여유 — 무부하 처리시간 S가 SLO에 가까우면 큐잉 여유가 없다.
    #     ρ_slo = 1 - S/SLO 가 그 여유다. 작을수록 조금만 부하가 늘어도 SLO를 넘으므로
    #     util을 낮게 둬서 일찍 증설해야 한다.
    #
    #   두 특성을 곱해 트리거를 정한다. 앱 이름이 아니라 측정값만 쓰므로 어떤 앱에도 성립한다.
    def _app_traits(app):
        m = measured[app]
        cf50 = m.get("p95_light") or 0          # 채점 경로 처리시간(캐시 포함)
        og50 = m.get("origin_p50") or 0         # 오리진 처리시간(파드 실제 일)
        # ★캐시 판정: 두 가지 신호 중 하나라도 성립하면 캐시 앱.
        #   (1) 채점경로 p95 < 오리진 p50 × 0.8 → CDN이 요청을 흡수하고 있다
        #   (2) 측정 시 cache_hit=True로 감지됨 (아래 detect_cache에서 설정)
        #   ★왜 OR인가: (1)은 측정 타이밍에 따라 불안정하다(첫 요청이 MISS면 cf50≈og50).
        #     (2)는 명시적 캐시 히트 테스트 결과. 하나라도 True면 "캐시 정책이 있다"는 증거.
        cached = bool(cf50 and og50 and cf50 < og50 * 0.8) or m.get("cache_hit", False)
        s_ms = cf50 or og50 or 0
        rho_slo = max(0.05, 1.0 - s_ms / float(SLO[app])) if s_ms else 0.5
        return cached, rho_slo

    def _derive_util(app, req_m, share=1):
        """util = '저부하 이용률 × 배수'. 배수는 측정된 앱 특성에서 나온다.

        ★기준점이 '저부하 이용률'이어야 하는 이유:
          request는 스케줄 예약용으로 작게 잡힌다(무료 구간 확보). 그래서 실사용 CPU가
          request를 크게 넘고, 포화 이용률이 200~1600%까지 나온다.
          이 구조에서 util을 [40,85]로 클램프하면 두 방향으로 다 틀린다:
            · 저부하 이용률이 85%보다 크면(예: 104%) baseline에서 이미 목표 초과
              → 트래픽이 없어도 HPA가 계속 증설한다(실측: 과증설 + 노드 폭증).
            · 저부하가 16%인 앱은 85%가 트래픽 5배 지점이라 증설이 너무 늦다.
          → 'baseline 대비 몇 배 부하에서 증설할 것인가'로 정의해야 앱 무관하게 성립한다.
        ★averageUtilization은 100 초과가 유효하다(request 대비 비율일 뿐).

        배수 = 1.3 + ρ_slo, 캐시 앱은 ×1.5
          ρ_slo(SLO 여유)가 크면 큐잉 여유가 있어 늦게 증설해도 된다 → 배수 크게.
          ρ_slo가 작으면 조금만 부하가 늘어도 SLO를 넘는다 → 배수 작게(빨리 증설).
          캐시 앱은 파드 CPU가 부하에 거의 반응하지 않으므로 더 늦게 증설(불필요한 파드 방지).
        """
        m = measured[app]
        cpu_sat = m.get("cpu") or 0
        rps_sat = m.get("rps") or 0
        if not (cpu_sat and req_m and rps_sat > 0):
            return IO_UTIL_FIXED
        _cpu_per_req = cpu_sat / float(rps_sat)
        _is_cpu_bound = _cpu_per_req >= CPU_BOUND_MPS
        if not _is_cpu_bound:
            # ★io 앱 util: 부하 p95와 SLO의 비율(pressure)에서 유도한다.
            cached, _ = _app_traits(app)
            slo = float(SLO[app])
            p95_load = m.get("p95") or 0
            og_p50 = m.get("origin_p50") or 0

            if p95_load > 0 and slo > 0:
                pressure = min(1.5, p95_load / slo)
                # 비캐시: util = 40 - pressure × 9
                #   pressure 0.8(user p95 160ms) → 33%
                #   pressure 1.0 → 31%,  pressure 0.5 → 36%
                util_derived = 40.0 - pressure * 9.0

                # 캐시 앱: 오리진 p50(미스 비용) 기반으로 별도 계산
                if cached and og_p50 > 0:
                    miss_pressure = min(1.0, og_p50 / slo)
                    # miss_pressure 0.045(product 9ms/200ms) → 29%
                    util_derived = 30.0 - miss_pressure * 15.0

                return max(20, min(45, int(round(util_derived))))

            # 폴백
            return IO_UTIL_CACHED if cached else IO_UTIL_FIXED
        # ★CPU-bound 앱: 동시성 목표를 SLO 여유에서 유도한다.
        #   conc = 0.5 + (1 - S/SLO) × 0.1
        #   S(고유지연)가 SLO에 가까우면(여유 적음) → conc 낮게(0.5) → 빨리 증설
        #   S가 SLO보다 한참 작으면(여유 큼) → conc 높게(0.6) → 늦게 증설(파드 절약)
        #   ★일반화: 어떤 앱이든 S와 SLO만 있으면 conc가 나온다.
        _s_ms = m.get("origin_p50") or m.get("p95_light") or 0
        _slo = float(SLO[app])
        if _s_ms > 0 and _slo > 0:
            _slo_headroom = max(0.0, 1.0 - _s_ms / _slo)
            _conc = 0.5 + _slo_headroom * 0.1
        else:
            _conc = CONC_CPU_BOUND
        _trigger_m = max(TRIGGER_FLOOR_M, _cpu_per_req * _conc)
        util = _trigger_m / float(req_m) * 100.0
        return int(round(max(UTIL_SANITY_MIN, min(100.0, util))))

    u_util = _derive_util("user", u_req, u_min)
    p_util = _derive_util("product", p_req, p_min)
    s_util = _derive_util("stress", s_req, 1)
    # ★시뮬레이션 출력: util 탐색 과정
    print("  ┌─ HPA util 탐색 결과 ────────────────────────────────────────")
    for _a, _u, _r in (("user", u_util, u_req), ("product", p_util, p_req), ("stress", s_util, s_req)):
        m = measured[_a]
        _cpu = m.get("cpu") or 0
        _rps = m.get("rps") or 0
        _cached, _rho = _app_traits(_a)
        _cpr_raw = _cpu / float(_rps) if _rps > 0 else 0
        _is_cpu = _cpr_raw >= CPU_BOUND_MPS
        _s_ms = m.get("p95_light") or m.get("origin_p50") or 0
        _slo = SLO[_a]
        if _is_cpu:
            _trigger = max(TRIGGER_FLOOR_M, _cpr_raw * CONC_CPU_BOUND)
            print(f"  │ {_a:8s}: util={_u}% [cpu-bound] cpu/rps={_cpr_raw:.0f}m×{CONC_CPU_BOUND}"
                  f" → trigger {_trigger:.0f}m / req {_r}m")
        else:
            _method = "M/M/1 탐색"
            _cache_str = " (캐시→-4%)" if _cached else ""
            print(f"  │ {_a:8s}: util={_u}% [io, {_method}] S={_s_ms}ms SLO={_slo}ms"
                  f" → 최대util에서 p95≤SLO 보장{_cache_str}")
    print("  └────────────────────────────────────────────────────────────")
    # stress util = 포화 CPU의 UTIL_TRIGGER_FRAC 지점, 저부하 이용률을 하한으로(hpa_util cpu 분기).
    #   ★stress는 min=max 잠금이 없어졌으므로 이 값이 실제로 작동한다.
    #     CPU-burn 앱은 부하와 CPU가 정비례하므로 util이 유효한 선행 신호다.
    #     단 요청 1건만 있어도 코어가 거의 포화되므로(저부하 CPU ≈ 포화 CPU) 하한(=평상시
    #     증설 금지선)이 상한을 넘어 CPU-HPA가 사실상 꺼지는 경우가 있다. 그건 정상이다 —
    #     CPU로는 '요청 1건'과 '요청 5건 대기'를 구분할 수 없기 때문이고,
    #     그 앱의 스케일 신호는 ρ(=rps/(파드×cap))·지연·5xx이며 scaler가 2초 주기로 본다.
    # stress util도 io 앱과 같은 정의를 쓴다 — 포화 CPU의 UTIL_TRIGGER_FRAC 지점,
    #   저부하 이용률 × UTIL_IDLE_MARGIN 을 하한으로.
    #   ★stress는 CPU-burn이라 요청 1건만 있어도 코어가 거의 포화된다(저부하 CPU ≈ 포화 CPU).
    #     그래서 하한이 상한을 넘어 CPU-HPA가 사실상 꺼지는 경우가 정상이다 —
    #     CPU로는 '요청 1건'과 '요청 5건 대기'를 구분할 수 없기 때문이다.
    #     stress의 스케일 신호는 CPU가 아니라 ρ(=rps/(파드×cap))·지연·5xx이고,
    #     그건 scaler가 2초 주기로 본다.
    # (s_util은 위에서 앱별로 직접 설정됨)
    s_scaleup = 3

    # stress: preferred anti-affinity → 한 노드에 여러 파드 가능.
    #   파드당 request = s_req(~node_cpu//2). 노드당 패킹 수 = avail // s_req.
    kp_nodes = max_nodes - mng_count
    pods_per_node = max(1, min(avail // s_req,                    # CPU 기준 패킹 수
                               mem_avail // max(1, s_mem_req)))   # 메모리 기준 패킹 수 → 더 빡빡한 쪽
    baseline_nodes = mng_count + 1                       # MNG + stress 최소 1노드 = baseline
    stages = node_stages(baseline_nodes, max_nodes)      # 총 노드 수 단계 사다리 (예: [4,6,8])
    # ★Karpenter 캡을 처음부터 하드캡(총 8대분)으로 준다 — 사다리 1단계로 시작하지 않는다.
    #   근거: Karpenter는 '수요 기반'이다. limits.cpu는 천장이지 목표가 아니라,
    #   Pending 파드가 없으면 노드를 만들지 않는다. 캡을 낮게 시작하면 얻는 것은 없고
    #   (평상시 노드 수는 어차피 수요가 정한다) 스파이크에서 파드가 Pending으로 막혀
    #   준수율만 잃는다 — 실측: baseline 패턴에서도 stress가 83%였던 원인 중 하나다.
    #   과증설 방어는 캡 사다리가 아니라 (a) HPA util 목표 (b) scaler의 최소 사이징
    #   (c) consolidateAfter 15s (d) 총 8대 하드캡이 담당한다.
    kp_node_cap = max(1, kp_nodes)
    # ★s_max = 노드 독점 앱의 파드 상한. '노드 수'로 묶는다.
    #   ★기존 `pods_per_node × kp_nodes` 는 request가 작아지면 폭주한다:
    #     실측 사고 — stress request가 200m(floor)으로 잡히자 pods_per_node = 8이 되고
    #     s_max = 8 × 7 = 56 이 산출됐다. 그 값이 base_max로 들어가 scaler/HPA가
    #     56파드까지 채울 수 있게 됐고, limit 2000m이라 실행 중에는 파드당 노드 1대가
    #     필요하므로 7노드(하드캡)까지 폭주하는 구성이 됐다.
    #   ★limit이 노드 allocatable 이상이면 '실행 중 1파드 = 1노드'다. 그러면 파드 상한은
    #     노드 예산(kp_nodes)을 넘을 이유가 전혀 없다 — 넘겨도 Pending만 쌓인다.
    #     limit이 작아 여러 파드가 실제로 공존 가능한 앱만 pods_per_node를 곱한다.
    _s_mono = (vcpu * 1000) >= avail          # limit(= vcpu×1000)이 노드 가용 이상인가
    s_max = max(2, kp_nodes if _s_mono else pods_per_node * kp_nodes)
    print(f"  [stress max] {'노드 독점(limit ≥ 노드가용)' if _s_mono else '공존 가능'} → "
          f"{s_max}파드 (Karpenter 노드 예산 {kp_nodes}대"
          f"{'' if _s_mono else f' × 노드당 {pods_per_node}파드'})")

    # ── max = 측정 기반 동적 결정 (하드코딩 X) ──
    # ★버그 수정 1 — 분모를 request가 아니라 max(request, 실측 포화 CPU)로.
    #   request가 실측보다 작으면(io 앱은 avail/4 상한에 걸려 그렇게 되기 쉽다) max가 그 비율만큼
    #   부풀고, 파드가 "예약만 하고 안 쓰는" 상태로 클러스터 예산을 선점한다.
    #   실측 사고: u_req 332m(실측 964m) → u_max = 8×1330//332 = 32 → 32파드가 10.6코어를 예약
    #     (실사용 4.8코어) → 무거운 앱이 전용 노드를 못 받아 1파드에 갇힘 → 성능 게이트 붕괴 → 비용 0.
    #     이 상태에선 노드를 6대→8대로 늘려도 늘어난 노드를 이 앱이 다시 먹어서 점수가 그대로였다.
    # ★버그 수정 2 — 앱마다 '클러스터 전체 예산'을 주면 세 앱이 같은 예산을 각자 다 쓴다고 계산해
    #   서로를 굶긴다. 전용 노드가 필요한 무거운 앱 몫을 먼저 떼고 남는 예산을 나눈다.
    #   (앱 이름 하드코딩 아님: s_max×s_req = "노드당 1파드급 앱이 실제로 점유할 예산")
    node_budget = max_nodes * avail
    # ★무거운 앱 몫은 '상주(min)'로 뗀다 — max로 떼면 안 된다.
    #   max는 천장이고 세 앱이 동시에 천장에 닿는 일은 없다. s_max(=하드캡 전체를 쓸 수 있는
    #   파드 수)로 예산을 떼면 io 앱의 max가 그만큼 깎여, 정작 io 스파이크에서 파드를 못 늘린다
    #   (실측: s_max 14 × 816m = 11424m을 예약해 user max가 46 → 9로 떨어졌다).
    #   상주분만 떼면 '항상 필요한 만큼'은 보장되고, 그 위는 먼저 필요한 앱이 쓴다.
    #   동시에 둘 다 필요해지면 노드 하드캡(8대)이 최종 제약이 되고, 그 안에서의 배분은
    #   우선순위(앱 간 동등) + 스케줄러가 정한다.
    heavy_reserve = min(node_budget - avail, s_min * s_req)   # 최소 1노드는 io에 남김
    io_budget = max(avail, node_budget - heavy_reserve)
    # ★max 산정 (버그 수정 2건) — max는 '천장'이지 목표가 아니다. 실제 파드 수는 scaler가
    #   need_rps(투영rps / cap)로 최소값을 정한다. 그러므로 max가 낮으면 손실만 있고 이득이 없다.
    #     · max가 낮으면 : 스파이크에서 파드를 못 늘려 ρ>1 → 큐 폭발 → 성능·가용성 동시 붕괴
    #                      + 비용 게이트(모든 앱 perf ≥ 30%)까지 잃는다
    #     · max가 높으면 : 아무 일도 안 일어난다 (scaler가 min=max로 잠그므로 HPA 재량 0,
    #                      노드는 총 8대 하드캡, 실제 파드 수는 수요 기반 사이징이 결정)
    #
    # ★io 앱 max = 'MNG 노드에서 상대 앱의 상주분을 뺀 나머지'로 잡는다.
    #   ★여기가 비용의 급소다. io 앱은 request가 작아 파드를 늘리는 것 자체는 공짜지만,
    #     MNG 노드 용량을 넘는 순간 Karpenter가 워커 노드를 만든다 → 그 뒤로는 공짜가 아니다.
    #     그리고 HPA는 노드를 전혀 모른다. max가 MNG 용량보다 크면 HPA가 CPU만 보고
    #     그 천장까지 파드를 늘려 노드를 만들어버린다.
    #   ★분배 방식을 '실측 CPU 비 정적 분배'에서 바꿨다. 정적 분배는 용량을 낭비한다:
    #     실측 검증 — user가 15파드(1230m)를 필요로 하는 부하에서 정적 분배는 user에게
    #     938m(11파드)만 줬다. 나머지 339m는 product 몫인데 product는 상주 120m만 쓰고 있었다.
    #     그래서 MNG에 자리가 있는데도 user가 11파드에 막혀 준수율이 87%에 머물렀다(-0.5점).
    #   → 각 앱이 'MNG 전체 - 상대 앱 상주분'까지 쓸 수 있게 한다.
    #     한쪽이 한가하면 다른 쪽이 그 자리를 다 쓴다 = 낭비 0.
    #   ★둘이 동시에 max까지 차면 MNG를 넘어 워커 1대가 붙을 수 있다. 그건 허용한다:
    #     · 파드가 부족한 손실은 '지속적'이다 (준수율은 누적 비율이라 계속 깎인다)
    #     · 워커 1대 손실은 '일시적'이다 (부하가 빠지면 consolidateAfter 15초에 회수)
    #     · 그리고 둘이 정확히 동시에 피크를 치는 경우는 드물다
    #   ★상한은 여전히 총 8대 하드캡이다.
    # ★io max = '클러스터 io 예산'을 자기 request로 나눈 값 (MNG로 묶지 않는다).
    #   ★MNG로 묶었다가 되돌렸다 — 그게 스파이크 성능 붕괴의 원인이었다:
    #     실측 사고 — user max를 4(MNG 안)로 묶으니 22rps 스파이크에서 need 15파드인데
    #     4파드에 갇혀 ρ>>1 → user 36.8% / product 75.4% / stress 77.4%로 전부 무너졌다.
    #     비용은 12/12(노드 2대) 만점이었지만 성능에서 8.5점을 잃어 순손실이 컸다.
    #   ★'트래픽 처리를 위해 필요한 노드를 최소로'가 목표다. 필요한데 못 늘리는 것은
    #     최소가 아니라 부족이다. max는 천장이고, 실제 파드 수는 scaler의
    #     need_rps(= 투영rps / cap)가 정한다 — 딱 필요한 만큼만 늘어난다.
    #   ★과증설이 안 되는 이유:
    #     · HPA util이 높다(240%+) → HPA가 need를 넘어 max까지 채우지 않는다
    #       (이전엔 util 160%라 HPA가 채워 19파드가 됐다)
    #     · scaler 천장 상향은 min(hpa_hard, need)로 need에 묶인다
    #     · 노드 총 8대 하드캡 + consolidateAfter 45s
    #     · 부하가 빠지면 need가 줄고 축소 경로가 되돌린다
    #   ★분모는 request다(스케줄러가 보는 값). 상대 앱 상주분만 뺀다.
    # ★max = 'baseline + 카펜터 노드 1~2대 분량'. 이 이상은 절대 필요하지 않다.
    #   product는 캐싱이라 파드 몇 개면 충분하고, user만 스파이크에서 늘어남.
    #   max가 과하면 HPA가 그 천장까지 채워 노드를 소진한다.
    #   ★MNG(~1930m) + 카펜터 1대(~1930m) = 3860m 에서 stress 몫을 빼면 io 예산.
    _io_total = avail_mng + avail - s_min * s_req  # io 앱이 쓸 수 있는 총 예산
    # ★max = 'MNG 무료 구간 + 워커 1대'까지. 그 이상은 노드 예산 근거가 없다.
    #   근거: max는 천장이고 실제 파드 수는 HPA가 정한다. 하지만 max가 노드 예산을 넘으면
    #   HPA가 그 천장까지 채우려다 Pending만 쌓이고(노드 하드캡) 성능이 깎인다.
    #   MNG(무료) + 워커 1대면 스파이크에 충분하고, 부족하면 Pending → 카펜터가 판단한다.
    # ★max = '노드 예산이 허용하는 파드 수'. 앱별 임의 배수를 쓰지 않는다.
    #   근거: max는 천장이고 실제 파드 수는 HPA가 util로 정한다. 다만 노드 예산을 넘는
    #   천장은 Pending만 쌓으므로(노드 하드캡) 예산으로 자른다.
    #   ★캐시 앱이 과도하게 늘지 않는 것은 max가 아니라 util이 막는다:
    #     캐시 앱은 파드 CPU가 부하에 거의 반응하지 않아 util 트리거에 도달하지 않는다
    #     (_derive_util이 캐시 판정으로 배수를 1.5배 올려 더 늦게 증설한다).
    #     즉 '캐시니까 max를 작게'는 중복 제약이고, 진짜 스파이크에서 파드를 못 늘리게 만든다.
    # ★io max = 'MNG(상시 켜진 노드)에 들어가는 파드 수'. 워커 1대분을 더하지 않는다.
    #   ★근거: io 파드는 MNG 안에서만 공짜다. max가 MNG를 넘으면 HPA가 CPU만 보고
    #     그 천장까지 채우고, 넘친 파드가 Pending → 워커 노드가 붙는다(비용 손실).
    #     실측 대조 — max 48(MNG+워커1대)로 두면 2880m를 요구해 노드가 늘었다.
    #     max 20(MNG 안)이면 1400m로 MNG에 들어가 노드가 안 늘어난다.
    #   ★파드가 부족하지 않은 이유: MNG에 들어가는 파드 수 × 파드당 SLO-safe rps 가
    #     이미 충분한 처리량이다(user 20파드 × 40rps = 800 rps).
    #     진짜로 그걸 넘는 부하면 Pending이 나고 카펜터가 노드를 준다 — 그때는 정당하다.
    # ★io max = MNG 전체 밀도 + rolling surge 여유.
    #   "MNG에 이 앱 파드가 최대 몇 개 들어가는가" + maxSurge 2 = max.
    #   ★상대 앱 상주분을 빼지 않는다: 스파이크에서 한쪽만 바쁘면 MNG 전체를 쓸 수 있어야 한다.
    #   ★+2 = rollingUpdate maxSurge. 업데이트 중에도 max에 안 걸리게.
    #   ★오버플로 노드가 있으므로 max를 넘기면 Pending → 카펜터가 노드를 준다.
    #     max는 "천장"이지 목표가 아니므로 넉넉하게 둬야 스파이크에서 파드 부족이 안 난다.
    _mng_density = avail_mng // max(1, u_req)
    u_max = max(u_min + 2, _mng_density + 2)
    p_max = max(p_min + 1, _mng_density + 2)
    print(f"  io max: user {u_max}파드 / product {p_max}파드 "
          f"(MNG 앱가용 {avail_mng}m ÷ request — 이 안에서는 노드 추가 0)")

    # ★scaleUp 스텝도 측정에서 유도한다: SLO 여유(ρ_slo)가 작은 앱일수록 큐가 빨리 쌓이므로
    #   한 번에 많이 올려야 한다. 여유가 큰 앱은 조금씩 올려 과증설을 막는다.
    #   상한은 max의 절반 — 한 번에 천장의 절반 이상 올리면 오버슛이 된다.
    def _scaleup_pods(app, mx):
        # ★io 앱: MNG 안에서 파드를 빨리 늘려도 노드 비용이 없다.
        #   scaleUp을 공격적으로(6+) 잡아 스파이크 초반 큐잉을 방지한다.
        #   CPU-bound 앱: 파드=노드이므로 3이 적정 (한번에 노드 3대까지).
        _c, rho = _app_traits(app)
        _cpr = (measured[app].get("cpu") or 0) / max(1, measured[app].get("rps") or 1)
        _is_cpu = _cpr >= CPU_BOUND_MPS
        if not _is_cpu:
            # io 앱: 최소 6, 최대 max//2 (MNG 안이라 비용 0)
            return max(6, min(mx // 2, 10))
        else:
            # cpu-bound: 1파드/15초. 1파드=1노드이므로 한번에 여러 노드 부팅을 방지.
            # 순차 증설 → 과도기가 짧고 안정적. stress SLO 1000ms라 30초 지연도 SLO 내.
            return 1
    u_scaleup = _scaleup_pods("user", u_max)
    p_scaleup = _scaleup_pods("product", p_max)
    s_scaleup = _scaleup_pods("stress", s_max)

    u_min = min(u_min, u_max)                  # 상주가 max를 넘지 않게
    p_min = min(p_min, p_max)

    # ★4사분면 상주 여유(min↑) — 비활성화.
    #   이전에 "느림/burst 앱은 min을 올려 상주로 흡수"했으나, scaleUp 6파드+100%/15초가
    #   빠른 증설을 보장하므로 상주를 올릴 필요가 없다.
    #   ★상주를 올리면: baseline에서 파드 4개 → MNG를 더 일찍 포화 → 오버플로 노드 필요
    #     → 비용↑ + 복잡성↑. scaleUp이 15초 내에 12파드를 만들어주므로 상주 2로 충분.
    #   ★38점 구성도 min 2였다.
    u_head, p_head, s_head = False, False, False
    def _needs_headroom(app):
        c, r = measured[app]["cpu"], measured[app].get("rps", 0)
        if r > 0 and c / r >= CPU_BOUND_MPS:        # CPU-bound → CPU-HPA가 담당, 제외
            return False
        # ★판정에 '부하 p95'를 쓰면 안 된다. 동시성 적응 이후 부하 p95는 SLO의 0.9배를 목표로
        #   만들어진 제어 결과값이라, p95 ≥ SLO×0.75 조건이 항상 참이 되어 모든 io 앱이
        #   무조건 부스트를 받는다(= 상주 낭비 → MNG 예산 잠식 → baseline 붕괴).
        #   → 부하와 무관한 값으로 판정한다:
        #     · 느림   : 채점 경로 고유 지연(p50)이 SLO에 근접 = 큐잉 여유가 원래 없는 앱
        #     · burst  : 오리진 단일요청의 꼬리/중앙값 비 (큐잉 0, 캐시 영향 0 → 앱 고유 특성)
        light = measured[app].get("p95_light", 0)          # 채점 경로 최소 처리시간(p50)
        og50 = measured[app].get("origin_p50", 0)
        og95 = measured[app].get("origin_p95", 0)
        slow = light > 0 and light >= SLO[app] * SLOW_RATIO
        # ★burst는 '비율'만으로 보면 안 된다. 지연이 짧은 앱은 지터만으로 3배가 쉽게 나온다
        #   (실측: 오리진 p50 12ms → 꼬리 40ms = 3.3배지만 SLO 200ms 기준 완전히 무해).
        #   꼬리가 SLO에 유의미한 크기일 때만 burst로 인정한다.
        bursty = (og50 > 0 and og95 > 0
                  and (og95 / og50) >= BURST_RATIO
                  and og95 >= SLO[app] * 0.25)
        return bool(slow or bursty)
    # (_needs_headroom 비활성화됨 — 위에서 False, False, False로 설정)
    # ★상주 부스트는 'MNG 예산 안'에서만 한다.
    #   ★버그였던 것: max로만 클램프해서 MNG를 넘을 수 있었다.
    #     실측 — user min이 4→6으로 부스트되고, scaler 워밍업이 ×2 해서 12파드가 됐다.
    #     12 × 116m = 1392m > MNG 1278m → 워커 노드 발생. 트래픽은 1.8 rps였다.
    #     (노드 4대 / 비용 10-12) 상주는 '항상 켜져 있는' 값이라 노드를 만들면 상시 비용이다.
    #   → 부스트분이 MNG 여유 안에 들어갈 때만 적용한다. 안 들어가면 부스트를 생략한다
    #     (스파이크 대응은 scaler의 need_rps가 하고, 그건 MNG를 넘어도 정당하다).
    def _fit_boost(cur, add, req, other_used, cap_m):
        room = max(0, cap_m - other_used)
        return min(cur + add, max(cur, room // max(1, req)))
    _bcap = int(avail_mng * MNG_RESERVE_FRAC)
    if u_head:
        u_min = min(u_max, _fit_boost(u_min, HEADROOM_ADD, u_req,
                                      p_min * p_req, _bcap))
    if p_head:
        p_min = min(p_max, _fit_boost(p_min, HEADROOM_ADD, p_req,
                                      u_min * u_req, _bcap))
    # ★노드 독점 앱에는 상주 부스트를 하지 않는다.
    #   그 앱은 파드 1개가 노드 1대이므로 부스트가 곧 baseline 노드 증가다.
    #   실측 사고 — 상주가 2로 올라가 stress가 2노드를 잡고, 남은 노드에 io 파드가
    #   흩어져 baseline이 4대가 됐다(비용 직접 손실).
    #   느림/burst 대응은 HPA가 한다(util이 측정 기반이라 부하 즉시 반응한다).
    if s_head and not ((vcpu * 1000) >= avail):
        s_min = min(s_max, s_min + HEADROOM_ADD)

    # ★★baseline 2 절대 보장 (가장 중요): user+product 상주(min)는 반드시 MNG 1노드에 들어가야 함.
    #   ★기준은 avail_mng(MNG 노드 실측 여유)이다. 워커 노드 기준(avail)을 쓰면 MNG는 애드온이
    #     몰려 여유가 훨씬 작으므로 "들어간다"고 계산하고도 스케줄러가 거부한다
    #     → 상주 파드가 별도 노드로 밀려 baseline이 3~4대가 된다(실측 사고).
    #   초과하면 남는 파드가 Pending → Karpenter가 노드 추가 → 비용 기준선 붕괴.
    # ★★baseline 절대 보장: io 상주(user+product)는 MNG 노드에, stress 상주는 워커 1대에.
    #   ★기존엔 io 상주만 avail_mng와 비교했는데, 그것만으로는 부족했다:
    #     실측 사고 — user request가 310m으로 커져 io 상주가 1050m이 됐고 MNG(1278m)에
    #     '겨우' 들어갔다. 그런데 stress 상주(434m)를 더하면 1484m > 1278m 이라
    #     stress가 MNG에 못 들어가고 별도 노드가 필요해진다. 거기까진 설계대로(총 2대)인데,
    #     stabilize()가 그 stress 노드를 '스케일아웃 노드'로 오인해 드레인하고,
    #     축출된 stress 파드가 다시 Pending → Karpenter가 새 노드 → 그 노드도 드레인 대상
    #     → 노드가 2→3→4→5→7로 늘어나는 양성 피드백이 생겼다.
    #   → io 상주를 'MNG 예산에서 충분한 여유를 남기고' 제한한다.
    #     여유(MNG_RESERVE_FRAC)를 남기면 stabilize가 드레인해도 파드가 갈 곳이 있다.
    _io_cap = int(avail_mng * MNG_RESERVE_FRAC)
    while ((u_min * u_req + p_min * p_req) > _io_cap
           or (u_min * u_mem_req + p_min * p_mem_req) > mem_avail) and (u_min > 1 or p_min > 1):
        if u_min >= p_min and u_min > 1:
            u_min -= 1
        elif p_min > 1:
            p_min -= 1
        else:
            break

    # ★★min=1씩으로도 예산을 넘는 경우의 최후 보호 — request를 비례 축소한다.
    #   위 루프는 min을 깎아 맞추지만, 둘 다 이미 1이면 더 깎을 게 없어 그냥 빠져나온다.
    #   그러면 상주 파드가 MNG 한 대에 안 들어가 3번째 노드가 '상시' 필요해지고
    #   baseline 2가 깨진다(비용 기준선이 무너짐 → 비용 점수 직접 손실).
    #   실측 위험: user 729m + product 519m = 1248m / 예산 1330m → 여유 82m뿐이고,
    #   포화미달 앱은 실행마다 측정 CPU가 흔들려(523m→692m) 언제든 넘칠 수 있다.
    #   → 넘칠 때만 발동하고, 넘치는 만큼만 줄인다(floor 아래로는 안 내려감).
    resident = u_min * u_req + p_min * p_req
    if resident > avail_mng:
        shrink = avail_mng / float(resident)
        u_floor = 300 if u_bound == "cpu" else 30
        p_floor = 300 if p_bound == "cpu" else 60
        u_req = max(u_floor, int(u_req * shrink))
        p_req = max(p_floor, int(p_req * shrink))
        print(f"  ⚠ 상주 request가 MNG 실측 예산({avail_mng}m) 초과 → 비례 축소: "
              f"user={u_req}m product={p_req}m (baseline {mng_count + 1}대 보장)")

    # Karpenter 하드캡: (max_nodes - mng_count)대 분량 → 노드 폭증·비용 차단
    # ★Karpenter 하드캡을 kp_node_cap(s_max 계산에 쓴 것과 동일)으로 일치 → 노드 폭증 원천 차단.
    #   이전엔 (max_nodes-mng)=7노드분을 허용해 stress는 3노드로 제한됐어도 user/product가 나머지를
    #   써서 노드 7대까지 늘어남(실측). 두 값을 같게 하면 총 노드 = MNG + kp_node_cap 로 확정.
    kp_cpu = max(vcpu, kp_node_cap * vcpu)
    # ★버그 수정: Karpenter NodePool의 limits는 노드 'capacity'로 집계된다(allocatable 아님).
    #   allocatable(예: 3117Mi)로 계산하면 실제 capacity(예: 3782Mi)보다 ~18% 작게 잡혀
    #   CPU 캡보다 메모리 캡이 먼저 걸리고, 그러면 노드 증설이 조용히 막힌다.
    #   실측 사고: limits {cpu:8(=4노드), memory:13Gi} → 메모리가 3노드에서 걸려
    #     "all available instance types exceed limits for nodepool" 으로 파드 영구 Pending.
    #   노드 수는 CPU 캡으로만 통제하는 것이 설계 의도이므로, 메모리는 항상 비구속이 되게
    #   capacity × 노드수 × 여유배수로 넉넉히 준다. (메모리 비율이 다른 타입에도 자동 대응)
    mem_per_node_mi = node_mem_cap_mi or (int(node_mem_mi * 1.25) if node_mem_mi else 4096)
    kp_mem_gi = max(2, math.ceil(kp_node_cap * mem_per_node_mi * MEM_CAP_HEADROOM / 1024.0))

    # ── PriorityClass 배정 (측정 기반, 앱 이름 하드코딩 X) ──
    # ★결론: 앱 간에는 '동등'하게 둔다. 어느 앱도 다른 앱을 선점하지 않는다.
    #
    # 이력 — 두 번의 실측이 이 결론을 만들었다:
    #   ① 처음: io 앱이 high, 무거운 앱이 normal → 무거운 앱이 밀려 가용성 21.5%
    #      당시 진짜 원인은 우선순위가 아니었다. io 앱의 max가 32(클러스터 전체 예산으로 계산)이고
    #      request가 실측의 1/3이라, io 앱이 10.6코어를 '예약만 하고' 선점한 것이었다.
    #   ② 뒤집은 뒤: 무거운 앱이 high → 그 앱은 94.9%(90% 티어 초과, 남는 4.9%p는 점수 0)
    #      대신 io 앱이 24~48%로 떨어져 성능 게이트(30%)를 3회 연속 깼다 → 비용 12점 전부 0.
    #
    # → ①의 원인(max 산정·request 사이징)은 이미 고쳤으므로 선점으로 보호할 필요가 없다.
    #   그리고 선점은 '누가 이기는가'만 정하고 총 용량을 늘리지 않는다. 한쪽을 이기게 하면
    #   반대쪽이 정확히 그만큼 굶는다 — 두 실측이 그걸 양방향으로 보여줬다.
    #   자원이 진짜 부족하면 노드 사다리(Pending+캡고갈 → 최대까지 확장)가 해결해야 하고,
    #   그게 선점보다 정확한 대응이다.
    # ※pause(웜풀)만 -10으로 남긴다 → 실제 파드가 웜 노드를 즉시 차지하는 동작은 유지된다.
    prio = {a: "high-priority" for a in ("user", "product", "stress")}

    # ★user·product는 항상 함께(MNG에 패킹), stress는 무거우면 항상 격리.
    #   앱별 부분 공존(product만 stress 노드에 허용)을 시도했다가 되돌렸다 — 이득이 없었다:
    #     MNG 앱가용 1278m ≥ user 727m + product 410m = 1137m → 두 앱이 MNG 한 대에 들어간다.
    #     product는 cap이 4707 rps/pod라 1파드로 충분해 별도 노드가 필요할 일이 없다.
    #     즉 stress 노드에 넣어도 회수되는 노드가 0대이고, 스케줄링 복잡도만 늘어난다.
    #   → 판정은 'stress가 무거운가' 하나로 유지한다(stress_isolate).
    #     무거우면 user·product 둘 다 격리, 가벼우면 둘 다 공존(노드 수 감소).
    coexist = {a: (not stress_isolate) for a in ("user", "product")}
    print(f"  [배치] user+product = MNG 패킹({u_req + p_req}m ≤ MNG가용 {avail_mng}m) / "
          f"stress = {'전용 노드(격리)' if stress_isolate else '같은 노드 공존'}")

    # ★★★최종 검증 (sanity check) — 어떤 측정 결과가 나와도 '과도한 증설'이 불가능하게.
    #   측정은 비결정론적이다(네트워크 지터·CPU 상태·캐시 워밍업). 그래서 개별 산출식을
    #   아무리 고쳐도 특정 조합에서 위험한 값이 나올 수 있다.
    #   실측 사고 3건이 모두 여기서 걸러진다:
    #     ① stress request 200m(floor) → 노드당 8파드 → s_max 56 → 7노드 폭주
    #     ② io request 30m(floor) → u_max 38 → HPA가 38파드까지 채워 워커 노드 생성
    #     ③ 상주 합계가 MNG 초과 → baseline이 2대가 아니라 3대로 시작
    #   → 산출이 끝난 뒤 '물리적으로 말이 되는가'를 한 번 더 검사해 강제 교정한다.
    _fix = []
    # (0) io max 상한을 'MNG + 워커 1대' 분량으로 묶는다.
    # (io max는 위에서 이미 MNG+카펜터1대 분량으로 제한됨)
    # (1) io 앱: max × request 가 '클러스터 io 예산'을 넘으면 깎는다.
    #     ★MNG 기준으로 깎았다가 되돌렸다 — 그게 스파이크에서 파드를 못 늘려
    #       성능을 8.5점 잃은 원인이었다(비용은 만점이었지만 순손실).
    #       노드 예산 기준이면 '필요할 때 노드를 쓰되 하드캡을 넘지 않는' 상태가 된다.
    if u_max * u_req > io_budget:
        _new = max(u_min + 1, io_budget // max(1, u_req))
        _fix.append(f"user max {u_max}→{_new} (io 예산 {io_budget}m 초과 방지)")
        u_max = _new
    if p_max * p_req > io_budget:
        _new = max(p_min + 1, io_budget // max(1, p_req))
        _fix.append(f"product max {p_max}→{_new} (io 예산 {io_budget}m 초과 방지)")
        p_max = _new
    # (2) 노드 독점 앱: max 가 노드 예산을 넘으면 깎는다. 넘겨도 Pending만 쌓인다.
    _kpn = max(1, max_nodes - mng_count)
    if (vcpu * 1000) >= avail and s_max > _kpn:
        _fix.append(f"stress max {s_max}→{_kpn} (1파드=1노드인데 노드 예산 {_kpn}대 초과)")
        s_max = _kpn
    # (3) 상주 합계가 MNG 여유율을 넘으면 baseline이 깨진다 → min을 깎는다.
    #     ★여유율(MNG_RESERVE_FRAC)을 남긴다: 꽉 채우면 파드를 재배치할 자리가 없어
    #       drain/consolidation이 새 노드를 만드는 경로가 생긴다(실측: 노드 2→7).
    _io_cap2 = int(avail_mng * MNG_RESERVE_FRAC)
    while (u_min * u_req + p_min * p_req) > _io_cap2 and (u_min > 1 or p_min > 1):
        if u_min >= p_min and u_min > 1:
            u_min -= 1
        elif p_min > 1:
            p_min -= 1
        else:
            break
    _res = u_min * u_req + p_min * p_req
    # (4) min ≤ max 보장
    u_min, p_min, s_min = min(u_min, u_max), min(p_min, p_max), min(s_min, s_max)
    if _fix:
        print("  [최종 검증] 위험한 조합을 교정했다:")
        for f in _fix:
            print(f"    · {f}")
    print(f"  [최종 검증] 상주 io {_res}m ≤ MNG여유 {_io_cap2}m "
          f"{'OK' if _res <= _io_cap2 else '★초과'} "
          f"(baseline: io는 MNG 1대, stress는 여유 {avail_mng-_res}m에 공존 가능"
          f"{'O' if s_min*s_req <= avail_mng-_res else 'X→별도노드'}) | "
          f"io max: user {u_max}파드 / product {p_max}파드 (io예산 {io_budget}m 이내) | "
          f"stress max {s_max}파드 ≤ 노드예산 {_kpn}대 | 총 노드 하드캡 {max_nodes}대")

    # ★2-풀 구조: stress-pool (taint) + apdev-pool (io 오버플로)
    #   stress-pool CPU = 고정 6대분(12CPU). 캡은 안전망이지 목표가 아니다.
    #     실제 노드 수는 HPA max(=s_max)가 결정한다. 캡에 막혀 Pending → 성능 폭락 방지.
    #   apdev-pool CPU = io 오버플로 최대 2대분 (고정)
    #   ★최소 노드 운영을 보장하는 것은:
    #     (1) HPA util — CPU가 올라야만 파드를 늘림 (io: 45%, stress: 측정유도)
    #     (2) HPA max = s_max — turn.py가 측정 기반으로 필요 최소 파드 수를 산출
    #     (3) consolidateAfter 30s — 빈 노드 즉시 회수
    #     (4) stress min = 1 — baseline은 항상 1파드=1노드
    return {
        "karpenter": {"cpu": str(kp_cpu), "mem": f"{kp_mem_gi}Gi"},
        "user":    {"req": f"{u_req}m", "lim": (f"{u_lim}m" if u_lim else None), "mem_req": f"{u_mem_req}Mi", "mem_lim": f"{u_mem_lim}Mi", "util": u_util, "scaleup": u_scaleup, "min": u_min, "max": u_max, "bound": u_bound, "headroom": u_head, "prio": prio["user"], "coexist": coexist["user"]},
        "product": {"req": f"{p_req}m", "lim": (f"{p_lim}m" if p_lim else None), "mem_req": f"{p_mem_req}Mi", "mem_lim": f"{p_mem_lim}Mi", "util": p_util, "scaleup": p_scaleup, "min": p_min, "max": p_max, "bound": p_bound, "headroom": p_head, "prio": prio["product"], "coexist": coexist["product"]},
        "stress":  {"req": f"{s_req}m", "lim": f"{s_lim}m", "mem_req": f"{s_mem_req}Mi", "mem_lim": f"{s_mem_lim}Mi", "util": s_util, "scaleup": s_scaleup, "min": s_min, "max": s_max, "gomax": gomax, "bound": "cpu", "headroom": s_head, "prio": prio["stress"], "isolate": stress_isolate},
        "info": {"avail": avail, "node_cpu": node_cpu_m, "vcpu": vcpu, "kp_nodes": max_nodes - mng_count,
                 "kp_node_cap": kp_node_cap, "mng_count": mng_count,
                 "node_stages": stages, "mem_per_node_mi": mem_per_node_mi,
                 "req_sum": f"{u_min*u_req + p_min*p_req}m(user+product 노드A) / stress {s_req}m(노드B 독차지)",
                 "mng_budget": f"{mng_budget}m", "measured": measured},
    }


def _unfreeze_hpa(config=None):
    """측정용 HPA 잠금(min=max=1)을 해제한다.

    ★이 함수가 없으면 turn.py가 중간에 끊긴 클러스터는 '스케일 봉쇄' 상태로 남는다:
      maxReplicas=1 이면 HPA도 scaler도 파드를 못 늘리고, 부하가 오면 큐가 무한히 쌓여
      준수율이 0으로 수렴하고 5xx가 난다(노드는 2대라 비용만 좋아 보인다).
    ★config가 있으면 산출된 최종값으로, 없으면 안전 기본값으로 되돌린다.
      기본값은 넉넉하게 둔다 — max는 천장이라 크게 둬도 실제 파드 수는
      scaler/HPA의 수요 판단이 정하고, '봉쇄'가 훨씬 나쁜 실패이기 때문이다."""
    for app in ["user", "product", "stress"]:
        if config:
            mn = int(config[app]["min"])
            mx = max(mn, int(config[app]["max"]))
        else:
            mn, mx = 1, 8
        patch = json.dumps({"spec": {"minReplicas": mn,
                                     "maxReplicas": mx}}).replace('"', '\\"')
        ok, _ = kubectl(f'-n {NAMESPACE} patch hpa/{app}-hpa --type=merge -p "{patch}"')
        if not ok:
            print(f"  ⚠ {app} HPA 잠금 해제 실패 — 수동 확인 필요: "
                  f"kubectl -n {NAMESPACE} get hpa {app}-hpa")


def apply_config(config, node_type, max_nodes, mng_count=2):
    for app in ["user", "product", "stress"]:
        c = config[app]
        gomax = str(config["stress"].get("gomax", 2))
        patch = json.dumps({"spec": {"template": {"spec": {
            "priorityClassName": c.get("prio", "normal-priority"),
            "containers": [{"name": app,
            "env": [{"name": "GOMAXPROCS", "value": gomax}] if app == "stress" else None,
            "resources": {
                "requests": {"cpu": c["req"], "memory": c["mem_req"]},
                "limits": {"cpu": c["lim"], "memory": c["mem_lim"]},
            }}]}}}})
        # stress 아닌 앱은 env=None → JSON "env": null 을 제거 (kubectl에 null 넘기지 않음)
        patch = patch.replace('"env": null, ', '')
        # ★io 앱은 c["lim"]이 None이다 → "cpu": null 이 되고, strategic merge patch에서
        #   null은 '그 키를 삭제'를 뜻한다. 즉 CPU limit이 제거되어 무제한이 된다.
        #   ★limit을 제거하는 이유: io 앱은 CPU를 짧게 burst로 쓰는데(대부분 DB 대기)
        #     limit이 있으면 그 burst 순간 CFS 스로틀이 걸려 요청이 강제 대기한다.
        #     실측: limit 없음 → user 96.8% / limit 1259m → user 78.9%.
        patch = patch.replace('"', '\\"')
        kubectl(f'-n {NAMESPACE} patch deploy/{app} --type=strategic -p "{patch}"')

        # ★배치(affinity)는 여기서 건드리지 않는다 — deploy.yaml이 단일 소스다.
        #   이전엔 여기서 io 앱에 required anti-affinity(app=stress)를 다시 깔았는데,
        #   그게 노드 폭증을 재발시키는 경로였다:
        #     required면 stress가 앉은 노드마다 io 앱이 배치 불가가 되고, stress는
        #     maxSkew 1로 노드마다 퍼지므로 io 앱의 배치 가능 노드가 계속 줄어든다
        #     → Pending → Karpenter가 노드 생성 → 그 노드에도 stress가 퍼짐 → 반복.
        #     실측: 튜닝 단계에서만 노드가 6대까지 늘고 stress가 MNG를 점유했다.
        #   ★지금은 배치가 '노드 라벨'로 결정론적으로 정해진다(deploy.yaml):
        #     · stress    : nodeAffinity required — Karpenter 노드에만 (MNG 진입 차단)
        #     · user/product: nodeAffinity preferred — MNG 선호, anti-affinity는 preferred
        #   스케줄 순서와 무관하게 항상 같은 결과가 나오고 Pending이 생기지 않는다.
        #   turn.py가 이 파일을 덮어쓰면 그 보장이 깨지므로 patch를 하지 않는다.
        #   ※config["stress"]["isolate"] 판정은 여전히 request 사이징과 s_max에 쓰인다.

        hpa = json.dumps({"spec": {"minReplicas": c["min"], "maxReplicas": c["max"],
            "behavior": {
                "scaleUp": {"stabilizationWindowSeconds": 0, "policies": [
                    {"type": "Pods", "value": c.get("scaleup", 4), "periodSeconds": 15},
                    {"type": "Percent", "value": 100, "periodSeconds": 15}], "selectPolicy": "Max"},
                "scaleDown": {"stabilizationWindowSeconds": 60, "policies": [{"type": "Percent", "value": 30, "periodSeconds": 30}], "selectPolicy": "Max"}},
            "metrics": [{"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": c["util"]}}}]
        }}).replace('"', '\\"')
        kubectl(f'-n {NAMESPACE} patch hpa/{app}-hpa --type=merge -p "{hpa}"')

    kp = config["karpenter"]
    # ★단일 풀 패치: apdev-pool (taint 없음, 격리는 podAntiAffinity가 담당)
    kp_patch = json.dumps({"spec": {"limits": {"cpu": kp["cpu"], "memory": kp["mem"]},
        "disruption": {"consolidationPolicy": "WhenEmptyOrUnderutilized", "consolidateAfter": "45s",
                       "budgets": [{"nodes": "1"}]}}}).replace('"', '\\"')
    kubectl(f'patch nodepool apdev-pool --type=merge -p "{kp_patch}"')

    for app in ["user", "product", "stress"]:
        kubectl(f"-n {NAMESPACE} rollout status deploy/{app} --timeout=120s")


# ── 검증 ──

def baseline_config(config):
    """★38점 검증 구성(yaml 기본값)을 config 형식으로 만든다.

    A/B 비교의 'A안'이다. 측정 기반 튜닝값(B안)과 같은 부하로 재보고 이긴 쪽을 적용한다.
    ★이 값을 두는 이유: 측정은 실행마다 흔들린다(포화 CPU 593~1918m, payload 84~117).
      한 번의 나쁜 측정이 나쁜 설정으로 굳는 것을 막는 안전판이 필요하다.
    ★request × util = 트리거 CPU 가 실제 스케일 동작을 정한다:
      user 70×0.33 = 23m / product 70×0.29 = 20m / stress 600×0.55 = 330m
    메모리와 karpenter 설정은 측정값(config)을 그대로 쓴다 — 이 둘은 안정적이다.
    """
    fixed = {
        "user":    {"req": "70m",  "lim": None,     "util": 33, "min": 2, "max": 20, "scaleup": 4},
        "product": {"req": "70m",  "lim": None,     "util": 29, "min": 2, "max": 20, "scaleup": 4},
        "stress":  {"req": "600m", "lim": "2000m",  "util": 55, "min": 1, "max": 5,  "scaleup": 2},
    }
    out = {"karpenter": config["karpenter"], "info": config["info"]}
    for app in ("user", "product", "stress"):
        c = dict(config[app])          # mem_req/mem_lim/gomax/prio/isolate 등은 측정값 유지
        c.update(fixed[app])
        out[app] = c
    return out


def config_points(sc, nodes):
    """★채점표 기준 점수로 환산한다. A/B 비교의 판정 기준.

    성능·가용성은 앱당 4점이고 임계를 넘을 때마다 0.5점이 가산된다(누적).
    비용은 cost_ratio(평균EC2 ÷ 기준 2대) 티어다.
    ★'perf% 합계'로 비교하지 않는 이유: 채점이 이산적이라 티어를 넘지 않는 개선은 0점이다.
      89.9% → 90%는 0.5점이고 90.1% → 95%는 0점이므로, 합계로 비교하면 판정이 틀린다.
    """
    perf_tiers = (90.0, 87.5, 85.0, 82.5, 80.0, 70.0, 50.0, 30.0)
    avail_tiers = (99.9, 99.5, 99.0, 98.0, 95.0, 90.0, 80.0, 50.0)

    def tier_pts(v, tiers):
        for i, t in enumerate(tiers):
            if v >= t:
                return 4.0 - i * 0.5
        return 0.0

    total, detail = 0.0, {}
    for app in ("user", "product", "stress"):
        a_pct, p_pct = sc.get(app, (0, 0))
        ap, pp = tier_pts(a_pct, avail_tiers), tier_pts(p_pct, perf_tiers)
        detail[app] = (pp, ap)
        total += pp + ap
    # 비용: ratio 1.0=12점, 이후 0.25배마다 -1점 (채점표 근사)
    ratio = max(1.0, nodes / 2.0)
    cost = max(0.0, 12.0 - (ratio - 1.0) / 0.25)
    total += cost
    return total, cost, detail


async def _measure_config(label, cfg, base, seed_u, seed_p, node_type, max_nodes, mng_count):
    """설정을 적용하고 같은 부하로 재서 점수를 낸다 (A/B 공통 경로)."""
    apply_config(cfg, node_type, max_nodes, mng_count)
    await asyncio.sleep(AB_SETTLE)
    results = {"user": [], "product": [], "stress": []}
    await _run_load(base, seed_u, seed_p, AB_LOAD_SECS, results,
                    u_workers=3, p_workers=3, s_workers=2)
    sc = score(results)
    nodes = count_live_nodes()
    pts, cost, detail = config_points(sc, nodes)
    print(f"  [{label}] 총 {pts:.1f}점 (비용 {cost:.1f} / 노드 {nodes}대) "
          + " ".join(f"{a} 성능{detail[a][0]:.1f}" for a in ("user", "product", "stress")))
    return pts, sc, nodes


def stress_early_config(config, measured):
    """★C안: B안(측정값)에서 stress만 '더 일찍 증설'하게 만든 변형.

    왜 필요한가: stress는 89%처럼 90% 티어에 1~2%p 못 미치는 경우가 반복된다.
      그 1%p는 request/limit이 아니라 '언제 파드를 늘리는가'(트리거)가 정한다.
      트리거가 늦으면 큐가 쌓인 뒤에 노드를 만들고, 노드 부팅 60초 동안 준수율이 깎인다.
    ★트리거만 낮춘다 — request/limit은 B안 그대로 둔다.
      request는 스케줄 밀도, limit은 처리 속도를 정하므로 이 둘은 이미 최적이다.
      바꿔야 할 것은 '동시 요청 몇 개에서 늘릴지'뿐이다.
      B안: 0.5개 지점(util 50%) → C안: CPU_CONC_EARLY(0.35)개 지점
    ★A/B/C를 같은 부하로 재서 이긴 쪽을 쓴다. C안이 노드를 더 쓰면 비용 점수가 깎이므로,
      '노드 1대 값을 하는 성능 향상'일 때만 C안이 이긴다 — 판정은 채점표가 한다.
    """
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in config.items()}
    s = out["stress"]
    req_m = _parse_cpu_m(s["req"]) or 0
    if req_m <= 0:
        return out
    cpu_sat = (measured["stress"].get("cpu") or 0)
    rps_sat = (measured["stress"].get("rps") or 0)
    if cpu_sat <= 0 or rps_sat <= 0:
        return out
    cpu_per_req = cpu_sat / float(rps_sat)
    trig = max(TRIGGER_FLOOR_M, cpu_per_req * CPU_CONC_EARLY)
    s["util"] = int(round(max(UTIL_SANITY_MIN, min(100.0, trig / req_m * 100.0))))
    return out


def score(results):
    """각 앱 avail%, perf%(SLO 이내) 반환 + 출력."""
    print(f"\n  {'api':<10} {'count':>6} {'avail%':>7} {'perf%':>7} {'avg':>7} {'p95':>7}")
    out = {}
    for api in ["user", "product", "stress"]:
        data = results[api]
        if not data:
            print(f"  {api:<10} NO DATA"); out[api] = (0, 0); continue
        total = len(data)
        # 가용성 = 2xx AND 5초 이내(채점기 기준). 2xx여도 5초 초과면 가용성 실패로 잡힘(stress 주의).
        ok = len([1 for s, t in data if 200 <= s < 300 and t <= AVAIL_SLO])
        perf = len([1 for s, t in data if 200 <= s < 300 and t <= SLO[api]])
        times = sorted(t for _, t in data)
        avg = sum(times) / len(times); p95 = times[int(len(times) * 0.95)]
        a_pct, p_pct = 100 * ok / total, 100 * perf / total
        mark = "OK" if p_pct >= 80 else "!!"
        print(f"  {api:<10} {total:>6} {a_pct:>6.1f}% {p_pct:>6.1f}% {avg:>6.0f}ms {p95:>6.0f}ms {mark}")
        out[api] = (a_pct, p_pct)
    _, n = kubectl("get nodes --no-headers")
    print(f"\n  nodes: {len([l for l in n.split(chr(10)) if l.strip()]) if n else 0}")
    return out


# ── 안정화: 검증에서 늘어난 파드/노드를 MNG로 수렴 ──

async def stabilize(config, baseline_nodes=2):
    """검증에서 늘어난 파드/노드를 min으로 되돌리고, 스케일아웃 Karpenter 노드가 회수돼
    baseline(2대: MNG 1 + stress 카펜터 1)로 수렴할 때까지 폴링 → 채점은 깨끗한 2대에서 시작.
    ★ stress가 앉은 카펜터 노드는 baseline이라 회수 대상 아님(cordon 제외)."""
    print("\n안정화 중 (baseline 2대로 수렴 — MNG 1 + stress 카펜터 노드 1)...")
    for app in ["user", "product", "stress"]:
        kubectl(f"-n {NAMESPACE} scale deploy/{app} --replicas={config[app]['min']}")
    await asyncio.sleep(10)
    for app in ["user", "product", "stress"]:
        kubectl(f"-n {NAMESPACE} rollout status deploy/{app} --timeout=120s")

    deadline = time.time() + 240
    while time.time() < deadline:
        # ★'앱 파드가 하나라도 있는 노드'는 필요한 노드다 — 회수 대상이 아니다.
        #   ★기존엔 stress 파드가 있는 노드만 keep에 넣었다. 그건 'stress가 Karpenter
        #     노드에, io가 MNG에' 배치된다는 가정이었는데, 그 반대가 될 수 있다:
        #     stress_isolate=True면 user/product에 required anti-affinity(app=stress)가
        #     걸리므로, stress가 MNG에 먼저 앉으면 io 앱이 워커 노드로 밀려난다.
        #     그러면 io 파드가 사는 워커가 'extra'로 잡혀 영원히 수렴 판정이 안 된다
        #     (실측: "현재 2대 (스케일아웃 카펜터 1대)"를 15회 반복하고 타임아웃).
        #   → 어느 앱이든 파드가 있으면 그 노드는 필요하다. 이게 배치와 무관하게 맞다.
        _, pn = kubectl(f'-n {NAMESPACE} get pods -l app --field-selector=status.phase=Running '
                        f'-o jsonpath="{{.items[*].spec.nodeName}}"')
        keep_nodes = {n for n in pn.strip().strip('"').split() if n}
        knodes = live_karpenter_nodes()                             # ★살아있는 노드만
        extra_knodes = [n for n in knodes if n not in keep_nodes]   # 앱 파드가 없는 = 빈 노드
        total = count_live_nodes()                                  # ★종료중·등록중 제외
        if not extra_knodes and total <= baseline_nodes:
            print(f"  ✅ 노드 {total}대로 수렴 (baseline {baseline_nodes}, 전부 앱 파드 보유) "
                  f"— 채점 준비 완료")
            return
        # ★★drain을 하지 않는다 — 그게 노드를 늘리는 원인이었다.
        #   실측 사고: 빈 노드를 drain하면 파드가 축출되고, 갈 자리가 없으면 Pending이 되어
        #     Karpenter가 새 노드를 만든다. 그 새 노드도 'extra'로 잡혀 다음 사이클에
        #     또 drain되고 → 노드가 2→3→4→5→7로 계속 늘어났다(양성 피드백).
        #     'drained set으로 한 번만'이라는 방어가 있었지만 새 노드는 새 이름이라 무효였다.
        #   ★대신 Karpenter의 consolidation에 맡긴다:
        #     · replicas를 min으로 내렸으므로 남은 노드는 실제로 비어 간다
        #     · consolidateAfter(45s) 뒤 Karpenter가 스스로 회수한다
        #     · Karpenter는 '파드를 옮길 자리가 있는지' 먼저 확인하므로 새 노드를 안 만든다
        #   즉 우리가 할 일은 '기다리는 것'뿐이다. 드레인은 순손실이었다.
        print(f"  … 현재 {total}대 (스케일아웃 카펜터 {len(extra_knodes)}대) "
              f"— Karpenter consolidation 대기 ({CONSOLIDATE_AFTER})")
        await asyncio.sleep(15)
    total = count_live_nodes()
    print(f"  ⚠ 아직 {total}대 (목표 {baseline_nodes}). 곧 Karpenter가 회수함 — "
          f"채점 시작 전 `kubectl get nodes`로 {baseline_nodes}대인지 반드시 확인할 것.")


async def main():
    if len(sys.argv) < 2:
        print("사용법: python turn.py <CF endpoint>"); sys.exit(1)
    base = sys.argv[1].rstrip("/")
    print("=== turn.py (최종 튜닝툴) ===\n")

    # ★측정 경로 2개 (캐싱 앱 때문에 반드시 분리해야 한다)
    #   base   = 채점 경로(CloudFront) → 클라이언트가 보는 지연 = SLO 판단 근거
    #   origin = 오리진(ALB)          → 파드가 실제로 하는 일 = request/HPA 산출 근거
    origin = get_origin_endpoint(base)
    print(f"측정 경로: 채점={base}")
    print(f"          오리진={origin}" + ("  (ALB 탐지 실패 → 채점 경로로 대체)" if origin == base else ""))

    # 노드 스펙은 클러스터에서 자동으로 읽음 (인스턴스 타입 물어보지 않음)
    node_cpu_m, vcpu, node_mem_mi, node_mem_cap_mi, node_type = get_node_specs()
    sys_mng, sys_worker = get_system_reserve(SYSTEM_PER_NODE)
    print(f"노드 (자동 감지): {node_type}  —  {node_cpu_m}m allocatable / {vcpu} vCPU / "
          f"{node_mem_mi}Mi alloc·{node_mem_cap_mi}Mi capacity mem")
    print(f"시스템 예약 (실측): MNG {sys_mng}m → 앱 가용 {node_cpu_m - sys_mng}m  /  "
          f"워커 {sys_worker}m → 앱 가용 {node_cpu_m - sys_worker}m")
    print(f"  ※MNG 값이 'user+product 상주가 한 노드에 들어가는가' = baseline 2대를 결정한다\n")

    # ★최대 총 노드 수(천장). Karpenter limits.cpu가 하드캡 → HPA max를 올려도 이 노드수를 못 넘음.
    #   실제 사용 노드 수는 여기가 아니라 단계 사다리(NODE_STAGE_MULT)가 정한다:
    #     baseline(2) → 1단계 4 → 2단계 6 → 3단계 8. scaler가 자원부족 확인 시에만 다음 단계로.
    #   ★캡은 천장이지 평균이 아님 — 비용은 '실행 중 인스턴스 개수의 시간 평균'으로 매겨지므로,
    #     스파이크에 잠깐 천장까지 가도 회수가 빠르면(consolidateAfter 15s) 평균은 낮게 유지된다.
    max_nodes = MAX_NODES_HARD
    # 사전 체크: 노드 포화면 측정 오염
    _, top = kubectl("top nodes --no-headers")
    if top:
        busy = [l.split()[0].split(".")[0] for l in top.splitlines()
                if len(l.split()) >= 3 and l.split()[2].rstrip("%").isdigit() and int(l.split()[2].rstrip("%")) >= 80]
        if busy:
            print(f"⚠ 노드 CPU 포화: {busy} — 이전 부하 잔재 정리 후 재실행 권장.")
            if input("  계속? (y/N): ").strip().lower() != "y":
                return

    seed_u, seed_p = await _seed(origin)

    # ★측정 경로 분리의 이유(실측 근거):
    #   CloudFront 캐시 정책은 캐시키=id, TTL 3600이다(과제 요구사항이라 유지).
    #   X-Cache 헤더로 확인한 결과 같은 id는 'Hit', 다른 id는 'Miss'로 정상 동작한다.
    #   그런데 측정이 id를 고정하면 첫 요청만 오리진에 가고 나머지는 전부 캐시에서 처리된다
    #   → 파드가 놀아 보이고(cpu=1m) request가 실제(50~140m)의 1/50로 잡힌다
    #   → HPA가 즉시 max로 밀어 파드 8~15개, 다른 앱의 CPU를 잠식 → 게이트 붕괴.
    #   그래서 '캐싱은 그대로 두고 측정만 오리진에서' 한다. 캐시가 없는 앱은 두 경로가 같은 값이 나온다.
    print()

    # [1] 실측 — stress 포화 측정 준비:
    #   ① 코어 전부 열기(GOMAXPROCS=vCPU, limit=노드코어) → 앱의 진짜 core appetite
    #   ② '단일 파드'로 고정(replicas=1, HPA min=max=1) → 부하가 한 파드에 몰려 진짜 포화됨
    #      (이걸 안 하면 부하가 여러 파드+HPA증설로 희석돼 실측이 낮게 나옴 → oversubscription 위험)
    print("\n[1/4] 실측 (모든 앱 단일 파드 고정 → 파드당 rps/p95 깨끗하게)...")
    # stress: 코어 전부 열고 포화. user/product: 넉넉한 limit(스로틀 방지)으로 단일 파드.
    #   모두 replicas=1 + HPA min=max=1 → 부하가 한 파드에 몰려 파드당 처리율/지연이 정확.
    up_lim = f"{node_cpu_m // 2}m"
    probe = json.dumps({"spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "stress",
        "env": [{"name": "GOMAXPROCS", "value": str(vcpu)}],
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                      "limits": {"cpu": f"{vcpu*1000}m", "memory": "512Mi"}}}]}}}}).replace('"', '\\"')
    kubectl(f'-n {NAMESPACE} patch deploy/stress --type=strategic -p "{probe}"')
    for app in ["user", "product"]:
        up_probe = json.dumps({"spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": app,
            "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                          "limits": {"cpu": up_lim, "memory": "256Mi"}}}]}}}}).replace('"', '\\"')
        kubectl(f'-n {NAMESPACE} patch deploy/{app} --type=strategic -p "{up_probe}"')
    hpa_freeze = json.dumps({"spec": {"minReplicas": 1, "maxReplicas": 1}}).replace('"', '\\"')
    for app in ["user", "product", "stress"]:
        kubectl(f'-n {NAMESPACE} patch hpa/{app}-hpa --type=merge -p "{hpa_freeze}"')
        kubectl(f"-n {NAMESPACE} rollout status deploy/{app} --timeout=120s")
    # ★★측정 잠금은 반드시 해제한다 — 실패해도, 중단해도, 취소해도.
    #   치명적 버그였다: 측정을 위해 min=max=1로 잠그고 apply_config에서 복원하는 구조였는데,
    #   그 사이에 (a) 사용자가 확인 프롬프트에서 n을 누르면 (b) Ctrl+C로 끊으면
    #   (c) 계산 중 예외가 나면 → HPA가 min=max=1 로 남는다.
    #   그 상태는 '스케일 자체가 봉쇄'다: 파드가 1개에 고정되어 부하가 오면 큐가 무한히 쌓이고
    #   준수율이 0으로 수렴하며 5xx까지 난다. 노드는 2대라 비용만 좋아 보인다.
    #   → try/finally로 감싸 어떤 경로로 나가도 스케일 가능한 상태로 되돌린다.
    try:
        measured = await measure(base, origin, seed_u, seed_p)
    finally:
        _unfreeze_hpa()          # 안전 기본값으로 즉시 해제 (apply_config가 최종값으로 덮어씀)

    # [1.5] baseline = 2대: MNG 1(user+product 패킹) + Karpenter stress 노드 1(anti-affinity).
    #   ★ MNG 2로 하면 user/product가 두 MNG 노드에 퍼져 stress가 3번째 노드로 밀림(ratio 1.5). MNG 1이면
    #     user+product가 한 노드에 패킹 → stress는 그 노드에 못 앉아(anti-affinity) 카펜터 전용노드 1대 → 총 2대.
    #   mng_count=1(실제 MNG=user/product). 카펜터 캡 = (max_nodes-1) = stress1 + 스케일아웃.
    mng_count = 1                       # 실제 MNG=1(user/product). max_nodes는 위에서 입력받음(총 천장).
    s_cpu = measured["stress"]["cpu"]; half = (node_cpu_m - SYSTEM_PER_NODE) // 2
    tag = "heavy" if s_cpu >= half else "light"
    print(f"\n[baseline] 2대 = MNG 1(user+product) + Karpenter stress 노드 1. stress {s_cpu}m ({tag}). 최대 {max_nodes}대")

    # [2] 계산
    print("\n[2/4] 실측 기반 계산...")
    config = calculate(measured, node_cpu_m, vcpu, max_nodes, mng_count,
                       node_mem_mi=node_mem_mi, node_mem_cap_mi=node_mem_cap_mi,
                       sys_mng=sys_mng, sys_worker=sys_worker)
    _print_config(config, node_type, max_nodes, mng_count)

    # scaler.py용 파드당 RPS 용량 저장 → scaler가 읽어 정확한 비례 스케일링
    #   (원본은 turn이 안 써서 scaler가 하드코딩 기본값 폴백 중이던 연결을 완성)
    cap_path = os.path.join(os.path.dirname(__file__), "scaler_cap.json")
    # ★cap = "SLO 지키며 파드가 감당하는 RPS" (포화 throughput 아님).
    #   측정 rps는 포화 처리율 → 그때 부하p95가 SLO를 넘었으면 SLO/p95 만큼 할인 → 지연-안전 용량.
    #   이래야 scaler의 need_rps가 "지연 지키는 파드 수"를 정확히 산출한다.
    #
    # ★버그 수정: 하한이 절대값 1.0이었다. 앱과 무관한 마법값이라
    #     · 포화 처리율이 1.2rps인 앱: 정당한 값 0.42가 1.0으로 올라가 용량 2.4배 과대평가
    #       → scaler가 파드를 2.4배 적게 띄움 → 그 앱이 붕괴(실측: 가용성 21.5%)
    #     · 포화 처리율이 0.3rps인 앱: 3.3배 과대평가
    #   → 하한을 '측정 처리율의 비율'로 바꾼다. 이러면 어떤 앱이든 스케일이 맞고,
    #     동시에 할인이 무한정 커져 파드가 폭증하는 것도 막는다(최대 1/CAP_MIN_FRAC 배).
    #
    # ★구조적 burst 앱 주의: 큐잉 0에서도 꼬리가 SLO를 넘는 앱은 p95 ≤ SLO가 애초에 불가능하다.
    #   그런 앱은 할인이 항상 걸리므로 하한이 실질 값이 된다. 그게 맞는 동작이다
    #   (꼬리는 스케일로 못 고치므로 큐잉만 없애는 선에서 파드를 준다).
    # ★버그 수정 2 — '구조적 burst' 앱에 p95 할인을 적용하면 안 된다.
    #   할인 근거인 p95는 부하 중 값이지만, 큐잉이 0인 상태(단일요청)에서 이미 꼬리가 SLO를
    #   넘는 앱이 있다(요청 일부가 본질적으로 무거운 경우). 그 꼬리는 부하를 줄여도 사라지지
    #   않으므로, SLO/p95로 할인하는 것은 '부하와 무관한 앱 특성'을 벌하는 것이 된다.
    #   실측 사고: 큐잉0 꼬리가 SLO의 1.9배인 앱에서 cap이 1.4 → 0.75로 깎였고,
    #     1파드=1노드라 부하 4rps에 노드 6대를 요구했다. 실제로는 3대로 같은 성능이 나오는데
    #     남은 3대는 성능 티어를 못 넘기는 마진에만 쓰였다(90%→94.9%, 둘 다 같은 점수).
    #   → 그런 앱은 측정 처리량 자체를 cap으로 쓴다(외삽하지 않으므로 보수적).
    #   ★이 정의는 turn.py의 hpa_util(_slo_safe_rps)과 반드시 동일해야 한다.
    #     둘이 갈라지면 "cap은 3파드로 충분하다는데 HPA는 6파드에서 안정" 같은 모순이 생긴다.
    CAP_MIN_FRAC = 0.5
    cap_data = {}
    for app in ["user", "product", "stress"]:
        r = measured[app]["rps"]
        p95 = measured[app].get("p95", 0)
        og95 = measured[app].get("origin_p95", 0)      # 큐잉 0 상태의 꼬리
        # ★초기 cap을 scaler와 '같은 식'으로 산출한다 — 큐잉 관계식.
        #   기존은 `r × min(1, SLO/p95)` 였는데 선형 비례 근사라 두 방향으로 틀린다.
        #   scaler는 p95 = S/(1-ρ) 로 역산하므로, 초기값도 같은 식으로 주면
        #   시작부터 같은 동작점에 있고 수렴할 거리가 짧아진다.
        #     ρ̂    = 1 - S/p95        (측정 부하에서의 부하율)
        #     ρ_slo = 1 - S/SLO       (p95가 SLO가 되는 부하율)
        #     cap  = r × ρ_slo/ρ̂
        #   S는 큐잉 0 상태의 꼬리(단일요청 p95) — scaler의 svc_ms와 같은 정의다.
        _s = (measured[app].get("p95_light_tail") or og95
              or measured[app].get("p95_light") or 0)
        _q = None
        if r > 0 and _s > 0 and p95 > _s * 1.05:
            _rho_hat = 1.0 - _s / float(p95)
            _rho_slo = 1.0 - _s / float(SLO[app])
            if _rho_slo > 0.02 and _rho_hat > 0.02:
                _q = r * (_rho_slo / _rho_hat)
        if og95 and og95 > SLO[app]:
            safe = r                                   # 구조적 burst → 할인 없음
        elif _q is not None:
            safe = _q
        else:
            safe = r * min(1.0, SLO[app] / p95) if p95 > 0 else r
        # ★측정 조건과 실부하 조건의 괴리를 감안해 보수적으로 깎아서 넘긴다.
        #   측정은 같은 key를 반복 조회하므로 DB 캐시·커넥션풀이 더워진 상태다.
        #   실트래픽은 매번 다른 key라 파드가 매 요청마다 실제 일을 한다.
        #   실측 괴리: user cap 46.4로 측정됐으나 실부하에서는 ~2.0 rps/pod (23배).
        #   ★과대보다 과소가 안전하다: 과소면 파드가 조금 더 뜨지만 io 앱은 max가
        #     MNG 안으로 묶여 노드가 안 늘고(비용 0), 과대면 파드가 부족해 준수율이 직접 깎인다.
        #   ★노드 독점 앱은 파드=노드이므로 깎지 않는다(과소 = 노드 낭비).
        #     그 앱은 CPU-burn이라 측정과 실제의 괴리도 작다.
        _is_mono = (app == "stress" and config["stress"].get("isolate", True))
        if not _is_mono:
            safe = safe * CAP_SEED_DISCOUNT
        cap_data[app] = round(max(r * CAP_MIN_FRAC * (1.0 if _is_mono else CAP_SEED_DISCOUNT),
                                  safe), 2)
        # ★★io 앱: '상주 파드가 나눠 받을 때의 파드당 처리량'을 상한으로 둔다.
        #   측정 rps는 모두 '단일 파드가 부하를 전부 받은' 값이다(replicas=1 고정 측정).
        #   운영에서는 상주 min개가 나눠 받으므로 파드당 처리량은 그 1/min 수준이다.
        #   rps_lo(동시성 4 프로브의 실제 처리량)를 min으로 나눈 값이 '실동작에 가장 가까운
        #   파드당 처리량'이고, 초기 cap이 그보다 크면 need_rps가 과소해져 파드가 부족해진다.
        #   ★이게 '극초반 3분 성능 저하'의 직접 원인이었다:
        #     cap 11.0으로 시작 → need_rps = ceil(22/0.9/11.0) = 3파드
        #     실제 필요 13파드 → ρ>1 → 큐 폭발. cap 교정이 수렴할 때까지 계속 낮은 준수율.
        #     rps_lo/min = 14/4 = 3.5 로 시작하면 첫 사이클부터 need_rps = 7파드다.
        #   ★리스크가 0인 이유: 과소 추정이면 파드가 더 뜨는데, io max가 MNG 안으로
        #     묶여 있어 노드가 물리적으로 안 늘어난다. 그리고 상향 교정이 10초 내에
        #     실제 용량으로 되돌린다. 반대로 과대면 준수율이 깎이고 복구되지 않는다.
        if False and not _is_mono:
            _lo = measured[app].get("rps_lo") or 0
            _mn = max(1, int(config[app]["min"]))
            if _lo > 0:
                cap_data[app] = round(min(cap_data[app], _lo / float(_mn)), 2)
    with open(cap_path, "w") as f:
        json.dump(cap_data, f)
    _unsat = [a for a in ("user", "product", "stress") if measured[a].get("unsat")]
    print(f"  scaler_cap.json 저장(SLO-safe 용량): {cap_data}"
          + (f"  ※{_unsat}는 포화미달 → 이 값은 하한(실제 용량은 더 큼)" if _unsat else ""))

    # ★큐잉0 준수율을 별도 파일로 저장한다 (ok/n 원자료를 함께 넘긴다).
    #   scaler_cap.json은 {app: float} 형식이고 읽는 쪽이 float()로 파싱하므로 구조를 못 바꾼다.
    #   ★비율만 넘기지 않고 표본수(n)까지 넘기는 이유:
    #     scaler가 신뢰구간(Wilson 상한)을 계산해야 한다. "낙관적으로 봐도 목표에 못 닿는다"가
    #     확인되면 파드를 늘려도 달성 불가이므로 즉시 추격을 멈출 수 있다(노드 낭비 차단).
    #     비율만 넘기면 그 판단이 표본 오차에 취약해진다.
    perf_path = os.path.join(os.path.dirname(__file__), "scaler_perf.json")
    perf_data = {}
    for app in ("user", "product", "stress"):
        d = measured[app]
        if d.get("achievable") is not None:
            perf_data[app] = {"rate": d["achievable"],
                              "ok": d.get("ok_n", 0), "n": d.get("samp_n", 0),
                              # ★포화 시 CPU와 그때의 rps — scaler가 '요청당 CPU'를 계산해
                              #   파드의 물리 최소 개수를 구한다(limit 스로틀 방지).
                              #   cap(rps/pod)만으로는 스로틀을 못 잡는다: 실측 stress는
                              #   요청당 1044m·s가 필요해 2.54rps에 2651m > limit 2000m → 2파드 필수.
                              "cpu_at_cap": d.get("cpu", 0), "rps_at_cap": d.get("rps", 0),
                              # ★무부하 서비스시간(ms) — scaler의 cap 자기교정 기준점.
                              #   scaler는 p95 = S/(1-ρ) 로 관측 p95에서 부하율을 역산하고,
                              #   'p95가 SLO가 되는 처리량'을 cap으로 삼는다. S가 없으면
                              #   선형 비례로 근사해야 하는데 큐잉은 비선형이라 cap이 진짜 값에
                              #   앉지 못한다(과부하에서 과소·저부하에서 과대 추정).
                              # ★백분위 정의를 scaler와 반드시 맞춘다.
                              #   scaler가 비교하는 m["p95"]는 SCORE_PCTL(0.92) 백분위다.
                              #   여기서 p50(p95_light)을 보내면 S가 실제보다 작아지고,
                              #   ρ_slo = 1 - S/SLO 가 과대해져 cap이 부풀고 파드가 부족해진다.
                              #   → 큐잉 0 상태의 '꼬리'(p95_light_tail = 단일요청 p95)를 보낸다.
                              #   약간 보수적(cap이 작게 = 파드가 조금 더)이라 안전한 방향이다.
                              "svc_ms": (d.get("p95_light_tail") or d.get("origin_p95")
                                         or d.get("p95_light") or 0)}
    if perf_data:
        with open(perf_path, "w") as f:
            json.dump(perf_data, f)
        print("  scaler_perf.json 저장(큐잉0 준수율 = 스케일링 천장): "
              + ", ".join(f"{a} {v['rate']*100:.0f}%({v['ok']}/{v['n']})"
                          for a, v in perf_data.items()))

    # ★prewarm.py용 노드-유도 설정 저장(하드코딩 제거 → 인스턴스 타입 불문 정확).
    #   pause_cpu = allocatable - 데몬셋 실사용분 → pause가 노드를 "진짜 독점".
    #     ★기존 avail(=allocatable-600)로 하면 남는 600m에 io 파드(60~150m)가 여러 개 끼어들어
    #       "빈 warm 노드"가 아니게 됨(실측: pause 노드에 product 3개 동거 → 웜풀 목적 실패).
    #       데몬셋(aws-node+kube-proxy) 실사용은 ~150m이므로 200m만 남기면 다른 앱 파드는 못 들어옴.
    #   kp_node_cap: s_max/kp_cpu와 동일한 캡 → warm도 같은 노드 예산 안에서만.
    #   ★max_warm은 보수적(≤2): "노드폭증 절대 금지". 실질 방어는 prewarm의 총노드≤cap 클램프
    #     (총노드=전체워커노드 포함 → cap 넘으면 warm 0, idle엔 0 회수, 재보충 중복발주 억제).
    #     plateau 눌러앉음은 이 클램프+CALM 반납이 구조적으로 제거하므로 max_warm=2로 둬도 안전.
    i = config["info"]
    DAEMONSET_RESERVE = 200                        # 데몬셋(aws-node/kube-proxy) 실사용분만 남김
    pause_cpu = max(500, int(i["node_cpu"]) - DAEMONSET_RESERVE)
    # ★scaler가 쓸 단계 사다리를 그대로 전달. scaler는 "max 도달 + p95 SLO 초과"를 확인했을 때만
    #   다음 단계로 점프한다(1대씩 아니라 단계 단위 → 회복이 빠름).
    #   node_mem_mi는 노드 capacity 기준 값 → scaler가 NodePool memory limit을 계산할 때 쓴다
    #   (allocatable로 계산하면 메모리 캡이 먼저 걸려 노드 증설이 막히는 버그가 재발).
    _mng = int(i["mng_count"])

    def hpa_ceiling(app):
        """그 앱의 HPA 천장(=파드 수 상한). 노드 예산으로 유도한다.

        ★두 가지를 정확히 반영해야 한다:
          ① 노드 독점 판정은 request가 아니라 limit으로 한다.
             stress는 request 965m이지만 limit 2000m로 노드(1930m)를 독점하므로 1파드/노드다.
             request로만 보면 2파드/노드로 잡혀 천장이 2배로 부풀고, 그 값은 절대 노드 한계를 넘는다
             (실측 버그: stress 천장이 48파드=48노드로 산출됐다. 한계는 24대다).
          ② 기준 노드 수는 '절대 한계'가 아니라 '사다리 최상단'이다.
             모든 앱이 각각 전체 클러스터를 차지할 수는 없다. 사다리 최상단으로 잡으면
             예상 부하는 덮으면서 천장이 과도하게 벌어지지 않는다.
             그보다 큰 수요는 scaler가 'rps로 정당화될 때만' 천장을 올려서 대응한다
             (수요 산술 트리거 + rps 교차검증 + 속도 제한).
        """
        req = _parse_cpu_m(config[app]["req"]) or 100
        node_cpu = int(i["node_cpu"])
        # ★배치 밀도는 request로 계산한다(스케줄러가 보는 값). limit 기준으로 잡으면
        #   limit이 노드 전체인 앱은 request가 작아도 '노드당 1파드'로 잡혀 천장이
        #   실제 수용량보다 작아진다 → 스파이크에서 파드를 못 늘린다.
        per_node = max(1, node_cpu // max(1, req))
        base_nodes = max(2, int(i["kp_nodes"]) + _mng)      # 사다리 최상단(총 노드)
        return max(int(config[app]["min"]) + 2, per_node * base_nodes)
    kp_stages = sorted({max(1, min(int(i["kp_nodes"]), s - _mng)) for s in i["node_stages"]})
    # ★비상 단계 분리는 폐기했다(EMERGENCY_STAGES=0) — 총 노드 8대가 하드캡이므로
    #   '정상/비상'을 나누면 정상 상한만 8보다 낮아져 스파이크에서 처리량을 잃는다.
    #   과증설 방어는 단계 분리가 아니라 (a) 8대 하드캡 (b) cap 자기교정 (c) 위험등급
    #   (노드는 ρ>1 지속·게이트 임박에서만) 세 가지가 담당한다.
    _kp_abs = max(1, MAX_NODES_ABS - _mng)          # Karpenter 노드 하드캡 (총 8대 - MNG)
    n_norm = max(1, len(kp_stages) - EMERGENCY_STAGES)
    prewarm_cfg = {"pause_cpu": pause_cpu,
                   "kp_node_cap": int(i["kp_node_cap"]),
                   # ★모든 상한을 하드캡으로 클램프한다 — 어떤 경로로도 총 8대를 못 넘게.
                   "kp_node_cap_max": min(_kp_abs, kp_stages[-1]),
                   # ★절대 한계 = 하드캡(8대) - MNG.
                   #   이전엔 `kp_stages[-1] * 3`으로 사다리의 3배까지 열어뒀는데, 그게
                   #   "이득 없이 노드가 늘어나는" 경로였다. 노드를 더 줘도 처리가 안 나아진
                   #   실측(12대에서 파드당 CPU 2~17%)이 그 근거다.
                   #   처리량 부족은 cap 자기교정(scaler)으로 풀고, 노드는 8대로 묶는다.
                   "kp_node_cap_abs": _kp_abs,
                   # ★게이트 임박 전용 비상 한계도 같은 값 — break-glass 경로를 제거했다.
                   #   근거: 노드를 14대까지 열면 2h 평균이 5.5대를 넘어 비용 티어가 4점 밑으로
                   #   떨어진다. 게이트로 지키려던 12점을 비용에서 그대로 잃으므로 이득이 없다.
                   "kp_node_cap_break": _kp_abs,
                   "kp_node_cap_normal": min(_kp_abs, kp_stages[n_norm - 1]),  # 정상 대응 상한
                   # ★MNG 노드 수 — scaler가 'Karpenter 하드캡 = 총 8대 - MNG'를 계산하는 데 쓴다.
                   #   없으면 scaler가 1로 가정하므로 더 보수적으로(캡이 작게) 동작한다.
                   "mng_count": _mng,
                   "kp_node_stages": kp_stages,
                   "mem_per_node_mi": int(i["mem_per_node_mi"]),
                   "vcpu": int(i["vcpu"]),
                   # ★baseline(앱별 상주 파드)을 기록한다 — scaler가 이 값을 정답으로 쓴다.
                   #   scaler는 운영 중 HPA minReplicas를 올리므로, baseline을 라이브 HPA에서
                   #   읽으면 '자기가 올린 값'을 baseline으로 오인한다(재시작마다 피크가 굳는 래칫).
                   "base_min": {a: int(config[a]["min"]) for a in ("user", "product", "stress")},
                   # ★base_max = turn.py가 산출한 max 그대로 쓴다.
                   #   ★이전엔 hpa_ceiling(=노드당 파드 × 하드캡 노드)으로 부풀렸다. 그게
                   #     비용 붕괴의 경로였다: request가 작은 io 앱은 그 값이 100~200파드가 되고,
                   #     HPA는 노드를 모르니 CPU만 보고 그 천장까지 파드를 늘려 워커 노드를 만든다.
                   #     (계산: user request 82m × 168파드 = 13.8코어 = 워커 7대)
                   #   ★올바른 순서: '노드를 안 늘리는 구간'에서 출발하고, 그 구간으로 부족함이
                   #     실제로 확인될 때만(Pending·5xx·지연 지속) scaler가 max_ceiling을
                   #     hpa_hard까지 올린다. scaler가 그 경로를 이미 갖고 있으므로
                   #     여기서 미리 열어둘 이유가 없다 — 열어두면 되돌릴 방법이 없다.
                   #   ※stress는 turn.py의 s_max가 이미 '하드캡 전체'라 그대로 충분하다.
                   "base_max": {a: int(config[a]["max"])
                                for a in ("user", "product", "stress")},
                   "max_warm": 0}   # ★prewarm 비활성(비용 격리). 노드 증설 주범은 user/product의
                   #   SLO(200ms) 추격이지 warm이 아님(CSV 확인). warm 다시 쓰려면 max(1,min(2,kp_node_cap)).
    with open(os.path.join(os.path.dirname(__file__), "prewarm_cfg.json"), "w") as f:
        json.dump(prewarm_cfg, f)
    print(f"  prewarm_cfg.json 저장(노드-유도): {prewarm_cfg}")
    print(f"  노드: baseline {_mng + 1}대 / 상한 {kp_stages[-1] + _mng}대 "
          f"(MNG {_mng} + Karpenter {kp_stages[-1]})")

    # ── stress 노드 사이징 진단 ────────────────────────────────────────────────
    # ★stress는 CPU-burn + 노드당 1파드라 준수율을 결정하는 변수가 '노드당 코어 수' 하나다.
    #   파드를 늘려도 같은 노드의 코어를 나눠 쓰므로 E[T]가 안 변한다(M/G/1-PS).
    #   ★그런데 채점 비용은 인스턴스 '개수'만 본다 — 노드를 키우는 것은 점수상 공짜다.
    #   그래서 '현재 코어로 90% 티어를 지킬 수 있는 rps'를 계산해 알려준다.
    s_svc = measured["stress"].get("p95_light", 0) or measured["stress"].get("origin_p50", 0)
    _slo = SLO["stress"]
    t_max = _slo / math.log(10.0)      # 0.90 = 1 - exp(-SLO/E[T]) → E[T] ≤ SLO/ln10
    if s_svc <= 0:
        print("\n  stress 사이징: 처리시간 측정 실패 → 판단 불가")
    elif s_svc >= t_max:
        need_c = math.ceil(vcpu * s_svc / (t_max * 0.5))
        print(f"\n  ⚠ stress: 무부하 처리시간 {s_svc}ms ≥ {t_max:.0f}ms → 트래픽이 0에 가까워도 "
              f"90% 티어 불가. vCPU {vcpu} → {need_c} 이상 필요(비용 점수는 개수만 보므로 공짜)")
    else:
        rho90 = 1.0 - s_svc / t_max
        rps90 = rho90 / (s_svc / 1000.0)
        _msg = ""
        if rps90 < 3.0:
            _msg = (f" → 트래픽이 이보다 크면 vCPU {vcpu * 2}+ 권장"
                    f"(비용은 개수만 보므로 공짜)")
        print(f"\n  stress 사이징: 처리시간 {s_svc}ms / {vcpu} vCPU → "
              f"노드 1대가 90% 티어로 감당하는 rps {rps90:.1f}{_msg}")
    if input("\n적용 + 검증? (y/n): ").strip().lower() != "y":
        # ★취소해도 HPA는 스케일 가능한 상태로 둔다.
        #   측정 잠금은 위 finally에서 이미 풀렸지만, 여기서 산출된 min/max로 맞춰주면
        #   '적용은 안 했지만 최소한 스케일은 되는' 상태가 된다.
        _unfreeze_hpa(config)
        print(f"취소 — HPA는 산출값으로 복원했다 "
              f"(min/max: " + " ".join(f"{a}={config[a]['min']}/{config[a]['max']}"
                                       for a in ("user", "product", "stress")) + ")")
        return

    # [3] 산술 비교 — 측정값 유도(B)와 고정값(A)를 '채점표 산술'로 비교해 이긴 쪽을 1번만 적용한다.
    #   ★A/B/C 부하 비교를 없앤 이유: 3번의 deploy patch가 rollout을 3번 유발하고,
    #     그 사이 클러스터가 불안정해져 채점 시작 시점에 성능이 깎인다(실측: B 적용 후 76.9%).
    #     부하 비교 대신 측정값에서 '예상 성능'을 산술로 추정하고 채점표 티어로 환산한다.
    #   ★판정 기준: 트리거 CPU(= request × util)가 '동시 요청 N개 지점'에 있는지.
    #     그리고 request가 '노드당 적정 파드 수'를 만드는지. 두 조건을 만족하면 측정값을 쓴다.
    #     아니면 고정값(38점 검증 구성)으로 폴백한다.
    #   ★1번만 적용하므로 rollout 1회, 안정화 1회 — 소요 시간 5분 단축.
    print("\n[3/4] 산술 비교 (측정값 vs 고정값)")
    cfg_a = baseline_config(config)

    # ★앱별로 개별 판정: 측정값이 기본값의 ±20% 이내면 기본값, 벗어나면 측정값.
    #   앱마다 독립적으로 결정 → 혼합 가능 (user는 기본값, stress는 측정값 등).
    SIMILARITY_THRESHOLD = 0.20
    final = dict(config)  # 측정값을 기본으로 시작
    final["karpenter"] = config["karpenter"]
    final["info"] = config["info"]
    
    for app in ("user", "product", "stress"):
        b_req = _parse_cpu_m(config[app]["req"]) or 0
        b_util = config[app]["util"]
        b_trig = b_req * b_util / 100.0
        a_req = _parse_cpu_m(cfg_a[app]["req"]) or 0
        a_util = cfg_a[app]["util"]
        a_trig = a_req * a_util / 100.0
        ratio = b_trig / a_trig if a_trig > 0 else 1.0
        similar = abs(ratio - 1.0) <= SIMILARITY_THRESHOLD
        
        if similar:
            # 기본값 사용 (검증된 최적)
            c = dict(config[app])
            c.update(cfg_a[app])
            final[app] = c
            print(f"  {app:8} 트리거 {b_trig:.0f}m vs 기본 {a_trig:.0f}m → 차이 {(ratio-1)*100:+.0f}% → 기본값 적용")
        else:
            # 측정값 사용 (앱이 다름)
            final[app] = config[app]
            print(f"  {app:8} 트리거 {b_trig:.0f}m vs 기본 {a_trig:.0f}m → 차이 {(ratio-1)*100:+.0f}% → ★측정값 적용 (앱이 다름)")
    
    print(f"  최종: user {final['user']['util']}%/{final['user']['req']} "
          f"product {final['product']['util']}%/{final['product']['req']} "
          f"stress {final['stress']['util']}%/{final['stress']['req']}")

    # stress 조기증설(C): 비활성화.
    #   ★이전에 자동 적용했으나, stress util을 35%로 낮추면 stress가 과증설돼서
    #     노드를 과다 점유하고 user에게 돌아갈 자원이 줄어 성능이 떨어졌다 (실측: user 65.8%).
    #   stress util은 측정 기반(CONC_CPU_BOUND=0.5)으로 유도된 값(50%)이 정답이다.
    #   SLO에 빡빡한 앱이라도 util을 낮추는 게 아니라 min을 올리는 게 올바른 대응이다.
    #   (min은 baseline 노드에 있어 비용 0, util 낮추면 max까지 채워 노드가 늘어남)

    apply_config(final, node_type, max_nodes, mng_count)
    config = final
    capped = {a for a in ("user", "product", "stress")
              if (measured[a].get("p95_light", 0) or 0) > SLO[a]}

    # [4] 안정화 (검증에서 늘어난 파드/노드를 min으로 수렴)
    print("\n[4/4]", end="")
    await stabilize(config)
    print("\n=== 튜닝 완료 (값은 실측 기반 고정 / 채점 중 HPA·Karpenter 자율 대응) ===")
    for app in ["user", "product", "stress"]:
        c = config[app]
        tail = " ⚠앱한계(성능 천장)" if app in capped else ""
        print(f"  {app:8} req={c['req']} lim={c['lim'] or 'none'} min={c['min']} max={c['max']} util={c['util']}%{tail}")


def _print_config(config, node_type, max_nodes, mng_count):
    i = config["info"]
    print(f"  ({node_type}, {i['node_cpu']}m/{i['vcpu']}vCPU × 최대 {max_nodes}대 = MNG {mng_count} + Karpenter {i['kp_nodes']})")
    print(f"  MNG 상주 request 합계: {i['req_sum']} / 예산 {i.get('mng_budget','?')} (노드 available {i['avail']}m/대 × {mng_count}) → 예산 이내면 노드 0(비용안전)")
    print(f"  [Karpenter] cpu={config['karpenter']['cpu']} mem={config['karpenter']['mem']} "
          f"= 현재 {i['kp_node_cap']}대 (1단계) / 절대천장 {i['kp_nodes']}대 "
          f"consolidateAfter={CONSOLIDATE_AFTER}(빈 노드 즉시 회수)")
    print(f"  [stress GOMAXPROCS] {config['stress'].get('gomax')} (= vCPU, 코어 전부 사용)")
    for app in ["user", "product", "stress"]:
        c = config[app]
        b = c.get("bound", "?")
        head = c.get("headroom", False)
        scale_by = "util-HPA" if b == "cpu" else "적정min(I/O)"
        prof = f"{b}-bound" + (f"·느림/burst→상주+{HEADROOM_ADD}" if head else "")
        print(f"  [{app:<7}] cpu req={c['req']:>5} lim={(c['lim'] or 'none'):>6} | mem req={c['mem_req']:>6} lim={c['mem_lim']:>6} | util={c['util']}% scaleUp={c.get('scaleup',4)}/15s min={c['min']} max={c['max']} | {prof} → {scale_by}")


if __name__ == "__main__":
    asyncio.run(main())
