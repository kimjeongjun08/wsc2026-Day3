"""scaler_guard.py — scaler.py 헬스체크 + 자동재시작 (SPOF 보강, hint [4])

■ 왜 필요한가
  HPA util을 껐으므로(hint [2]) scaler가 '유일한' 스케일 판단주체다. scaler가 죽거나 멈추면
  스케일링이 정지 → 부하 증가 시 파드가 영구 Pending → 성능/가용성 붕괴 → 비용 게이트까지 0.
  이 watchdog가 scaler.py를 띄우고 두 신호로 이상을 감지해 자동 재시작한다:
    (1) 프로세스 종료(크래시/OOM)         → proc.poll()
    (2) scaler_state.json 하트비트 정지    → ts가 HEARTBEAT_MAX 초과(정상은 2s 주기)

■ 사용법 (scaler.py 대신 이걸 실행)
    python scaler_guard.py <CF endpoint>
  인자는 scaler.py로 그대로 전달된다. Ctrl+C 시 scaler도 함께 종료.
"""
import subprocess
import sys
import os
import time
import json
import signal

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "scaler_state.json")
SCALER = os.path.join(HERE, "scaler.py")

HEARTBEAT_MAX = 20.0   # scaler_state.json ts가 이보다 오래되면 '멈춤'으로 판정(정상 2s 주기)
CHECK = 5              # 감시 주기(초)
MIN_RESTART_GAP = 10   # 재시작 최소 간격(크래시 루프 시 폭주 방지)
START_GRACE = 30       # 기동 직후 이 시간엔 하트비트 미검사(초기 측정/부팅 여유)


def state_age():
    try:
        with open(STATE) as f:
            return time.time() - float(json.load(f).get("ts", 0))
    except Exception:
        return None


def launch(args):
    return subprocess.Popen([sys.executable, SCALER] + args)


def main():
    args = sys.argv[1:]
    if not args:
        print("사용법: python scaler_guard.py <CF endpoint>")
        sys.exit(1)

    print(f"[guard] scaler 감시 시작 — 프로세스 종료 또는 하트비트>{HEARTBEAT_MAX:.0f}s 시 자동 재시작")
    proc = launch(args)
    started_at = time.time()
    restarts = 0

    def _stop(*_):
        print("\n[guard] 종료 — scaler도 함께 정리")
        try:
            if proc.poll() is None:
                proc.terminate()
                time.sleep(2)
                if proc.poll() is None:
                    proc.kill()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    while True:
        time.sleep(CHECK)
        dead = proc.poll() is not None
        age = state_age()
        # 기동 직후 유예 구간엔 하트비트로 죽었다 판정하지 않음
        stale = (age is not None
                 and time.time() - started_at > START_GRACE
                 and age > HEARTBEAT_MAX)

        if not (dead or stale):
            continue

        reason = "프로세스 종료" if dead else f"하트비트 정지({age:.0f}s)"
        # 크래시 루프 방지: 마지막 기동 후 최소 간격 지켰을 때만 재시작
        if time.time() - started_at < MIN_RESTART_GAP:
            time.sleep(MIN_RESTART_GAP)

        print(f"[guard] scaler 이상 감지({reason}) → 재시작 #{restarts + 1} "
              f"({time.strftime('%H:%M:%S')})")
        try:
            if proc.poll() is None:      # 멈췄지만 살아있으면(행) 먼저 죽인다
                proc.terminate()
                time.sleep(3)
                if proc.poll() is None:
                    proc.kill()
        except Exception:
            pass

        proc = launch(args)
        started_at = time.time()
        restarts += 1


if __name__ == "__main__":
    main()
