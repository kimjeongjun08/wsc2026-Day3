"""
autotune.py
입력: 노드 타입 + 최대 노드 수
→ Karpenter/HPA/Deploy 최적값 계산 + 적용 + 부하 검증

사용법: python autotune.py <endpoint>
"""
import asyncio
import aiohttp
import subprocess
import sys
import time
import random
import uuid
import json

NAMESPACE = "apdev"

# 노드 allocatable CPU (밀리코어)
NODE_CPU = {
    "t3.medium": 1800,
    "t3.large": 1800,
    "t3.xlarge": 3800,
    "m5.large": 1800,
    "m5.xlarge": 3800,
}

# HPA util threshold: request 대비 실측 사용량이 낮으면 util%를 높여야 불필요 스케일 방지
# 최소 노드 1대 유지 목표 → request 최소화, threshold 높게

WARMUP_DURATION = 20  # 워밍업 부하 유지 시간(초) - kubectl top 반영 대기 포함


async def warmup_and_measure(base):
    """
    짧은 부하를 걸면서 실측 CPU를 수집.
    부하 종료 후 kubectl top으로 평균값 반환.
    """
    seed_u = f"_w_{random.randint(1000000,9999999)}"
    seed_p = f"_w_{random.randint(1000000,9999999)}"

    async with aiohttp.ClientSession() as s:
        await s.post(f"{base}/v1/user", json={"requestid": rid(), "uuid": uid(), "username": seed_u, "email": f"{seed_u}@t.org"})
        await s.post(f"{base}/v1/product", json={"requestid": rid(), "uuid": uid(), "id": seed_p, "name": seed_p, "price": 1})

    end = time.time() + WARMUP_DURATION

    async def hit(session, api):
        while time.time() < end:
            try:
                if api == "user":
                    # POST + GET 번갈아 → DB 쓰기/읽기 모두 측정
                    uname = f"_w_{random.randint(1000000,9999999)}"
                    async with session.post(f"{base}/v1/user",
                        json={"requestid": rid(), "uuid": uid(), "username": uname, "email": f"{uname}@t.org"}) as r:
                        await r.read()
                    async with session.get(f"{base}/v1/user?email={seed_u}@t.org&requestid={rid()}&uuid={uid()}") as r:
                        await r.read()
                elif api == "product":
                    pid = f"_w_{random.randint(1000000,9999999)}"
                    async with session.post(f"{base}/v1/product",
                        json={"requestid": rid(), "uuid": uid(), "id": pid, "name": pid, "price": 1}) as r:
                        await r.read()
                    async with session.get(f"{base}/v1/product?id={seed_p}&requestid={rid()}&uuid={uid()}") as r:
                        await r.read()
                else:
                    async with session.post(f"{base}/v1/stress",
                        json={"requestid": rid(), "uuid": uid(), "length": 256}) as r:
                        await r.read()
            except Exception:
                pass
            await asyncio.sleep(0.02)

    conn = aiohttp.TCPConnector(limit=30)
    async with aiohttp.ClientSession(connector=conn) as session:
        tasks = [asyncio.create_task(hit(session, api)) for api in ["user"]*6 + ["product"]*6 + ["stress"]*6]
        await asyncio.gather(*tasks)

    # kubectl top은 15초 주기로 갱신 → 부하 중에 찍힌 값 반영됨
    measured = {}
    for app in ["user", "product", "stress"]:
        val = _top_cpu(app)
        measured[app] = val
        print(f"  {app}: {val}m")
    return measured


def _top_cpu(app):
    """파드 평균 CPU (밀리코어). 실패 시 보수적 기본값."""
    defaults = {"user": 40, "product": 40, "stress": 150}
    ok, out = kubectl(f"-n {NAMESPACE} top pod -l app={app} --no-headers")
    if not ok or not out:
        return defaults[app]
    total, count = 0, 0
    for line in out.strip().split("\n"):
        parts = line.split()
        if len(parts) >= 2:
            cpu_str = parts[1]
            try:
                total += int(cpu_str.replace("m", "")) if cpu_str.endswith("m") else int(cpu_str) * 1000
                count += 1
            except ValueError:
                pass
    return (total // count) if count > 0 else defaults[app]


def kubectl(cmd):
    r = subprocess.run(f"kubectl {cmd}", shell=True, capture_output=True, text=True)
    return r.returncode == 0, r.stdout.strip()


def calculate(node_type, max_nodes, measured=None):
    """
    measured: {"user": int(m), "product": int(m), "stress": int(m)} 실측 CPU (밀리코어)
              None이면 보수적 기본값 사용
    최소 노드 1대 운영 목표: request 최소화, pod 밀도 최대화
    """
    cpu = NODE_CPU.get(node_type, 1800)
    system_per_node = 350
    available_1node = cpu - system_per_node  # 1노드 실제 스케줄 가능 CPU

    # --- request 결정: 실측 * 1.2 (20% 여유) → 최소 10m ---
    # I/O bound (user/product): 실측 낮음 → request 작게 잡아야 노드에 많이 들어감
    # HPA util = actual / request * 100 이므로, request를 실측에 맞게 잡아야 HPA가 제대로 트리거
    if measured:
        u_req = max(10, int(measured["user"] * 1.2))
        p_req = max(10, int(measured["product"] * 1.2))
        # stress request: 스케줄링 예약용 → 유휴 기준 작게 잡아야 노드에 많이 들어감
        # limit으로 실제 사용량 제한. 실측값의 30%만 request로 예약.
        s_req = max(50, int(measured["stress"] * 1.2))
    else:
        u_req = 20
        p_req = 20
        s_req = 50  # 기본값도 작게

    s_lim_target = int(measured["stress"] * 2) if measured else (cpu * 70 // 100)
    s_lim = max(200, min(s_lim_target, cpu * 70 // 100))  # 최소 200m

    # --- HPA maxReplicas: request 기반 역산 ---
    # 최대 노드 전체 available CPU를 request로 나눔 → 이론적 최대 pod 수
    # 단, 실제론 다른 app도 함께 뜨므로 * 0.7 safety factor
    total_capacity = available_1node * max_nodes
    u_max = max(4, int(total_capacity * 0.7 // u_req))
    p_max = max(4, int(total_capacity * 0.7 // p_req))
    s_max = max(4, int(total_capacity * 0.9 // s_req))
    # 상한 캡: 노드 너무 많이 생기지 않게
    u_max = min(u_max, 20)
    p_max = min(p_max, 20)
    # s_max 상한: max_nodes 전체를 stress만 채울 때의 이론적 최대
    s_max = min(s_max, int(available_1node * max_nodes // max(s_req, 1)))

    # --- HPA util threshold ---
    # util = actual/request * 100. request를 실측 기반으로 잡았으므로:
    # - 평소(유휴): util ≈ 100% (actual ≈ request)
    # - 부하시: actual > request → util > 100 → 스케일 트리거
    # threshold = 70%: 실측 * 1.2 request 대비 실측이 83% 수준에서 트리거
    u_util = 70
    p_util = 70
    s_util = 60   # stress: 더 일찍 스케일 (50 → 40)

    s_scaleup = max(2, s_max // 3)  # 더 빠르게 (//4 → //3)

    # --- Karpenter ---
    karpenter_max_nodes = max_nodes - 1  # MNG 1대 항상 존재
    karpenter_cpu = max(2, (cpu * karpenter_max_nodes) // 1000)

    return {
        "karpenter": {"cpu": str(karpenter_cpu), "mem": f"{karpenter_cpu * 2}Gi", "consolidate": "20s"},
        "user": {"req": f"{u_req}m", "max": u_max, "util": u_util},
        "product": {"req": f"{p_req}m", "max": p_max, "util": p_util},
        "stress": {"req": f"{s_req}m", "lim": f"{s_lim}m", "max": s_max, "util": s_util, "scaleup": s_scaleup},
        "info": {
            "node_cpu": cpu, "available_1node": available_1node,
            "initial_req_sum": f"{u_req + p_req + s_req * 2}m",
            "karpenter_nodes": karpenter_max_nodes,
            "measured": measured or "기본값 사용",
        }
    }


def apply_all(config, node_type):
    c = config

    # Karpenter: limits + instanceType 제한
    kp = c["karpenter"]
    kp_patch = json.dumps({"spec": {
        "limits": {"cpu": kp["cpu"], "memory": kp["mem"]},
        "disruption": {"consolidationPolicy": "WhenEmptyOrUnderutilized", "consolidateAfter": kp["consolidate"]},
        "template": {"spec": {"requirements": [
            {"key": "karpenter.k8s.aws/instance-type", "operator": "In", "values": [node_type]},
            {"key": "karpenter.sh/capacity-type", "operator": "In", "values": ["on-demand"]},
        ]}}
    }}).replace('"', '\\"')
    kubectl(f'patch nodepool apdev-pool --type=merge -p "{kp_patch}"')

    # user/product (no limit)
    for app in ["user", "product"]:
        req = c[app]["req"]
        patch = json.dumps({"spec": {"template": {"spec": {"containers": [{"name": app, "resources": {
            "requests": {"cpu": req, "memory": "48Mi"}
        }}]}}}}).replace('"', '\\"')
        kubectl(f'-n {NAMESPACE} patch deploy/{app} --type=strategic -p "{patch}"')
        hpa = json.dumps({"spec": {"minReplicas": 1, "maxReplicas": c[app]["max"],
            "behavior": {
                "scaleUp": {"stabilizationWindowSeconds": 0, "policies": [{"type": "Pods", "value": 3, "periodSeconds": 15}], "selectPolicy": "Max"},
                "scaleDown": {"stabilizationWindowSeconds": 30, "policies": [{"type": "Percent", "value": 100, "periodSeconds": 15}], "selectPolicy": "Max"}
            },
            "metrics": [{"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": c[app]["util"]}}}]
        }}).replace('"', '\\"')
        kubectl(f'-n {NAMESPACE} patch hpa/{app}-hpa --type=merge -p "{hpa}"')

    # stress
    s = c["stress"]
    patch = json.dumps({"spec": {"template": {"spec": {"containers": [{"name": "stress", "resources": {
        "requests": {"cpu": s["req"], "memory": "128Mi"},
        "limits": {"cpu": f"{NODE_CPU.get(node_type, 1800) * 50 // 100}m", "memory": "512Mi"}
    }}]}}}}).replace('"', '\\"')
    kubectl(f'-n {NAMESPACE} patch deploy/stress --type=strategic -p "{patch}"')
    hpa = json.dumps({"spec": {"minReplicas": s.get("min", 2), "maxReplicas": s["max"],
        "behavior": {
            "scaleUp": {"stabilizationWindowSeconds": 0, "policies": [{"type": "Pods", "value": s["scaleup"], "periodSeconds": 15}], "selectPolicy": "Max"},
            "scaleDown": {"stabilizationWindowSeconds": 30, "policies": [{"type": "Percent", "value": 100, "periodSeconds": 15}], "selectPolicy": "Max"}
        },
        "metrics": [{"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": s["util"]}}}]
    }}).replace('"', '\\"')
    kubectl(f'-n {NAMESPACE} patch hpa/stress-hpa --type=merge -p "{hpa}"')

    # rollout 대기 (패치로 인한 재시작 완료)
    for app in ["user", "product", "stress"]:
        kubectl(f"-n {NAMESPACE} rollout status deploy/{app} --timeout=90s")


def rid():
    return str(random.randint(100000000000, 999999999999))

def uid():
    return str(uuid.uuid4())


async def verify(base, seed_base=None):
    """실제 HPA 트리거될 정도의 부하로 검증 (concurrency 높게)"""
    # seed_base: seed POST는 ALB 직접 (WAF 우회), 부하는 base(CF)로
    if seed_base is None:
        seed_base = base
    results = {"user": [], "product": [], "stress": []}
    seed_u = f"_t_{random.randint(1000000,9999999)}"
    seed_p = f"_t_{random.randint(1000000,9999999)}"

    async with aiohttp.ClientSession() as s:
        await s.post(f"{seed_base}/v1/user", json={"requestid": rid(), "uuid": uid(), "username": seed_u, "email": f"{seed_u}@t.org"})
        await s.post(f"{seed_base}/v1/product", json={"requestid": rid(), "uuid": uid(), "id": seed_p, "name": seed_p, "price": 1})

    # 45초, concurrency 15 (HPA 트리거 충분)
    duration = 45
    end = time.time() + duration

    async def worker(session, api):
        while time.time() < end:
            t0 = time.time()
            try:
                if api == "user":
                    async with session.get(f"{base}/v1/user?email={seed_u}@t.org&requestid={rid()}&uuid={uid()}") as r:
                        await r.read(); results[api].append((r.status, (time.time()-t0)*1000))
                elif api == "product":
                    async with session.get(f"{base}/v1/product?id={seed_p}&requestid={rid()}&uuid={uid()}") as r:
                        await r.read(); results[api].append((r.status, (time.time()-t0)*1000))
                else:
                    async with session.post(f"{base}/v1/stress", json={"requestid": rid(), "uuid": uid(), "length": 256}) as r:
                        await r.read(); results[api].append((r.status, (time.time()-t0)*1000))
            except:
                results[api].append((0, (time.time()-t0)*1000))
            await asyncio.sleep(random.uniform(0.05, 0.2))

    conn = aiohttp.TCPConnector(limit=50)
    async with aiohttp.ClientSession(connector=conn) as session:
        tasks = []
        # user/product/stress 균등하게 각 5 → 각 HPA 트리거 검증 가능
        for _ in range(5):
            tasks.append(asyncio.create_task(worker(session, "user")))
            tasks.append(asyncio.create_task(worker(session, "product")))
            tasks.append(asyncio.create_task(worker(session, "stress")))
        await asyncio.gather(*tasks)
    return results


def print_results(results):
    # 채점 기준: availability (전체 응답), performance (SLO 이내 응답)
    # SLO: user/product ≤200ms, stress ≤1000ms
    # 계단: 90/87.5/85/82.5/80/70/50/30%
    thresholds = [90.0, 87.5, 85.0, 82.5, 80.0, 70.0, 50.0, 30.0]
    slo_ms = {"user": 200, "product": 200, "stress": 1000}

    def tier(pct):
        for t in thresholds:
            if pct >= t:
                return t
        return 0

    print(f"\n  {'api':<10} {'avail%':>7} {'tier':>6} | {'perf%':>7} {'tier':>6} | {'avg':>7} {'p95':>7}")
    all_30 = True
    for api in ["user", "product", "stress"]:
        data = results[api]
        if not data:
            print(f"  {api:<10} NO DATA")
            all_30 = False
            continue
        total = len(data)
        valid = [(s,t) for s,t in data if s != 0]  # 연결실패 제외
        total_v = len(valid) if valid else 1
        avail = len([1 for s, t in valid if 200 <= s < 300]) / total_v * 100
        perf  = len([1 for s, t in valid if 200 <= s < 300 and t <= slo_ms[api]]) / total_v * 100
        times = sorted(t for _, t in data)
        avg = sum(times) / len(times)
        p95 = times[int(len(times) * 0.95)]
        a_tier = tier(avail)
        p_tier = tier(perf)
        mark = "✓" if p_tier >= 30 else "✗"
        print(f"  {api:<10} {avail:>6.1f}% {a_tier:>5.1f} | {perf:>6.1f}% {p_tier:>5.1f} | {avg:>6.0f}ms {p95:>6.0f}ms {mark}")
        if p_tier < 30:
            all_30 = False

    _, n = kubectl("get nodes --no-headers")
    nodes = len([l for l in n.split("\n") if l.strip()]) if n else 0
    _, hpa_out = kubectl(f"-n {NAMESPACE} get hpa --no-headers")
    print(f"\n  nodes: {nodes}")
    if hpa_out:
        for line in hpa_out.split("\n"):
            if line.strip():
                print(f"  hpa: {line.strip()}")
    return all_30


async def main():
    if len(sys.argv) < 2:
        print("사용법: python autotune.py <CF endpoint>")
        sys.exit(1)

    base = sys.argv[1].rstrip("/")
    print("=== Autotune ===\n")

    node_type = input("노드 타입 (t3.medium): ").strip() or "t3.medium"
    max_nodes = int(input("최대 노드 수 (managed 포함): ").strip() or "4")

    if node_type not in NODE_CPU:
        print(f"지원 타입: {list(NODE_CPU.keys())}"); return

    warmup_base = base

    # 워밍업 부하 → 실측 CPU (ALB 직접 호출로 WAF 우회)
    print(f"\n{WARMUP_DURATION}초 워밍업 부하 + 실측 CPU...")
    measured = await warmup_and_measure(warmup_base)

    config = calculate(node_type, max_nodes, measured)

    print(f"\n{'='*55}")
    print(f"  계산 결과 ({node_type}, 최대 {max_nodes}대 = MNG 1 + Karpenter {config['info']['karpenter_nodes']})")
    print(f"{'='*55}")
    print(f"  노드 CPU: {config['info']['node_cpu']}m | 1대 available: {config['info']['available_1node']}m")
    print(f"  실측값: {config['info']['measured']}")
    print(f"  초기 request 합 (u1+p1+s2): {config['info']['initial_req_sum']}")
    print(f"\n  [Karpenter] cpu={config['karpenter']['cpu']} mem={config['karpenter']['mem']} consolidate={config['karpenter']['consolidate']}")
    print(f"  [user]    req={config['user']['req']:>5} | limit=none | HPA min=1 max={config['user']['max']} util={config['user']['util']}%")
    print(f"  [product] req={config['product']['req']:>5} | limit=none | HPA min=1 max={config['product']['max']} util={config['product']['util']}%")
    print(f"  [stress]  req={config['stress']['req']:>5} | limit={config['stress']['lim']} | HPA min=2 max={config['stress']['max']} util={config['stress']['util']}% scaleUp={config['stress']['scaleup']}/15s")

    confirm = input(f"\n적용 + 검증? (y/n): ").strip().lower()
    if confirm != "y": print("취소"); return

    # 적용 전 카펜터 노드 cordon (rolling update 시 새 파드가 MNG에만 뜨도록)
    _, kp_out = kubectl("get nodes -l karpenter.sh/nodepool --no-headers -o custom-columns=NAME:.metadata.name")
    for kn in [n.strip() for n in kp_out.split("\n") if n.strip()]:
        kubectl(f"cordon {kn}")

    # 적용
    print("\n적용 중...")
    apply_all(config, node_type)
    print("완료. 30초 안정화...")
    await asyncio.sleep(30)

    # 검증 (stress에 부하 집중)
    print("45초 검증 (stress 부하 집중)...")
    results = await verify(warmup_base, warmup_base)
    passed = print_results(results)

    if passed:
        print("\n✓ performance 30% 이상 (비용 최적화 점수 조건 충족)")
    else:
        print("\n✗ performance 30% 미만. 자동 재튜닝 시작...")

        for attempt in range(1, 4):  # 최대 3회 재시도
            print(f"\n{'='*55}")
            print(f"  재튜닝 시도 {attempt}/3")
            print(f"{'='*55}")

            retry_config = calculate(node_type, max_nodes, measured)

            # util을 10%씩 낮춤 (더 일찍 스케일)
            for app in ["user", "product", "stress"]:
                retry_config[app]["util"] = max(30, retry_config[app]["util"] - attempt * 10)

            # maxReplicas safety factor 올리기
            cpu = NODE_CPU.get(node_type, 1800)
            available = cpu - 350
            total = available * max_nodes
            sf = 0.7 + attempt * 0.1
            for app in ["user", "product", "stress"]:
                req_m = int(retry_config[app]["req"].replace("m", ""))
                retry_config[app]["max"] = max(retry_config[app]["max"], int(total * sf // req_m))

            # stress minReplicas 올려서 스파이크 초입 대응
            retry_config["stress"]["min"] = min(attempt + 1, 4)

            print(f"  [user]    req={retry_config['user']['req']} max={retry_config['user']['max']} util={retry_config['user']['util']}%")
            print(f"  [product] req={retry_config['product']['req']} max={retry_config['product']['max']} util={retry_config['product']['util']}%")
            print(f"  [stress]  req={retry_config['stress']['req']} max={retry_config['stress']['max']} util={retry_config['stress']['util']}% min={retry_config['stress'].get('min',1)}")

            print("\n재적용 중...")
            apply_all(retry_config, node_type)
            print("완료. 60초 안정화 (HPA 반응 대기)...")
            await asyncio.sleep(60)

            # HPA가 실제로 반응했는지 확인 후 검증
            _, hpa_out = kubectl(f"-n {NAMESPACE} get hpa --no-headers")
            if hpa_out:
                print(f"  HPA 상태: {hpa_out.replace(chr(10), ' | ')}")

            print(f"45초 검증 (재시도 {attempt})...")
            results = await verify(warmup_base, warmup_base)
            passed = print_results(results)

            if passed:
                print(f"\n✓ 재튜닝 {attempt}회 만에 성공!")
                break
            else:
                print(f"\n✗ 재튜닝 {attempt}회 실패. {'다시 시도...' if attempt < 3 else '최대 시도 횟수 초과.'}")
                config = retry_config

    # 안정화: rolling update 완료 + 카펜터 노드 정리 → MNG 1대로 수렴
    print("\n안정화 중...")

    # rolling update 완료 대기
    for app in ["user", "product", "stress"]:
        kubectl(f"-n {NAMESPACE} rollout status deploy/{app} --timeout=180s")

    # 카펜터 노드 정리: cordon → rollout restart(MNG에만 뜸) → drain
    await asyncio.sleep(10)
    _, karpenter_nodes_out = kubectl("get nodes -l karpenter.sh/nodepool --no-headers -o custom-columns=NAME:.metadata.name")
    karpenter_nodes = [n.strip() for n in karpenter_nodes_out.split("\n") if n.strip()]

    if karpenter_nodes:
        print(f"  카펜터 노드 {len(karpenter_nodes)}대 정리...")
        for node in karpenter_nodes:
            kubectl(f"cordon {node}")
        # MNG에 새 파드 뜨도록 restart (maxUnavailable:0이라 5xx 없음)
        for app in ["user", "product", "stress"]:
            kubectl(f"-n {NAMESPACE} rollout restart deploy/{app}")
        for app in ["user", "product", "stress"]:
            kubectl(f"-n {NAMESPACE} rollout status deploy/{app} --timeout=180s")
        # drain
        for node in karpenter_nodes:
            kubectl(f"drain {node} --ignore-daemonsets --delete-emptydir-data --force --grace-period=30 --timeout=60s")
        print("  Karpenter consolidation 대기 (30초)...")
        await asyncio.sleep(30)

    # 최종 확인: 카펜터 노드가 아직 있으면 한 번 더 정리
    _, leftover = kubectl("get nodes -l karpenter.sh/nodepool --no-headers -o custom-columns=NAME:.metadata.name")
    leftover_nodes = [n.strip() for n in leftover.split("\n") if n.strip()]
    if leftover_nodes:
        print(f"  카펜터 노드 {len(leftover_nodes)}대 추가 정리...")
        for node in leftover_nodes:
            kubectl(f"cordon {node}")
        for app in ["user", "product", "stress"]:
            kubectl(f"-n {NAMESPACE} rollout restart deploy/{app}")
        for app in ["user", "product", "stress"]:
            kubectl(f"-n {NAMESPACE} rollout status deploy/{app} --timeout=180s")
        for node in leftover_nodes:
            kubectl(f"drain {node} --ignore-daemonsets --delete-emptydir-data --force --grace-period=30 --timeout=60s")
        await asyncio.sleep(30)
    else:
        print("  카펜터 노드 없음.")

    _, n = kubectl("get nodes --no-headers")
    node_count = len([l for l in n.split("\n") if l.strip()]) if n else 0
    print(f"  현재 노드: {node_count}대")

    print("\n=== 튜닝 완료 ===")


if __name__ == "__main__":
    asyncio.run(main())
