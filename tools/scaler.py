"""
scaler.py — 경량 레이턴시 보조 스케일러

역할: HPA(CPU)가 못 보는 '레이턴시 악화'를 감지해 HPA minReplicas만 올린다.
     HPA가 주도하고, 이 툴은 바닥만 올려주는 보조다.

핵심 원칙:
  - 파드를 늘려서 해결되는 문제에만 반응한다.
  - '앱 자체가 느린 것'(파드 수와 무관)에는 반응하지 않는다.
  - 반응 조건: p95 > SLO AND 파드 CPU가 높음 (= 용량 부족 증거)
  - 이 두 조건이 동시에 성립해야만 min을 올린다.

사용법: python scaler.py <endpoint>
"""
import subprocess, sys, time, json, urllib.request, urllib.error
import uuid as _uuid, random as _random

NAMESPACE = "apdev"
APPS = ["user", "product", "stress"]
SLO = {"user": 200, "product": 200, "stress": 1000}

POLL = 10           # 측정 주기(초) — 너무 짧으면 노이즈, 너무 길면 늦음
PROBES = 5          # 앱당 프로브 수
UP_THRESHOLD = 1.0  # p95 >= SLO * 이 값이면 증설 후보
DN_THRESHOLD = 0.4  # p95 < SLO * 이 값이면 축소 후보
UP_CONSECUTIVE = 3  # 연속 3회(=30초) 위반 + CPU 높아야 올림
DN_CONSECUTIVE = 12 # 연속 12회(=2분) 정상이어야 내림
CPU_THRESHOLD = 50  # 파드 평균 CPU util이 이 이상이어야 '용량 부족'으로 인정

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def kubectl(cmd):
    r = subprocess.run(f"kubectl {cmd}", shell=True, capture_output=True, text=True)
    return r.returncode == 0, r.stdout.strip()


def get_hpa_min(app):
    ok, out = kubectl(f'-n {NAMESPACE} get hpa/{app}-hpa -o jsonpath="{{.spec.minReplicas}}"')
    try:
        return int(out.strip('"')) if ok else 1
    except ValueError:
        return 1


def get_hpa_current_util(app):
    """HPA가 보고하는 현재 CPU 이용률(%)."""
    ok, out = kubectl(f'-n {NAMESPACE} get hpa/{app}-hpa -o '
                      f'jsonpath="{{.status.currentMetrics[0].resource.current.averageUtilization}}"')
    try:
        return int(out.strip('"')) if ok else 0
    except (ValueError, TypeError):
        return 0


def set_hpa_min(app, val):
    patch = json.dumps({"spec": {"minReplicas": val}}).replace('"', '\\"')
    kubectl(f'-n {NAMESPACE} patch hpa/{app}-hpa --type=merge -p "{patch}"')


def load_config():
    """turn.py가 저장한 base_min/base_max를 읽는다."""
    import os
    cfg_path = os.path.join(os.path.dirname(__file__), "prewarm_cfg.json")
    try:
        with open(cfg_path) as f:
            c = json.load(f)
        base_min = {k: int(v) for k, v in (c.get("base_min") or {}).items()}
        base_max = {k: int(v) for k, v in (c.get("base_max") or {}).items()}
        return base_min, base_max
    except Exception:
        return {"user": 2, "product": 1, "stress": 1}, {"user": 15, "product": 6, "stress": 4}


def seed_data(endpoint):
    """프로브용 실제 데이터를 생성한다. 이게 없으면 GET 프로브가 빈 결과를 반환하며 느려진다."""
    rid = str(_random.randint(100000000000, 999999999999))
    uid = str(_uuid.uuid4())
    uname = f"_sc_{_random.randint(1000000, 9999999)}"
    pid = f"_sc_{_random.randint(1000000, 9999999)}"
    try:
        data = json.dumps({"requestid": rid, "uuid": uid, "username": uname, "email": f"{uname}@t.org"}).encode()
        req = urllib.request.Request(f"{endpoint}/v1/user", data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass
    try:
        data = json.dumps({"requestid": rid, "uuid": uid, "id": pid, "name": pid, "price": 1}).encode()
        req = urllib.request.Request(f"{endpoint}/v1/product", data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass
    return uname, pid


def probe(endpoint, app, seed_user, seed_product):
    """실제 존재하는 데이터를 조회해 응답시간을 측정한다."""
    rid = str(_random.randint(100000000000, 999999999999))
    uid = str(_uuid.uuid4())
    try:
        if app == "user":
            url = f"{endpoint}/v1/user?email={seed_user}@t.org&requestid={rid}&uuid={uid}"
            req = urllib.request.Request(url)
        elif app == "product":
            url = f"{endpoint}/v1/product?id={seed_product}&requestid={rid}&uuid={uid}"
            req = urllib.request.Request(url)
        else:
            data = json.dumps({"requestid": rid, "uuid": uid, "length": 100}).encode()
            req = urllib.request.Request(f"{endpoint}/v1/stress", data=data,
                                        headers={"Content-Type": "application/json"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        return (time.time() - t0) * 1000
    except Exception:
        return SLO[app] * 2


def measure(endpoint, app, seed_user, seed_product):
    """프로브 n회의 p95를 반환."""
    lats = sorted(probe(endpoint, app, seed_user, seed_product) for _ in range(PROBES))
    # p95 = 상위 5% 지점. 5개 중 가장 느린 1개.
    idx = max(0, int(len(lats) * 0.95) - 1)
    return lats[idx] if lats else SLO[app] * 2


def main():
    if len(sys.argv) < 2:
        print("사용법: python scaler.py <endpoint>")
        sys.exit(1)
    endpoint = sys.argv[1].rstrip("/")
    base_min, base_max = load_config()
    # max_min = min 올림의 상한. 과증설 방지.
    max_min = {a: min(base_max.get(a, 4), max(base_min.get(a, 2) + 3, base_max.get(a, 4) // 3)) for a in APPS}
    cur_min = {a: get_hpa_min(a) for a in APPS}
    up_count = {a: 0 for a in APPS}
    dn_count = {a: 0 for a in APPS}

    print(f"scaler 시작 — 레이턴시 + CPU 기반 보조")
    print(f"  base_min: {base_min}")
    print(f"  max_min:  {max_min}")
    print(f"  조건: p95 > SLO AND CPU > {CPU_THRESHOLD}% → min +1 (연속 {UP_CONSECUTIVE}회)")
    print(f"  복원: p95 < SLO×{DN_THRESHOLD} → min -1 (연속 {DN_CONSECUTIVE}회)")
    print()

    # seed 데이터 생성
    seed_user, seed_product = seed_data(endpoint)
    print(f"  probe seed: user={seed_user}, product={seed_product}")
    print()

    while True:
        for app in APPS:
            p95 = measure(endpoint, app, seed_user, seed_product)
            slo = SLO[app]
            cpu_util = get_hpa_current_util(app)

            if p95 >= slo * UP_THRESHOLD and cpu_util >= CPU_THRESHOLD:
                # 레이턴시 나쁨 + CPU 높음 = 용량 부족. 파드를 늘리면 해결됨.
                up_count[app] += 1
                dn_count[app] = 0
                if up_count[app] >= UP_CONSECUTIVE:
                    new = min(cur_min[app] + 1, max_min[app])
                    if new > cur_min[app]:
                        set_hpa_min(app, new)
                        print(f"[{time.strftime('%H:%M:%S')}] ↑ {app} min {cur_min[app]}→{new} "
                              f"(p95={p95:.0f}ms, cpu={cpu_util}%)")
                        cur_min[app] = new
                    up_count[app] = 0
            elif p95 < slo * DN_THRESHOLD:
                # 충분히 여유 있음 → 천천히 내림
                dn_count[app] += 1
                up_count[app] = 0
                if dn_count[app] >= DN_CONSECUTIVE:
                    new = max(cur_min[app] - 1, base_min.get(app, 1))
                    if new < cur_min[app]:
                        set_hpa_min(app, new)
                        print(f"[{time.strftime('%H:%M:%S')}] ↓ {app} min {cur_min[app]}→{new} "
                              f"(p95={p95:.0f}ms, 안정)")
                        cur_min[app] = new
                    dn_count[app] = 0
            else:
                # SLO 근처 — 유지
                up_count[app] = 0
                dn_count[app] = 0

        time.sleep(POLL)


if __name__ == "__main__":
    main()
