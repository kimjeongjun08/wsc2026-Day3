"""nodewatch.py — 노드 과증설 원인 진단 (MNG vs Karpenter vs 앱별 파드 배치)

■ 왜 필요한가
  collector(채점기)는 계정의 '모든 EC2'를 세는데, scaler는 'Karpenter 노드만' 본다.
  그래서 MNG(managedNodeGroup, maxSize 4, role=worker)가 독립적으로 늘어나면
  scaler 눈엔 안 보이고 비용(avg_ec2)엔 그대로 반영된다 = 잡히지 않던 과증설.
  이 툴은 매 주기 '노드가 실제로 뭐로 차 있는지'를 찍어 원인을 확정한다.

■ 매 15초 출력 + nodewatch.csv 누적:
   - 총 노드 = MNG(카펜터 라벨 없음) + Karpenter(karpenter.sh/nodepool)
   - 앱별 Running 파드 수 (user/product/stress/overprovisioning)
   - 각 노드에 올라간 앱 (빈 노드·스필 즉시 보임)
   - Pending 파드 수

사용법: python nodewatch.py            # 15초 주기
        python nodewatch.py 5          # 5초 주기
  scaler/injector 돌아가는 동안 같이 실행. 노드 많을 때(예: plateau) 3~5분이면 충분.
"""
import subprocess
import sys
import os
import time
import json
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

NS = "apdev"
INTERVAL = int(sys.argv[1]) if len(sys.argv) > 1 else 15
CSV = os.path.join(os.path.dirname(__file__), "nodewatch.csv")


def kubectl(args):
    r = subprocess.run("kubectl " + args, shell=True, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def get_nodes():
    """{node: {'role':, 'karpenter':bool, 'ready':bool}}"""
    out = kubectl("get nodes -o json")
    nodes = {}
    if not out:
        return nodes
    try:
        for it in json.loads(out).get("items", []):
            name = it["metadata"]["name"]
            labels = it["metadata"].get("labels", {})
            conds = it.get("status", {}).get("conditions", [])
            ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conds)
            deleting = "deletionTimestamp" in it["metadata"]
            nodes[name] = {
                "role": labels.get("role", "-"),
                "karpenter": "karpenter.sh/nodepool" in labels,
                "ready": ready and not deleting,
            }
    except Exception:
        pass
    return nodes


def get_pods():
    """[(app, node, phase)] for apdev pods with app label."""
    out = kubectl(f"-n {NS} get pods -o json")
    pods = []
    if not out:
        return pods
    try:
        for it in json.loads(out).get("items", []):
            app = it["metadata"].get("labels", {}).get("app")
            if not app:
                continue
            pods.append((app, it["spec"].get("nodeName"), it.get("status", {}).get("phase")))
    except Exception:
        pass
    return pods


def ec2_running_pending():
    """계정 EC2 running+pending 수 (collector/채점기와 동일 정의). aws 없으면 None."""
    r = subprocess.run(
        'aws ec2 describe-instances --filters "Name=instance-state-name,Values=running,pending" '
        '--query "length(Reservations[].Instances[])" --output text',
        shell=True, capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip().isdigit():
        return int(r.stdout.strip())
    return None


def main():
    print(f"nodewatch — {INTERVAL}s 주기. Ctrl+C 종료. 로그: {CSV}")
    new = not os.path.exists(CSV)
    f = open(CSV, "a", encoding="utf-8")
    if new:
        f.write("ts,ec2,nodes_total,mng,karpenter,empty_worker,"
                "pods_user,pods_product,pods_stress,pods_pause,pending\n")
    try:
        while True:
            nodes = get_nodes()
            pods = get_pods()
            ec2 = ec2_running_pending()

            ready = {n: d for n, d in nodes.items() if d["ready"]}
            mng = [n for n, d in ready.items() if not d["karpenter"]]
            karp = [n for n, d in ready.items() if d["karpenter"]]

            # 노드별 앱 파드
            on_node = defaultdict(lambda: defaultdict(int))
            appcnt = defaultdict(int)
            pending = 0
            for app, node, phase in pods:
                if phase == "Pending":
                    pending += 1
                if phase == "Running":
                    appcnt[app] += 1
                    if node:
                        on_node[node][app] += 1

            empty_worker = [n for n in karp if not on_node.get(n)]

            ts = time.strftime("%H:%M:%S")
            print(f"\n[{ts}] EC2={ec2 if ec2 is not None else '?'}  "
                  f"K8s노드={len(ready)} (MNG {len(mng)} + Karpenter {len(karp)})  "
                  f"빈워커={len(empty_worker)}  Pending={pending}")
            print(f"   파드: user={appcnt.get('user',0)} product={appcnt.get('product',0)} "
                  f"stress={appcnt.get('stress',0)} pause={appcnt.get('overprovisioning',0)}")
            for n, d in ready.items():
                kind = "MNG " if not d["karpenter"] else "KARP"
                apps = " ".join(f"{a}×{c}" for a, c in on_node.get(n, {}).items()) or "★비어있음"
                print(f"     [{kind}] {n[-18:]:18} | {apps}")

            f.write(f"{int(time.time())},{ec2 if ec2 is not None else ''},{len(ready)},"
                    f"{len(mng)},{len(karp)},{len(empty_worker)},"
                    f"{appcnt.get('user',0)},{appcnt.get('product',0)},{appcnt.get('stress',0)},"
                    f"{appcnt.get('overprovisioning',0)},{pending}\n")
            f.flush()
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        print("\n종료.")
    finally:
        f.close()


if __name__ == "__main__":
    main()
