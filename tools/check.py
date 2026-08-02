"""
check.py — API 헬스체크 도구
각 애플리케이션(user, product, stress)의 POST/GET/PUT을 한 번씩 보내서 정상 응답 확인.

사용법: python check.py <endpoint>
"""
import asyncio
import aiohttp
import sys
import json
import uuid
import random
import time

TINY_PNG = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
    b'\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00'
    b'\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00'
    b'\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
)


def rid(): return str(random.randint(100000000000, 999999999999))
def uid(): return str(uuid.uuid4())


async def check_all(base: str):
    base = base.rstrip("/")
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        results = []
        pid = f"check_{rid()}"
        email = f"check_{rid()}@example.org"

        # ── 1. User POST ──
        t0 = time.time()
        try:
            async with session.post(f"{base}/v1/user",
                json={"requestid": rid(), "uuid": uid(), "username": f"check_{rid()}", "email": email},
                headers={"Content-Type": "application/json"}) as resp:
                body = await resp.text()
                lat = int((time.time() - t0) * 1000)
                results.append(("POST /v1/user", resp.status, lat, body[:100]))
        except Exception as e:
            results.append(("POST /v1/user", 0, 0, str(e)[:100]))

        # ── 2. User GET ──
        t0 = time.time()
        try:
            async with session.get(f"{base}/v1/user",
                params={"email": email, "requestid": rid(), "uuid": uid()},
                headers={"Content-Type": "application/json"}) as resp:
                body = await resp.text()
                lat = int((time.time() - t0) * 1000)
                results.append(("GET  /v1/user", resp.status, lat, body[:100]))
        except Exception as e:
            results.append(("GET  /v1/user", 0, 0, str(e)[:100]))

        # ── 3. Product POST ──
        t0 = time.time()
        try:
            async with session.post(f"{base}/v1/product",
                json={"requestid": rid(), "uuid": uid(), "id": pid, "name": f"check_{pid}", "price": 999},
                headers={"Content-Type": "application/json"}) as resp:
                body = await resp.text()
                lat = int((time.time() - t0) * 1000)
                results.append(("POST /v1/product", resp.status, lat, body[:100]))
        except Exception as e:
            results.append(("POST /v1/product", 0, 0, str(e)[:100]))

        # ── 4. Product GET ──
        t0 = time.time()
        try:
            async with session.get(f"{base}/v1/product",
                params={"id": pid, "requestid": rid(), "uuid": uid()},
                headers={"Content-Type": "application/json"}) as resp:
                body = await resp.text()
                lat = int((time.time() - t0) * 1000)
                results.append(("GET  /v1/product", resp.status, lat, body[:100]))
        except Exception as e:
            results.append(("GET  /v1/product", 0, 0, str(e)[:100]))

        # ── 5. Product PUT (이미지 업로드) ──
        t0 = time.time()
        try:
            form = aiohttp.FormData()
            form.add_field("id", pid)
            form.add_field("image", TINY_PNG, filename="check.png", content_type="image/png")
            async with session.put(f"{base}/v1/product", data=form) as resp:
                body = await resp.text()
                lat = int((time.time() - t0) * 1000)
                results.append(("PUT  /v1/product", resp.status, lat, body[:100]))
        except Exception as e:
            results.append(("PUT  /v1/product", 0, 0, str(e)[:100]))

        # ── 6. Image GET ──
        t0 = time.time()
        try:
            # PUT 응답에서 image_path 추출 시도
            img_path = None
            if results[-1][1] in (200, 201):
                try:
                    data = json.loads(results[-1][3])
                    img_path = data.get("image_path") or data.get("imagePath")
                except Exception:
                    pass
            url = f"{base}/images{img_path}" if img_path else f"{base}/images/{pid}/check.png"
            async with session.get(url) as resp:
                await resp.read()
                lat = int((time.time() - t0) * 1000)
                results.append(("GET  /images/...", resp.status, lat, f"url={url[-50:]}"))
        except Exception as e:
            results.append(("GET  /images/...", 0, 0, str(e)[:100]))

        # ── 7. Stress POST ──
        t0 = time.time()
        try:
            async with session.post(f"{base}/v1/stress",
                json={"length": 50},
                headers={"Content-Type": "application/json"}) as resp:
                body = await resp.text()
                lat = int((time.time() - t0) * 1000)
                results.append(("POST /v1/stress", resp.status, lat, body[:100]))
        except Exception as e:
            results.append(("POST /v1/stress", 0, 0, str(e)[:100]))

        # ── 결과 출력 ──
        print("\n" + "=" * 80)
        print(f"  API Health Check — {base}")
        print("=" * 80)
        print(f"  {'API':<20} {'Status':<8} {'Latency':<10} {'Result'}")
        print("  " + "-" * 76)

        all_ok = True
        for api, status, lat, body in results:
            if status in (200, 201):
                icon = "✅"
            elif status == 0:
                icon = "❌"
                all_ok = False
            else:
                icon = "⚠️"
                all_ok = False
            print(f"  {icon} {api:<18} {status:<8} {lat}ms{'':<5} {body[:50]}")

        print("  " + "-" * 76)
        if all_ok:
            print("  ✅ 모든 API 정상")
        else:
            print("  ❌ 일부 API에 문제 있음")
        print("=" * 80 + "\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python check.py <endpoint>")
        print("  예: python check.py https://d346kbimtuxxyu.cloudfront.net")
        sys.exit(1)
    asyncio.run(check_all(sys.argv[1]))
