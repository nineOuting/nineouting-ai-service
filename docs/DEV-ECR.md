# SCRUM-187 AI GHCR → DEV ECR

## 목적과 범위

AI 이미지는 GHCR에 있지만 DEV ECR이 비어 있어, 기존 `image.yml`의 빌드·검증 성공 후 DEV ECR로 AMD64 이미지를 전송한다. 모델/공용 코드/회의록 초안 검증과 AMD64·ARM64 빌드, develop·릴리스 GHCR 태그는 유지한다. 실제 EKS 배포와 Argo CD 설정 변경은 하지 않는다.

## 연결

- AWS 계정 `443845329525`, 리전 `ap-northeast-2`.
- OIDC 역할 `arn:aws:iam::443845329525:role/nineouting-dev-ai-service-github`.
- ECR `443845329525.dkr.ecr.ap-northeast-2.amazonaws.com/nineouting-dev-ai-service`.
- GHCR `ghcr.io/nineouting/nineouting-ai-service`.
- `build`의 두 플랫폼 검증 성공 → `merge`의 두 플랫폼 묶음 검증 성공 → `dev-ecr`.
- ECR 전송은 `refs/heads/develop`의 push 또는 workflow_dispatch에만 실행한다. PR·릴리스 태그·다른 브랜치에서는 실행하지 않는다.
- AWS Access Key와 Actions environment를 사용하지 않는다. 기존 OIDC subject의 develop 제한을 유지한다.

## 검증과 후속 출력

merge가 생성한 index digest는 Buildx `--metadata-file`의 `containerimage.descriptor.digest`에서 읽는다. 변할 수 있는 develop 태그 대신 이번 실행의 digest를 전송 Job으로 넘긴다. 형식 근거: https://docs.docker.com/reference/cli/docker/buildx/imagetools/create/#write-create-result-metadata-to-a-file---metadata-file

전송 스크립트는 인덱스에서 유일한 linux/amd64 manifest를 선택하고, ECR 복사 후 config digest·레이어 digest 일치와 실제 pull 이미지의 linux/amd64를 확인한다. 태그는 `sha-<commit>-<run-id>-<attempt>`로 중복 재사용을 피한다.

성공한 `dev-ecr` Job 출력:

- `image_repository`
- `image_digest`
- `image_uri` (repository@digest)
- `image_tag`

artifact `dev-ecr-image-<run-id>-<attempt>`에는 `image-result.txt`, ECR manifest와 조회 결과가 들어간다. Actions Summary에도 실제 URI·digest·플랫폼이 출력된다. 첫 성공 결과를 인프라 담당자에게 공유하고 수동 배포를 이어간다. 실패 Job만 재시도한 후 과거 attempt artifact를 섞지 않는다.

## 검증 상태

로컬 actionlint, shellcheck, bash 구문 검사, 전송 스크립트의 성공/실패 경로 mock 테스트를 통과했다. 기존 build Job 전체와 모델 검증이 변경되지 않았음을 비교했다. 명령:

```bash
actionlint .github/workflows/image.yml
shellcheck .github/scripts/publish-dev-ecr.sh
bash -n .github/scripts/publish-dev-ecr.sh
python3 .github/scripts/test_publish_dev_ecr.py
```

SCRUM-187 PR #67 병합 후 실제 실행 `42579944054` attempt 1에서 OIDC 인증·ECR Push·digest/플랫폼 검증이 성공했다. 검증 digest는 `sha256:859e546fc25351bc30bf81592840badce975be385af40bff11d006a8fbd04801`이다. 이번 SCRUM-188의 자동 PR 생성은 별도 검증 대상이며 아직 실제 실행 전이다.

## SCRUM-188: AI Helm values 변경 PR

인프라 PR #68 이후 dev에 AI values와 Auto Sync=false인 AI Application이 준비됐다. `dev-gitops` Job은 성공한 dev-ecr 뒤 동일 run/attempt artifact를 받아 인프라의 promotion 도구로 AI `image.digest` 한 줄 변경 PR을 생성한다.

먼저 인프라의 SCRUM-188 AI 매핑을 dev에 반영하고 AI 저장소 Actions Secret `DEV_GITOPS_TOKEN`에 인프라 Contents/Pull requests 읽기·쓰기 토큰을 등록해야 한다. 이 Workflow 변경을 develop에 반영한 뒤 실제 성공과 생성 PR의 인프라 CI를 확인한다. PR 실행에서는 dev-ecr/dev-gitops가 skip된다.

GitHub source/CI 읽기는 원본 AI `GITHUB_TOKEN` contents/actions 읽기 권한을 사용하고, 인프라 checkout/쓰기만 별도 GitOps 토큰을 사용한다. checkout 자격증명은 Git 설정에 남기지 않는다. 오래된 develop SHA, 실패 ECR, 다른 run/attempt artifact는 거절한다. 반복 실행의 artifact가 없으면 필요한 경우 Workflow 전체를 재실행한다.

자동 PR 생성은 AI 배포 완료를 뜻하지 않는다. AI Auto Sync는 켜지 않으며 이미지 PR 병합·실제 수동 Sync는 인프라 담당자의 별도 확인으로 진행한다. 기존 백엔드 5개 앱 정책은 변경하지 않는다.
