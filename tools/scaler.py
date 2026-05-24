#!/usr/bin/env python3
"""
EKS 스케일링 대시보드 + 즉시 스케일 + 자동 추천
실행: python scaler.py
"""
import subprocess
import json
import sys
import time
import os
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')

NS = "apdev"
APPS = ["user", "product", "stress"]
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"

# 목표 SLO
SLO = {
    "user":    {"response_ms": 200, "cpu_target": 50},
    "product": {"response_ms": 200, "cpu_target": 50},
    "stress":  {"response_ms": 1000, "cpu_target": 40},
}


def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True, shell=True)
    return result.stdout.strip()


def get_status():
    status = {}
    for app in APPS:
        dep = json.loads(run(f"kubectl get deploy {app} -n {NS} -o json"))
        hpa_raw = run(f"kubectl get hpa {app}-hpa -n {NS} -o json 2>nul")
        hpa = json.loads(hpa_raw) if hpa_raw else None

        replicas = dep["spec"]["replicas"]
        ready = dep.get("status", {}).get("readyReplicas", 0) or 0
        
        cpu_pct = 0
        if hpa:
            metrics = hpa.get("status", {}).get("currentMetrics", [])
            for m in metrics:
                if m.get("resource", {}).get("name") == "cpu":
                    cpu_pct = m["resource"]["current"].get("averageUtilization", 0)

        hpa_min = hpa["spec"]["minReplicas"] if hpa else "?"
        hpa_max = hpa["spec"]["maxReplicas"] if hpa else "?"
        hpa_desired = hpa["status"].get("desiredReplicas", "?") if hpa else "?"

        status[app] = {
            "replicas": replicas,
            "ready": ready,
            "cpu_pct": cpu_pct,
            "hpa_min": hpa_min,
            "hpa_max": hpa_max,
            "hpa_desired": hpa_desired,
        }
    
    # 노드 정보
    nodes_raw = run("kubectl get nodes -o json")
    nodes = json.loads(nodes_raw) if nodes_raw else {"items": []}
    node_count = len(nodes.get("items", []))
    
    return status, node_count


def print_dashboard(status, node_count):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"\n{BOLD}{'='*65}{RESET}")
    print(f"{BOLD}  EKS Scaler Dashboard │ {now} │ Nodes: {node_count}{RESET}")
    print(f"{BOLD}{'='*65}{RESET}")
    print(f"  {'App':<10} {'Pods':<12} {'CPU%':<10} {'HPA':<15} {'Status'}")
    print(f"  {'-'*60}")
    
    for app in APPS:
        s = status[app]
        # CPU 색상
        cpu = s["cpu_pct"]
        if cpu >= 80:
            cpu_str = f"{RED}{cpu}%{RESET}"
        elif cpu >= 50:
            cpu_str = f"{YELLOW}{cpu}%{RESET}"
        else:
            cpu_str = f"{GREEN}{cpu}%{RESET}"
        
        # Ready 상태
        if s["ready"] == s["replicas"]:
            ready_str = f"{GREEN}{s['ready']}/{s['replicas']}{RESET}"
        else:
            ready_str = f"{RED}{s['ready']}/{s['replicas']}{RESET}"
        
        hpa_str = f"{s['hpa_min']}-{s['hpa_max']} (want:{s['hpa_desired']})"
        
        # 추천
        target = SLO[app]["cpu_target"]
        if cpu > 80:
            rec = f"{RED}▲ SCALE UP{RESET}"
        elif cpu > target:
            rec = f"{YELLOW}~ watching{RESET}"
        elif s["replicas"] > 1 and cpu < 20:
            rec = f"{CYAN}▼ can reduce{RESET}"
        else:
            rec = f"{GREEN}● OK{RESET}"
        
        print(f"  {app:<10} {ready_str:<20} {cpu_str:<18} {hpa_str:<15} {rec}")
    
    print(f"{BOLD}{'='*65}{RESET}")


def scale(app, replicas):
    print(f"{YELLOW}  Scaling {app} → {replicas} replicas...{RESET}")
    run(f"kubectl scale deploy {app} -n {NS} --replicas={replicas}")
    print(f"{GREEN}  Done.{RESET}")


def scale_all(replicas_map):
    for app, r in replicas_map.items():
        scale(app, r)


def recommend(status):
    print(f"\n{BOLD}  Recommendations:{RESET}")
    for app in APPS:
        s = status[app]
        cpu = s["cpu_pct"]
        current = s["replicas"]
        target_cpu = SLO[app]["cpu_target"]
        
        if cpu == 0:
            print(f"  {app}: no metrics yet")
            continue
        
        # 필요 replicas = current * (cpu / target)
        ideal = max(1, int(current * cpu / target_cpu + 0.5))
        
        if ideal > current:
            print(f"  {app}: {RED}CPU {cpu}% > target {target_cpu}% → scale {current} → {ideal}{RESET}")
        elif ideal < current and cpu < 20:
            print(f"  {app}: {CYAN}CPU {cpu}% low → scale {current} → {max(1, current-1)}{RESET}")
        else:
            print(f"  {app}: {GREEN}OK (CPU {cpu}%, {current} pods){RESET}")


def interactive():
    while True:
        try:
            status, node_count = get_status()
            print_dashboard(status, node_count)
            recommend(status)
            
            print(f"\n{DIM}  Commands: [s]cale <app> <n> │ [a]ll <n> │ [r]efresh │ [q]uit{RESET}")
            cmd = input(f"  > ").strip().lower()
            
            if cmd == "q":
                break
            elif cmd == "r" or cmd == "":
                continue
            elif cmd.startswith("s "):
                parts = cmd.split()
                if len(parts) == 3:
                    scale(parts[1], int(parts[2]))
            elif cmd.startswith("a "):
                n = int(cmd.split()[1])
                scale_all({app: n for app in APPS})
            else:
                print(f"{RED}  Unknown command{RESET}")
        except KeyboardInterrupt:
            print("\n  Bye.")
            break
        except Exception as e:
            print(f"{RED}  Error: {e}{RESET}")
            time.sleep(2)


def loop_mode(interval):
    print(f"{DIM}  Auto-refresh every {interval}s (Ctrl+C to stop){RESET}")
    while True:
        try:
            os.system("cls" if os.name == "nt" else "clear")
            status, node_count = get_status()
            print_dashboard(status, node_count)
            recommend(status)
            time.sleep(interval)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--loop":
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else 15
        loop_mode(interval)
    else:
        interactive()
