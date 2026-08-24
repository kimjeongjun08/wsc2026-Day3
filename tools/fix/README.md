# tools/fix — 앱 변경·추가 대비용 (평소엔 안 씀)

대회에서 **애플리케이션 스펙이 바뀌거나**(경로/메서드/필드) **앱이 추가로 나올 때** 쓰는 대비 도구.
지금 스펙 그대로면 아무것도 할 필요 없다.

## 파일

| 파일 | 역할 |
|---|---|
| `spec.json` | API 스펙 단일 소스 — 경로·메서드·쿼리/바디 필드·SLA 만 적는다 |
| `spec.py` | spec.json 을 waf.tf(생성기 위임)·튜너(score.py APPS/SLA_S)에 전파 |
| `extra-app.sh` | 추가 앱을 클러스터+ALB 에 배포/회수 (GO.sh 방식) |

## 1) 스펙이 바뀌었을 때

```bash
vi spec.json                  # 바뀐 경로/메서드/필드만 고친다
python3 spec.py               # 검사: 무엇이 바뀌는지 보고만
python3 spec.py --apply       # 반영: tools/spec.py + score.py + waf.tf locals
cd ../../terraform && terraform apply   # waf 배포
```

- waf 는 검증된 기존 생성기(`tools/apply_spec.py`)를 그대로 쓴다. AllowValid 본문은
  `tools/generated_waf_rules.tf.txt` 로 생성되니 **검토 후** waf.tf 에 반영해라 (자동으로 안 덮는다).
- 경로가 `/v1/<앱이름>` 규약을 벗어나면 spec.py 가 직접 고칠 파일 목록을 경고로 알려준다.

## 2) 앱이 추가로 나왔을 때

```bash
vi spec.json                  # extra 블록: enabled=true, name/path/image 등 채움
python3 spec.py --apply       # waf 허용 + 튜너 채점 대상에 편입
cd ../../terraform && terraform apply
# ECR push 후 spec.json 의 image 채우고:
./extra-app.sh deploy         # TG + 리스너 규칙 + Deployment/Service/HPA/TGB
./extra-app.sh status
```

- **돌고 있는 `./GO.sh watch` 는 그대로 둔다.** 튜너는 TG 를 `apdev-<앱>` 이름으로
  자동 발견하므로, score.py 에 편입만 되면 기존 앱들과 함께 감시·채점한다.
- baseline 2대 유지: 추가 앱 requests 를 기존 앱과 같은 70m 급으로 잡아 2대 동거가
  가능하게 했다. 분산 강제(topologySpread)·stress 노드 회피·HPA(33%) 도 기존 앱과 동일 골격.
- 회수: `./extra-app.sh remove` 후 spec.json 의 enabled=false 로 되돌리고
  `python3 spec.py --apply` (waf·튜너 원복).

## 주의

- `tools/spec.py` 는 이제 **자동 생성 파일**이다 — 직접 고치지 말고 spec.json 을 고쳐라.
- spec.py 는 `--apply` 없이 돌리면 아무것도 안 바꾼다. 대회 중엔 검사 먼저.
