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

실제 첫 OIDC 인증·ECR Push 및 대상 digest 검증은 아직 실행하지 않았다. Workflow가 develop에 반영된 뒤 push 또는 develop 수동 실행으로 확인한다. 로컬 mock 결과는 AWS 성공의 증거가 아니다. 이번 로컬 작업에서 ECR/IAM/Kubernetes/Argo CD를 변경하지 않았다.

## 선택 요청: AI Helm values 변경 PR

점검한 인프라 dev에는 AI Helm values와 AI Application이 아직 없으며 AI 저장소의 DEV_GITOPS_TOKEN도 미등록이다. 기존 promotion 도구는 백엔드 5개 서비스/dev만 허용한다. 따라서 이번 전송 연결에서 빈 AI values나 임의 실행 설정을 만들어 PR을 생성하지 않는다.

이 작업의 검증된 image_uri/image_digest 출력을 다음 Helm 구성에 그대로 사용할 수 있다. 인프라에서 AI values·수동 Sync Application을 준비하고 별도 GitOps 토큰을 등록한 뒤, AI develop/ECR 이름에 맞는 엄격한 출처 검증으로 PR 생성을 추가한다. AI Auto Sync는 켜지 않는다. 기존 백엔드 5개 앱 정책도 변경하지 않는다.
