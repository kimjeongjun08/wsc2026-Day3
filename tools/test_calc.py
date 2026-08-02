"""
autotune 실행 전 가드: python -X utf8 test_calc.py
calculate()의 핵심 불변식 검사. 하나라도 깨지면 autotune을 돌리지 말 것.

모델 (2026-07-13 최종): 최적값을 '측정으로 도출'한다. 값을 베끼지 않는다.
  stress request = 약한 부하 중 실측 CPU(clamp 150~600). 그 파드가 실제 쓰는 만큼.
  limit 1000m = burst 천장 (비용/스케줄링에 안 잡힘, 단건 속도만 담당).
  util 50 / min 2 / max = 노드 수 기반. CPU 시간은 request 비례 배분이라
  request가 크면 user/product가 굶는다(2026-07-12 실증: user util 990%) → 작게.
  request 도출은 main()에서 stress_req = max(150, min(600, 실측)).
"""
import sys
sys.path.insert(0, '.')
from autotune import calculate

FAIL = []

def check(name, cond, detail=""):
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)

measured = {'user': 87, 'product': 12, 'stress': 39}

print("=== 실측 기반 request 도출 (stress 실측→clamp) ===")
# main()의 clamp를 재현: stress_req = max(150, min(600, measured))
for m_stress, exp in ((39, 150), (280, 280), (900, 600)):
    sreq = max(150, min(600, m_stress))
    r = calculate('t3.medium', 6, {**measured, 'stress': m_stress}, mng_count=2, stress_req=sreq)
    s = r['stress']
    print(f"  실측 stress={m_stress}m → request={s['req']} (clamp {exp}m)")
    check(f"실측 {m_stress} → request {exp}m (clamp)", s['req'] == f"{exp}m")

print("\n=== 파생값 (실측과 무관하게 고정 정책) — stress_req=280 예시 ===")
r = calculate('t3.medium', 6, measured, mng_count=2, stress_req=280)
s = r['stress']
print(f"  stress: req={s['req']} lim={s['lim']} min={s['min']} max={s['max']} util={s['util']}%")
print(f"  user: req={r['user']['req']} lim={r['user']['lim']} | product: req={r['product']['req']} | karpenter cpu={r['karpenter']['cpu']}")
check("stress util 50%", s['util'] == 50)
check("stress min 2 (HA)", s['min'] == 2)
check("stress max 10 (노드 폭증 상한)", s['max'] == 10)
check("stress limit = 1000m burst (비용 아님)", s['lim'] == "1000m")
check("karpenter cpu = (6-2)*2 = 8 하드캡", int(r['karpenter']['cpu']) == 8)
check("user request 작게 — 실측 87m도 60m 캡", r['user']['req'] == "60m")
check("product request 작게 — 30~60m", r['product']['req'] == "30m")

print("\n=== 재튜닝 시 request 축소 (util 아님) — 실측→200→150 ===")
for req_v in (280, 200, 150):
    rv = calculate('t3.medium', 6, measured, mng_count=2, stress_req=req_v)
    sv = rv['stress']
    print(f"  req={req_v}m: lim={sv['lim']} min={sv['min']} max={sv['max']} util={sv['util']}%")
    check(f"req={req_v}: request 반영", sv['req'] == f"{req_v}m")
    check(f"req={req_v}: limit·util 불변 (1000m/50%)", sv['lim'] == "1000m" and sv['util'] == 50)

print("\n=== 비용 상한 (max_nodes로 노드 폭증 방지) ===")
r3 = calculate('t3.medium', 3, measured, mng_count=2, stress_req=300)
check("max_nodes=3 → karpenter cpu=(3-2)*2=2 (노드 1대만 추가 허용)", int(r3['karpenter']['cpu']) == 2)
check("max_nodes=3 → stress max=min(6,10)=6", r3['stress']['max'] == 6)

print()
if FAIL:
    print(f"✗✗ 불변식 {len(FAIL)}개 깨짐: {FAIL}")
    print("   calculate()가 변조됨 — autotune 실행 금지. git diff로 확인할 것.")
    sys.exit(1)
print("✓ 전체 통과 — autotune 실행 안전.")
