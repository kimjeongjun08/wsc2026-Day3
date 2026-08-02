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

SLO = {"user": 200, "product": 200, "stress": 1000}   # 성능 SLO(ms)
AVAIL_SLO = 5000   # 가용성 SLO(ms) — 이 시간 넘으면 '가용성 실패'(채점기 기준). stress가 여기 걸릴 수 있음.

# 앱 판별: 요청당 CPU 부담(cpu_m / rps). 이 이상이면 CPU-bound(요청이 CPU를 태움 → util-HPA 작동).
#   미만이면 I/O-bound(DB 대기만 → CPU 안 오름 → util-HPA 무의미 → 적정 min으로 대응).
CPU_BOUND_MPS = 25

# cpu-bound request = 실사용 × 이 계수. 오버서브(작은 request→노드 몰림→스로틀)를 없애는 핵심 다이얼.
#   높을수록(1.0=실사용) 스로틀 0·성능↑·노드↑(비용). 낮을수록 싸지만 스로틀 위험. 0.7~0.85 권장.
CPU_REQ_FACTOR = 0.75

# io 앱(user/product) 상시 baseline. ★이 앱들은 DB/캐시라 CPU를 거의 안 씀 → CPU-HPA가 부하에
#   둔감(요청 몰려 지연 터져도 CPU 안 올라 스케일 지연 → "성능 떨어지면 회복 안 됨"의 정체).
#   대응: 상주(min)를 넉넉히 = CPU-HPA에 의존 않고 스파이크를 상주 파드로 흡수. 파드가 작아(30~200m)
#   MNG 노드에 패킹돼 비용 거의 안 늚(실측 ratio 1.11도 비용만점 = 압도적 여유). 성능이 유일 제약이라
#   상주를 넉넉히 = 스파이크를 상주로 흡수. 6=2h 테스트 user 85% → 상주+topologySpread로 스파이크 용량↑.
IO_HEADROOM = 2

# Karpenter consolidateAfter: 노드가 '놀기 시작한 뒤' 얼마 만에 회수하는가.
#   짧으면 부하 끝나고 빨리 회수 → 노드시간↓(비용 이득). 길면 warm 유지 → 스파이크 매끄럽지만 비용↑.
#   30초 = 부하 끝나면 빠른 회수(비용). turn.py는 이 값으로 완결 — 외부 툴 의존 없음.
CONSOLIDATE_AFTER = "30s"  # 노드 저활용 30초 뒤 회수 → 부하 빠지면 비용 빠르게 회복(요요는 phase가 분단위라 안전)


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
    allocatable CPU(밀리코어)·메모리(Mi), 물리 vCPU 수, 인스턴스 타입 라벨을 반환.
    실패 시 t3.medium 기본값 폴백."""
    ok, alloc = kubectl("get nodes -o jsonpath=\"{.items[0].status.allocatable.cpu}\"")
    _, cap = kubectl("get nodes -o jsonpath=\"{.items[0].status.capacity.cpu}\"")
    ok_m, amem = kubectl("get nodes -o jsonpath=\"{.items[0].status.allocatable.memory}\"")
    _, itype = kubectl("get nodes -o jsonpath=\"{.items[0].metadata.labels.node\\.kubernetes\\.io/instance-type}\"")
    alloc_m = _parse_cpu_m(alloc) if ok else None
    try:
        vcpu = int(float(cap.strip().strip('"'))) if cap else None
    except ValueError:
        vcpu = None
    node_mem_mi = _parse_mem_mi(amem) if ok_m else None
    return alloc_m or 1800, vcpu or 2, node_mem_mi or 3500, (itype.strip().strip('"') or "unknown")


def rid():
    return str(random.randint(100000000000, 999999999999))


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
            async with session.post(f"{base}/v1/stress", json={"requestid": rid(), "uuid": uid(), "length": random.randint(50, 200)}) as r:
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


async def _light_latency(base, app, seed_u, seed_p, n=8):
    """단일 요청 n개 순차 실행 → 큐잉 없는 '고유 지연' p95(ms).
    이게 SLO보다 크면 그 앱은 스케일해도 SLO 못 지킴(앱 자체 한계). 앱 무관 판별."""
    lats = []
    conn = aiohttp.TCPConnector(limit=1)
    async with aiohttp.ClientSession(connector=conn) as session:
        for _ in range(n):
            r = {app: []}
            await _hit(session, base, app, seed_u, seed_p, r)
            if r[app] and 200 <= r[app][0][0] < 300:
                lats.append(r[app][0][1])
    if not lats:
        return 0
    lats.sort()
    return round(lats[int(len(lats) * 0.95)])


async def measure(base, seed_u, seed_p):
    """앱 무관 측정 — 채점기가 보는 '레이턴시·처리율'을 주 신호로, CPU는 보조(여러 번 샘플 최대값).
      1) 고유 지연(light): 단일 요청 → 앱이 SLO 자체를 지킬 수 있는지 (큐잉 없는 순수 처리시간).
      2) 부하(load): 포화 → 파드당 처리율 rps·부하 p95, CPU는 여러 번 top 후 최대(지연 보정).
    측정 전 모든 앱은 단일 파드로 고정돼 있어야 함 (main에서 prep)."""
    loop = asyncio.get_event_loop()
    measured = {}
    for app in ["user", "product", "stress"]:
        # ── 1) 고유 지연 (단일 요청, 큐잉 0) ──
        p95_light = await _light_latency(base, app, seed_u, seed_p)

        # ── 2) 부하 중 rps·p95 + CPU 최대 샘플 ──
        stop = asyncio.Event()
        res = {app: []}
        cpu_max = [0]
        mem_max = [0]
        t_start = time.time()

        async def bg():
            end2 = time.time() + 70
            conn = aiohttp.TCPConnector(limit=20)
            async with aiohttp.ClientSession(connector=conn) as session:
                async def w():
                    while not stop.is_set() and time.time() < end2:
                        await _hit(session, base, app, seed_u, seed_p, res)
                await asyncio.gather(*[w() for _ in range(6)], return_exceptions=True)

        async def sampler():
            # 부하 도는 동안 top을 여러 번 → 최대값 (metrics-server 15s 평균/지연 보정)
            while not stop.is_set():
                await asyncio.sleep(8)
                c, m = await loop.run_in_executor(None, top_cpu_mem, app)
                if c:
                    cpu_max[0] = max(cpu_max[0], c)
                if m:
                    mem_max[0] = max(mem_max[0], m)

        task = asyncio.create_task(bg())
        samp = asyncio.create_task(sampler())
        await asyncio.sleep(45)  # 더 긴 창 → top 5~6회 샘플
        stop.set()
        await task
        samp.cancel()

        elapsed = max(1.0, time.time() - t_start)
        samples = res[app]
        oks = sorted(t for s, t in samples if 200 <= s < 300)
        rps = round(len(samples) / elapsed, 1)
        p95_load = round(oks[int(len(oks) * 0.95)]) if oks else 0
        cpu = cpu_max[0] or None
        mem = mem_max[0] or None

        if cpu is None:
            cpu, mem = {"user": 30, "product": 30, "stress": 500}[app], {"user": 48, "product": 48, "stress": 128}[app]
            print(f"  {app}: CPU 측정 실패 → 기본값 cpu={cpu}m")
        slo = SLO[app]
        ceil = " ⚠고유지연>SLO(스케일로 못 고침)" if p95_light > slo else ""
        print(f"  {app}: cpu={cpu}m mem={mem}Mi rps={rps} | 고유p95={p95_light}ms 부하p95={p95_load}ms{ceil}")
        measured[app] = {"cpu": cpu, "mem": mem, "rps": rps, "p95": p95_load, "p95_light": p95_light}
    return measured


# ── 계산 ──

def calculate(measured, node_cpu_m, vcpu, max_nodes, mng_count=2, stress_req_override=None, node_mem_mi=None):
    """
    실측 + 노드 스펙 기반. 인스턴스 타입/앱에 하드코딩된 값 없음:
      - 노드 CPU(node_cpu_m)·vCPU(vcpu)·메모리(node_mem_mi)는 클러스터에서 읽어온 실제값.
      - request/memory = 부하 중 실측 (앱이 바뀌어도 자동 반영).
      - stress limit = 노드 전체 CPU, GOMAXPROCS = vCPU (CPU-burn 앱은 코어 다 줄수록 빠름).
      - user/product limit = 노드 절반 (I/O 앱은 이걸로 충분, 스파이크 여유).
      - Karpenter 메모리 캡 = 실제 노드 메모리/vCPU 비율 (m5/r5 등 메모리 많은 타입에서 캡이
        CPU캡보다 빡빡해 노드 증설을 잘못 막는 것 방지).
    """
    avail = node_cpu_m - SYSTEM_PER_NODE

    u_cpu, p_cpu, s_cpu = measured["user"]["cpu"], measured["product"]["cpu"], measured["stress"]["cpu"]
    u_mem, p_mem, s_mem = measured["user"]["mem"], measured["product"]["mem"], measured["stress"]["mem"]

    mng_budget = mng_count * avail
    up_cap = max(150, avail // 4)
    u_lim = max(500, node_cpu_m // 2)
    p_lim = max(500, node_cpu_m // 2)
    s_lim = vcpu * 1000                 # stress: 노드 코어 전부(2코어 버스트) → 단건 최속
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
    #   io-bound(product): 모든 경로(GET캐시·POST/PUT쓰기·S3업로드)가 I/O 대기라 CPU 낮음(측정 8m).
    #     ★바닥 200m은 측정의 25배 과대예약이라 MNG 낭비 → user floor(30)와 정합하게 60m로 정확화.
    #     미스/쓰기 버스트는 limit(965m)이 흡수(I/O라 실제론 CPU 거의 안 씀). 남긴 MNG공간 = user concurrency.
    #   stress: 요청 1개가 2코어를 씀(속도는 limit 담당) → request는 작은 예약만.
    u_req = max(300, min(u_lim, avail // 2, int(u_cpu * CPU_REQ_FACTOR))) if u_bound == "cpu" else max(30, min(up_cap, u_cpu))
    p_req = max(300, min(p_lim, avail // 2, int(p_cpu * CPU_REQ_FACTOR))) if p_bound == "cpu" else max(60, min(up_cap, p_cpu))
    # stress는 pod anti-affinity로 노드를 독차지(user/product와 동거 금지) → 2코어 온전히 확보.
    #   request=node//2(~900m): util 45면 실사용 ~434m(0.45코어)에 스케일 = 공격적(스파이크 큐잉·503 방어).
    #   ★avail로 크게 잡고 util 60으로 보수화했더니 2h 테스트서 stress 78% 폭락(과소provision) → 되돌림.
    #    비용이 관대(ratio 1.11도 만점)라 "과증설"은 실익 없는 걱정이었고, 공격적 스케일이 정답.
    s_req = stress_req_override or (node_cpu_m // 2)

    # Memory: 실측 기반 (req=실측×1.3, limit=실측×3, floor)
    u_mem_req = max(48, int(u_mem * 1.3)); u_mem_lim = max(256, int(u_mem * 3))
    p_mem_req = max(48, int(p_mem * 1.3)); p_mem_lim = max(256, int(p_mem * 3))
    s_mem_req = max(64, int(s_mem * 1.3)); s_mem_lim = max(256, int(s_mem * 3))

    # ── 상주(min): ★baseline 2노드 유지 + MNG 남은 자리를 user 상주로 채워 concurrency 확보 ──
    #   [노드A(MNG): user+product 상주 패킹] + [노드B: stress 독차지] = 여전히 2대(다 MNG에 들어감=비용 0).
    #   ★user는 DB앱이라 스파이크 burst 흡수엔 concurrency(파드 수)가 중요 → MNG 빈 자리만큼 상주로 채움
    #     (앱 가벼우면 더 많이). 노드 추가 0이라 비용 안전. 상한 4(파드밀도·여유). 무거운 앱이면 fit2=false → 1.
    #   스파이크가 상주 넘으면 HPA 스케일 → 빠지면 scaleDown이 2노드로 수렴.
    fit2 = (2 * u_req + 2 * p_req) <= avail
    p_min = 2 if fit2 else 1
    u_min = max(2, min(4, (avail - p_min * p_req) // max(1, u_req))) if fit2 else 1
    s_min = 1

    # ── HPA util 임계 = 측정된 bound에서 유도 (하드코딩 X, request처럼 앱에 맞춰 적응) ──
    #   io(DB/캐시)=CPU-blind: 부하 몰려 지연 터져도 CPU가 잘 안 올라 신호가 약함 → 45로 일찍 스케일
    #     (늦으면 회복불가). request도 실측cpu라 절대 트리거가 낮아 민감.
    #   cpu-bound: CPU가 부하를 정직히 반영 → 포화 근처에서 스케일. cpu/rps(부하당 CPU)가 클수록 강한
    #     cpu-bound → 70(노드 CPU 70% 지속=진짜 포화에만; request=avail이라 util%가 노드 CPU%를 반영),
    #     약하면 60. ★stress(CPU-burn)는 여기 걸려 자동 70 — CPU 태우는 게 본업이라 낮은 임계면 일만 해도
    #     스케일=과증설(node=pod라 치명적). 앱이 바뀌어 user가 cpu-bound여도 자동으로 60~70 잡힘.
    # HPA util = bound에서 유도(원칙적 기본값 — 특정 부하에 크랭크 X, 어떤 부하든 합리적이 목표):
    #   io(user/product): 표준 타겟 45 (일찍 스케일하되 과하지 않게).
    #   cpu(stress): 50 (포화 근처, node provision 지연 감안 여유).
    #   ※I/O앱은 CPU-HPA 근본 한계(부하 몰려도 CPU 거의 안 올라 스케일 둔감) → 어떤 util도 완벽 스케일 불가.
    #     "어떤 부하든 만점"의 진짜 답은 RPS 기반 스케일(부하 실측으로 스케일). 이건 인프라 추가 필요.
    def hpa_util(bound):
        return 45 if bound == "io" else 50
    u_util, u_scaleup = hpa_util(u_bound), 6
    p_util, p_scaleup = hpa_util(p_bound), 4
    # stress util 50 (hpa_util cpu). ★util 낮추면(35) 평상부하(15rps=45%CPU)에도 3파드=3노드로 과증설(ratio 2.5).
    #   stress는 CPU-heavy라 낮은 util = 상시 여러 노드 = 비용폭발. 그래서 50 유지(평상시 1파드=저비용).
    #   스파이크 5xx는 "노드 provision 20~60s 지연"이 원인 = 물리. 이건 util로 못 고침 → scaler(RPS 선행)나
    #   warm 노드로만 해결. stress ≥90 자체는 큰 length 앱천장이라 물리적으로 어려움(스케일 무관).
    s_util, s_scaleup = hpa_util("cpu"), 3

    # ── max = '최대 노드에 그 앱 파드가 몇 개 들어가나'로 유도 (하드코딩 X) ──
    #   max에 닿는다 = 노드 예산을 다 썼다 → 더 원하면 '카펜터 추가 노드 상한'을 올리면 됨(비용 다이얼).
    #   cpu-bound(user)/io(product)는 request 기준, stress는 limit(2코어)이 실제 용량 제약이라 limit 기준.
    node_budget = max_nodes * avail                          # user/product용 노드 예산(전체)
    u_max = max(u_min + 2, node_budget // u_req)
    p_max = max(p_min + 2, min(node_budget // p_req, 16))     # product는 작아 많이 들어가나 캐시라 상한만
    # stress max = 버스트 상한. ★stress는 node=pod(anti-affinity)라 스케일=노드=비용 → avg(비용≥11) 보호를
    #   위해 3으로 캡. 80%+ 성능엔 2~3노드면 충분(스파이크 동시요청 분산). max_nodes 크게 잡아도 stress가
    #   노드예산 독식 안 함(user/product는 MNG 패킹이 우선). 더 필요하면 수동으로 이 캡·max_nodes 상향.
    s_max = max(2, min(max_nodes // 2, 3))
    u_min = min(u_min, u_max)                  # 상주가 max를 넘지 않게
    p_min = min(p_min, p_max)

    # Karpenter 하드캡: (max_nodes - mng_count)대 분량 → 노드 폭증·비용 차단
    kp_cpu = max(vcpu, (max_nodes - mng_count) * vcpu)
    # 메모리 캡은 실제 노드의 메모리/vCPU 비율로 (t3=2, m5=4, r5=8 GiB/vCPU) → 메모리캡이
    #   CPU캡보다 빡빡해 노드 증설을 잘못 막는 일 방지. 여유 위해 +1Gi.
    mem_per_vcpu_gi = (node_mem_mi / 1024.0 / vcpu) if node_mem_mi else 2.0
    kp_mem_gi = max(2, int(kp_cpu * mem_per_vcpu_gi) + 1)

    return {
        "karpenter": {"cpu": str(kp_cpu), "mem": f"{kp_mem_gi}Gi"},
        "user":    {"req": f"{u_req}m", "lim": f"{u_lim}m", "mem_req": f"{u_mem_req}Mi", "mem_lim": f"{u_mem_lim}Mi", "util": u_util, "scaleup": u_scaleup, "min": u_min, "max": u_max, "bound": u_bound},
        "product": {"req": f"{p_req}m", "lim": f"{p_lim}m", "mem_req": f"{p_mem_req}Mi", "mem_lim": f"{p_mem_lim}Mi", "util": p_util, "scaleup": p_scaleup, "min": p_min, "max": p_max, "bound": p_bound},
        "stress":  {"req": f"{s_req}m", "lim": f"{s_lim}m", "mem_req": f"{s_mem_req}Mi", "mem_lim": f"{s_mem_lim}Mi", "util": s_util, "scaleup": s_scaleup, "min": s_min, "max": s_max, "gomax": gomax, "bound": "cpu"},
        "info": {"avail": avail, "node_cpu": node_cpu_m, "vcpu": vcpu, "kp_nodes": max_nodes - mng_count,
                 "req_sum": f"{u_min*u_req + p_min*p_req}m(user+product 노드A) / stress {s_req}m(노드B 독차지)",
                 "mng_budget": f"{mng_budget}m", "measured": measured},
    }


def apply_config(config, node_type, max_nodes, mng_count=2):
    for app in ["user", "product", "stress"]:
        c = config[app]
        gomax = str(config["stress"].get("gomax", 2))
        patch = json.dumps({"spec": {"template": {"spec": {"containers": [{"name": app,
            "env": [{"name": "GOMAXPROCS", "value": gomax}] if app == "stress" else None,
            "resources": {
                "requests": {"cpu": c["req"], "memory": c["mem_req"]},
                "limits": {"cpu": c["lim"], "memory": c["mem_lim"]},
            }}]}}}})
        # stress 아닌 앱은 env=None → JSON "env": null 을 제거 (kubectl에 null 넘기지 않음)
        patch = patch.replace('"env": null, ', '')
        patch = patch.replace('"', '\\"')
        kubectl(f'-n {NAMESPACE} patch deploy/{app} --type=strategic -p "{patch}"')

        hpa = json.dumps({"spec": {"minReplicas": c["min"], "maxReplicas": c["max"],
            "behavior": {
                "scaleUp": {"stabilizationWindowSeconds": 0, "policies": [
                    {"type": "Pods", "value": c.get("scaleup", 4), "periodSeconds": 15},
                    {"type": "Percent", "value": 100, "periodSeconds": 15}], "selectPolicy": "Max"},
                "scaleDown": {"stabilizationWindowSeconds": 45, "policies": [{"type": "Percent", "value": 50, "periodSeconds": 30}], "selectPolicy": "Max"}},
            "metrics": [{"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": c["util"]}}}]
        }}).replace('"', '\\"')
        kubectl(f'-n {NAMESPACE} patch hpa/{app}-hpa --type=merge -p "{hpa}"')

    kp = config["karpenter"]
    kp_patch = json.dumps({"spec": {"limits": {"cpu": kp["cpu"], "memory": kp["mem"]},
        "disruption": {"consolidationPolicy": "WhenEmptyOrUnderutilized", "consolidateAfter": CONSOLIDATE_AFTER}}}).replace('"', '\\"')
    kubectl(f'patch nodepool apdev-pool --type=merge -p "{kp_patch}"')

    for app in ["user", "product", "stress"]:
        kubectl(f"-n {NAMESPACE} rollout status deploy/{app} --timeout=120s")


# ── 검증 ──

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
        # stress 노드 + warm pool(오버프로비저닝) 노드는 baseline → 회수/cordon 대상에서 제외.
        #   ★warm 노드를 cordon하면 unschedulable이라 버스트 시 preempt로 못 올라타 웜풀 무력화됨.
        _, sn = kubectl(f'-n {NAMESPACE} get pods -l app=stress -o jsonpath="{{.items[*].spec.nodeName}}"')
        _, wn = kubectl(f'-n {NAMESPACE} get pods -l app=overprovisioning -o jsonpath="{{.items[*].spec.nodeName}}"')
        keep_nodes = set(sn.strip().strip('"').split()) | set(wn.strip().strip('"').split())
        warm = 1 if wn.strip().strip('"') else 0
        _, kp = kubectl("get nodes -l karpenter.sh/nodepool --no-headers -o custom-columns=NAME:.metadata.name")
        knodes = [n.strip() for n in kp.split("\n") if n.strip()]
        extra_knodes = [n for n in knodes if n not in keep_nodes]   # stress·warm 뺀 = 스케일아웃 노드
        _, n = kubectl("get nodes --no-headers")
        total = len([l for l in n.split("\n") if l.strip()]) if n else 0
        if not extra_knodes and total <= baseline_nodes + warm:
            print(f"  ✅ 노드 {total}대로 수렴 (baseline {baseline_nodes} + warm {warm}) — 채점 준비 완료")
            return
        print(f"  … 현재 {total}대 (스케일아웃 카펜터 {len(extra_knodes)}대 → drain으로 파드 축출·회수)")
        # ★cordon만으론 잔여 파드가 안 옮겨져 노드가 안 죽음(Karpenter 소극적 — 실측 확인).
        #   drain으로 파드를 강제 축출 → min이라 MNG 노드에 자리 있어 재배치 → 빈 카펜터 노드 즉시 회수.
        #   PDB(maxUnavailable:1) 존중해 한 번에 1개씩만 내려 가용성 안전. (stress·warm 노드는 keep_nodes라 제외)
        for node in extra_knodes:
            kubectl(f"drain {node} --ignore-daemonsets --delete-emptydir-data --force --timeout=60s")
        await asyncio.sleep(15)
    _, n = kubectl("get nodes --no-headers")
    total = len([l for l in n.split("\n") if l.strip()]) if n else 0
    print(f"  ⚠ 아직 {total}대 (목표 {baseline_nodes}). 곧 Karpenter가 회수함 — "
          f"채점 시작 전 `kubectl get nodes`로 {baseline_nodes}대인지 반드시 확인할 것.")


async def main():
    if len(sys.argv) < 2:
        print("사용법: python turn.py <CF endpoint>"); sys.exit(1)
    base = sys.argv[1].rstrip("/")
    print("=== turn.py (최종 튜닝툴) ===\n")

    # 노드 스펙은 클러스터에서 자동으로 읽음 (인스턴스 타입 물어보지 않음)
    node_cpu_m, vcpu, node_mem_mi, node_type = get_node_specs()
    print(f"노드 (자동 감지): {node_type}  —  {node_cpu_m}m allocatable / {vcpu} vCPU / {node_mem_mi}Mi mem "
          f"({node_mem_mi/1024/vcpu:.1f}GiB/vCPU)\n")

    # ★최대 총 노드 수(천장) — 자동 설정(수동 입력 X, 튜닝툴이 정함). MNG1 + 카펜터로 총 6대까지만.
    #   Karpenter limits.cpu(=kp_cpu)가 하드캡 → HPA max 올려도 이 노드수 절대 못 넘음(초과 파드는 Pending).
    #   baseline 2(MNG=user+product + stress 카펜터1). 6 = 스파이크 시 stress 최대3 + user/product 오버플로 여유.
    #   ★캡은 천장이지 avg가 아님 — scaleDown 빠르면(60s/40%) 스파이크에 잠깐 8대 가도 끝나면 바로 반납→avg~3.
    #   2h 테스트서 spike(235rps)에 용량부족(547ms)·stress s_max3 캡 → 8로 올려 스파이크 여유 확보(비용은 avg라
    #   빠른 반납으로 낮게 유지). 비용 더 조이려면 낮추면 됨(단 스파이크 성능↓).
    max_nodes = 6

    # 사전 체크: 노드 포화면 측정 오염
    _, top = kubectl("top nodes --no-headers")
    if top:
        busy = [l.split()[0].split(".")[0] for l in top.splitlines()
                if len(l.split()) >= 3 and l.split()[2].rstrip("%").isdigit() and int(l.split()[2].rstrip("%")) >= 80]
        if busy:
            print(f"⚠ 노드 CPU 포화: {busy} — 이전 부하 잔재 정리 후 재실행 권장.")
            if input("  계속? (y/N): ").strip().lower() != "y":
                return

    seed_u, seed_p = await _seed(base)

    # 측정 대상: CloudFront(= 채점기가 실제로 때리는 경로). user/stress 지연이 CDN 포함 실제값으로 잡힘.
    #   product는 캐시라 이 경로에선 파드 부하가 안 잡히지만, 어차피 채점에서도 캐시라 파드 부하는 낮음.
    #   → product는 정밀측정 대신 io-bound 고정정책(request 바닥 100m + util 80)으로 안정화(calculate).
    print(f"측정 대상: CloudFront {base} (채점 실제 경로 — user/stress 지연이 CDN 포함 실제값)\n")

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
    measured = await measure(base, seed_u, seed_p)  # CF(채점 경로). HPA는 apply_config가 복원

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
    config = calculate(measured, node_cpu_m, vcpu, max_nodes, mng_count, node_mem_mi=node_mem_mi)
    _print_config(config, node_type, max_nodes, mng_count)

    if input("\n적용 + 검증? (y/n): ").strip().lower() != "y":
        print("취소"); return

    # [3] 적용 + 단일 검증 (★ 재검증/에스컬레이션 없음)
    #   튜닝값은 [1] 단일파드 깨끗한 실측에서 나온다. 검증은 '적용값이 도는지' 참고용일 뿐,
    #   이 결과로 값을 흔들지 않는다 → 이미 흔들린(과부하/churn) 상태에 부하 얹어 나쁜 값으로
    #   튜닝되는 것을 방지. 가벼운 단일 부하라 churn/수렴창도 최소.
    print("\n[3/4] 적용...")
    apply_config(config, node_type, max_nodes, mng_count)
    print("  30초 안정화...")
    await asyncio.sleep(30)
    print("  검증 (단일·가벼운 부하, 참고용 — 값 안 바꿈)...")
    results = {"user": [], "product": [], "stress": []}
    await _run_load(base, seed_u, seed_p, 40, results, u_workers=3, p_workers=3, s_workers=2)
    sc = score(results)
    capped = set()
    for app in ["user", "product", "stress"]:
        light = measured[app].get("p95_light", 0)
        avail_pct, perf_pct = sc[app]
        if light > SLO[app]:
            capped.add(app)
        if perf_pct < 80 or avail_pct < 99:
            if light > AVAIL_SLO:
                print(f"  {app}: 고유지연 {light}ms>5s → 가용성도 앱한계(단일요청도 초과, 못 고침)")
            elif light > SLO[app]:
                print(f"  {app}: 고유지연 {light}ms>{SLO[app]}ms → 성능은 앱한계(속도). 가용성(5s)은 파드로 지켜짐")
            else:
                print(f"  {app}: 부하 시 큐잉 조짐 — 채점 중 HPA(min={config[app]['min']}~max={config[app]['max']})가 대응")

    # [4] 안정화 (검증에서 늘어난 파드/노드를 min으로 수렴)
    print("\n[4/4]", end="")
    await stabilize(config)
    print("\n=== 튜닝 완료 (값은 실측 기반 고정 / 채점 중 HPA·Karpenter 자율 대응) ===")
    for app in ["user", "product", "stress"]:
        c = config[app]
        tail = " ⚠앱한계(성능 천장)" if app in capped else ""
        print(f"  {app:8} req={c['req']} lim={c['lim']} min={c['min']} max={c['max']} util={c['util']}%{tail}")


def _print_config(config, node_type, max_nodes, mng_count):
    i = config["info"]
    print(f"  ({node_type}, {i['node_cpu']}m/{i['vcpu']}vCPU × 최대 {max_nodes}대 = MNG {mng_count} + Karpenter {i['kp_nodes']})")
    print(f"  MNG 상주 request 합계: {i['req_sum']} / 예산 {i.get('mng_budget','?')} (노드 available {i['avail']}m/대 × {mng_count}) → 예산 이내면 노드 0(비용안전)")
    print(f"  [Karpenter] cpu={config['karpenter']['cpu']} mem={config['karpenter']['mem']} (하드캡 {i['kp_nodes']}대) consolidateAfter={CONSOLIDATE_AFTER}(churn 완화→스파이크 성능↑)")
    print(f"  [stress GOMAXPROCS] {config['stress'].get('gomax')} (= vCPU, 코어 전부 사용)")
    for app in ["user", "product", "stress"]:
        c = config[app]
        b = c.get("bound", "?")
        scale_by = "util-HPA" if b == "cpu" else "적정min(I/O)"
        print(f"  [{app:<7}] cpu req={c['req']:>5} lim={c['lim']:>6} | mem req={c['mem_req']:>6} lim={c['mem_lim']:>6} | util={c['util']}% scaleUp={c.get('scaleup',4)}/15s min={c['min']} max={c['max']} | {b}-bound → {scale_by}")


if __name__ == "__main__":
    asyncio.run(main())
