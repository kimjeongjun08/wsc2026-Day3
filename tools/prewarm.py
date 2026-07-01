"""
prewarm.py
트래픽 시작(경기 시작 1시간 뒤) 직전에 클러스터를 미리 데워 콜드스타트 SLO 미스를 방지.

트래픽 발생 시점이 예측 가능(T+1h)하므로, 첫 버스트가 들어오기 전에 HPA minReplicas(floor)를
목표치로 올려 파드를 미리 띄우고 Karpenter 가 노드를 프로비저닝해 Ready/Available 될 때까지
대기한다. 트래픽이 안정되면 --reset 으로 baseline(1/1/2) 복귀 → 이후 scaler.py/HPA 가 실부하에
맞춰 관리. Karpenter 노드 add + 이미지 pull 은 1~2분 걸리므로, 트래픽 3~5분 전에 실행 권장.

사용법:
  python prewarm.py                 # 기본 목표 user=3 product=3 stress=4 로 워밍업
  python prewarm.py 4 4 6           # user product stress 목표 replica 지정
  python prewarm.py --reset         # minReplicas 를 baseline(user=1 product=1 stress=2)로 복귀
의존성: kubectl (PATH), 표준 라이브러리만.
"""
import json
import subprocess
import sys
import time

NAMESPACE = "apdev"
APPS = ["user", "product", "stress"]
BASELINE = {"user": 1, "product": 1, "stress": 2}   # hpa.yaml 의 minReplicas 와 동일
DEFAULT_TARGET = {"user": 3, "product": 3, "stress": 4}
POLL_TIMEOUT = 240  # 초


def kubectl(cmd):
    r = subprocess.run(f"kubectl {cmd}", shell=True, capture_output=True, text=True)
    return r.returncode == 0, r.stdout.strip()


def set_min_replicas(app, val):
    patch = json.dumps({"spec": {"minReplicas": val}}).replace('"', '\\"')
    ok, _ = kubectl(f'-n {NAMESPACE} patch hpa/{app}-hpa --type=merge -p "{patch}"')
    return ok


def available(app):
    ok, out = kubectl(f"-n {NAMESPACE} get deploy/{app} --no-headers "
                      f"-o custom-columns=A:.status.availableReplicas")
    try:
        return int(out.strip())
    except Exception:
        return 0


def nodes_ready():
    ok, out = kubectl("get nodes --no-headers")
    if not ok or not out:
        return 0, 0
    lines = [l for l in out.splitlines() if l.strip()]
    ready = [l for l in lines if " Ready" in l and " NotReady" not in l]
    return len(ready), len(lines)


def reset():
    print("baseline(minReplicas) 복귀:")
    for app in APPS:
        ok = set_min_replicas(app, BASELINE[app])
        print(f"  {app:<8} min→{BASELINE[app]}  {'ok' if ok else 'FAIL'}")


def warm(target):
    print(f"prewarm 목표 minReplicas: {target}")
    for app in APPS:
        ok = set_min_replicas(app, target[app])
        print(f"  {app:<8} min→{target[app]}  {'ok' if ok else 'FAIL'}")

    print("\n노드/파드 Ready 대기...")
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        avail = {app: available(app) for app in APPS}
        rdy, total = nodes_ready()
        done = all(avail[app] >= target[app] for app in APPS)
        line = "  ".join(f"{app}={avail[app]}/{target[app]}" for app in APPS)
        print(f"  nodes {rdy}/{total} ready | {line}", end="\r", flush=True)
        if done:
            print(f"\n✅ 워밍업 완료 (nodes {rdy}/{total}). 트래픽 받을 준비 됨.")
            print("   트래픽 안정 후: python prewarm.py --reset")
            return 0
        time.sleep(5)
    print(f"\n⚠️ {POLL_TIMEOUT}s 내 목표 미달. 위 상태 확인 (Karpenter 노드/이미지 pull 지연 가능).")
    return 1


def main():
    args = sys.argv[1:]
    if args and args[0] == "--reset":
        reset()
        return
    target = dict(DEFAULT_TARGET)
    if len(args) == 3:
        try:
            target = {"user": int(args[0]), "product": int(args[1]), "stress": int(args[2])}
        except ValueError:
            print("사용법: python prewarm.py [user product stress] | --reset")
            sys.exit(2)
    sys.exit(warm(target))


if __name__ == "__main__":
    main()
