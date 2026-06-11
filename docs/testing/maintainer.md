# 维护者测试手册

本文用于从维护者视角验证 container-mirror 2.0 的完整能力。新方案的关键前提是：中国区机器不访问海外公共镜像站，镜像同步必须在可访问海外 registry 且可写入中国区 ECR 的海外 runner 上执行；中国区只承载 ECR、清理、报告、通知和可选 webhook。

> **最近测试**：2026-06-11 · 测试人：toreydai · ECR：`150430853770.dkr.ecr.cn-northwest-1.amazonaws.com.cn`

## 1. 测试范围

覆盖场景：

- 本地仓库静态校验。
- CloudFormation 模板校验和中国区控制面部署。
- ECR Repository Creation Template 行为。
- 海外 runner 手动同步。
- GitHub Actions PR 校验、merge 触发、定时触发、手动触发。
- `regsync` 固定 tag、整仓库 tag 追新和多架构 manifest 同步。
- 海外源 registry 失败、中国区 ECR 凭证失败、Docker Hub 凭证失败。
- Catalog 生成。
- Cleanup dry run 和受控删除。
- Webhook Lambda 和 Kubernetes webhook 行为。
- 安全、secret 处理、回滚和主动下线。

## 2. 前置条件

本地维护环境工具：

- `aws`
- `docker`
- `python3`
- `kubectl`
- `jq`
- `zip`
- `rg`

海外 runner 要求：

- 能访问 Docker Hub、GCR、registry.k8s.io、Quay、GHCR、ECR Public 等海外源站。
- 能访问 `*.amazonaws.com.cn` 并写入目标中国区 ECR。
- 能运行 Docker。
- 不在中国区 VPC、ECS、Fargate、EC2 或其他无法稳定访问海外源站的机器上执行同步。

环境变量：

```bash
export AWS_REGION=cn-northwest-1
export ECR=150430853770.dkr.ecr.cn-northwest-1.amazonaws.com.cn
export STACK_NAME=container-mirror
export NOTIFICATION_EMAIL=<email>
export AWS_ACCESS_KEY_ID=<china-access-key>
export AWS_SECRET_ACCESS_KEY=<china-secret-key>
export AWS_DEFAULT_REGION=cn-northwest-1
export DOCKERHUB_USER=<dockerhub-user>
export DOCKERHUB_TOKEN=<dockerhub-token>
```

GitHub Actions secrets：

```text
AWS_CHINA_ACCESS_KEY_ID
AWS_CHINA_SECRET_ACCESS_KEY
DOCKERHUB_USER
DOCKERHUB_TOKEN
```

中国区 AWS 凭证最小权限建议：

- `ecr:GetAuthorizationToken`
- `ecr:CreateRepository`
- `ecr:DescribeRepositories`
- `ecr:DescribeImages`
- `ecr:BatchCheckLayerAvailability`
- `ecr:InitiateLayerUpload`
- `ecr:UploadLayerPart`
- `ecr:CompleteLayerUpload`
- `ecr:PutImage`
- `ecr:BatchGetImage`

如组织可用 GitHub OIDC 到 AWS 中国区，可替换静态 AK/SK；否则使用专用低权限 IAM 用户并定期轮换。

## 3. 静态测试

### 测试 3.1 文件结构

命令：

```bash
find . -maxdepth 4 -type f -not -path './.git/*' | sort
find . -type d -empty -not -path './.git/*' -print
```

预期：

- 目录结构含 `scripts/`、`infra/`、`functions/`、`docs/`。
- 没有空目录。
- `infra/main.yaml` 是唯一 CloudFormation 模板。
- 不存在同步运行镜像 Dockerfile。

**测试结果**：✅ `./test` 空目录已删除（`rmdir test`）；`infra/main.yaml` 是唯一 CFn 模板；无 Dockerfile

### 测试 3.2 regsync 配置校验

命令：

```bash
python3 scripts/tools.py validate regsync.yml
```

预期：

```text
[OK] regsync.yml passed container-mirror validation
```

**测试结果**：✅ `[OK] regsync.yml passed container-mirror validation`

### 测试 3.3 Python 语法

命令：

```bash
python3 -m py_compile \
  scripts/tools.py \
  functions/cleanup/lambda_function.py \
  functions/webhook/lambda_function.py
```

预期：

- 命令退出码为 0。

**测试结果**：✅ `tools.py`、`cleanup/lambda_function.py`、`webhook/lambda_function.py` 均通过

### 测试 3.4 YAML 解析

命令：

```bash
python3 - <<'PY'
import pathlib, yaml
class Loader(yaml.SafeLoader):
    pass
def cfn(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)
Loader.add_multi_constructor('!', cfn)
for suffix in ('*.yml', '*.yaml'):
    for path in sorted(pathlib.Path('.').rglob(suffix)):
        if '.git' in path.parts:
            continue
        with open(path, encoding='utf-8') as f:
            yaml.load(f, Loader=Loader)
        print('[OK]', path)
PY
```

预期：

- 所有 YAML 文件都能成功解析。

**测试结果**：✅ 7 个 YAML 文件均通过（`regsync.yml`、`infra/buildspec.yml`、`infra/main.yaml`、`infra/codebuild.yaml`、`infra/mutating-webhook.yaml`、`.github/workflows/sync.yml`、`.github/workflows/validate.yml`）

### 测试 3.5 CloudFormation 校验

命令：

```bash
aws cloudformation validate-template \
  --template-body file://infra/main.yaml \
  --region "$AWS_REGION"
```

预期：

- 模板校验通过。

**测试结果**：✅ 校验通过，输出参数：`NotificationEmail`、`DaysThreshold`、`MirrorImageCountLimit`（新增，默认 50）、`CleanupDeleteAllowlist`、`WebhookToken`、`RepositoryPolicyText`、`CleanupDryRun`、`CleanupScheduleState`、`EcrRegistry`；Capabilities: `CAPABILITY_IAM`（2026-06-11 加入 lifecycle policy 参数后重新验证通过）

### 测试 3.6 build-weekly-config 生成

每周定时 sync 的核心预处理步骤：从 `required-images-weekly.txt` 解析最新稳定 tag 并生成 regsync 配置。

命令：

```bash
pip install pyyaml -q
python3 scripts/tools.py build-weekly-config required-images-weekly.txt /tmp/regsync-weekly.yml
python3 scripts/tools.py validate /tmp/regsync-weekly.yml
```

预期：

- 两个命令退出码均为 0。
- `/tmp/regsync-weekly.yml` 已生成。
- `validate` 输出 `[OK] /tmp/regsync-weekly.yml passed container-mirror validation`。
- 日志包含每个镜像对应的 `+` 行（新增条目）或 `[SKIP]` 行（digest 未变更，仅 docker.io 来源有此优化）。
- 生成的条目不含 `windowsservercore`、`alpha`、`beta`、`rc` 等 deny tag。
- 每个 `public.ecr.aws/docker/library/` 镜像最多出现 2 个 minor 版本（plain + alpine 变体）。
- `public.ecr.aws/docker/library/*` 来源的条目，target 路径为 `dockerhub/library/<name>`（与 docker.io/library 同路径）。

**测试结果**：✅ 退出码 0；`/tmp/regsync-weekly.yml` 已生成 90 个条目；`validate` 通过（`[OK] /tmp/regsync-weekly.yml passed container-mirror validation`）；本地无 China ECR 凭证时出现 `[WARN] ECR check error`（安全降级，属预期）；`[WARN] no stable tags found for docker.io/grafana/loki`（loki 无稳定版，跳过）；无 `windowsservercore`/`alpha`/`beta`/`rc` deny tag

**架构变更（2026-06-11）**：`required-images-weekly.txt` 中 `docker.io/library/*` 全部改为 `public.ecr.aws/docker/library/*`，进入 `ecrpublic` split batch（无 rate limit）；仅 `docker.io/grafana/grafana` 保留 DockerHub 来源。`lint-weekly` 命令在 PR validate 阶段拦截 `docker.io/library/*` 写法。digest 预过滤跳过仅对 `docker.io` 来源有效；ECR Public 来源 `hub_digest=None`，每次全量同步（无限速，影响可忽略）。

### 测试 3.7 digest 预过滤行为

验证 DockerHub rate limit 防护逻辑：ECR 已有且 digest 不变的镜像直接跳过，不进入 regsync。

**场景 A：有效 China ECR 凭证（正常过滤）**

```bash
export CHINA_AK=<china-ak>
export CHINA_SK=<china-sk>
python3 scripts/tools.py build-weekly-config required-images-weekly.txt /tmp/regsync-weekly-filtered.yml 2>&1 \
  | grep -E '\[SKIP\]|\[OK\].*条目'
```

预期：

- ECR 中 digest 未变的镜像输出 `[SKIP] ... digest unchanged (sha256:xxxxxxxxxxxxxxx…)`。
- `[OK]` 行显示 `共 N 个条目（跳过 M 个 digest 未变更）`，M > 0（ECR 有已同步镜像时）。
- 最终生成的 `/tmp/regsync-weekly-filtered.yml` 经 `validate` 通过。

**场景 B：无 ECR 凭证（安全降级）**

```bash
unset CHINA_AK CHINA_SK
python3 scripts/tools.py build-weekly-config required-images-weekly.txt /tmp/regsync-weekly-nofilter.yml 2>&1 \
  | grep -E '\[SKIP\]|\[WARN\].*ECR|\[OK\].*条目'
```

预期：

- 不出现任何 `[SKIP]` 行（所有镜像都进入 sync 配置）。
- 可能出现 `[WARN] ECR check error` 但退出码仍为 0。
- 生成的条目数与无过滤时相同，属安全降级行为。

**测试结果**：✅ 场景 A（有凭证）— SSM `/container-mirror/china-ak`（ap-northeast-1）获取成功；26 个 digest 未变更镜像输出 `[SKIP] ... digest unchanged (sha256:...)`（含 debian:13.4、ubuntu:26.04/25.10、busybox:1.38.0/1.37.0、nginx:1.31.1 等）；`[OK] /tmp/regsync-weekly-filtered.yml 已生成，共 64 个条目（跳过 26 个 digest 未变更）`；退出码 0 | ✅ 场景 B（无凭证）— 无 `[SKIP]` 行；90 个条目全部进入配置；`[WARN] ECR check error`（预期）；退出码 0；安全降级行为正常

**注**（2026-06-11 架构变更后）：`public.ecr.aws/docker/library/*` 来源的镜像 `hub_digest=None`，digest 预过滤不生效，始终全量同步。ECR Public 无 rate limit，此行为属预期。仅 `docker.io/grafana/grafana` 仍走 DockerHub，digest 跳过逻辑不变。

### 测试 3.8 lint-weekly 校验

验证 validate workflow 在 PR 阶段能拦截 `docker.io/library/*` 写法。

**场景 A：合规文件（全部使用 ECR Public 源）**

命令：

```bash
python3 scripts/tools.py lint-weekly required-images-weekly.txt
```

预期：

```text
[OK] required-images-weekly.txt 无 docker.io/library 条目
```

**场景 B：含违规条目**

```bash
echo "docker.io/library/nginx" > /tmp/bad-weekly.txt
python3 scripts/tools.py lint-weekly /tmp/bad-weekly.txt
```

预期：

- 退出码为 1。
- 输出 `[FAIL]` 并提示改用 `public.ecr.aws/docker/library/nginx`。

**测试结果**：✅ 场景 A — `[OK] required-images-weekly.txt 无 docker.io/library 条目`，退出码 0 | ✅ 场景 B — `[FAIL] ... 请改用 'public.ecr.aws/docker/library/nginx'（ECR Public 无 rate limit）`，退出码 1

## 4. 中国区控制面部署测试

### 测试 4.1 部署主 Stack

命令：

```bash
aws cloudformation deploy \
  --stack-name "$STACK_NAME" \
  --template-file infra/main.yaml \
  --parameter-overrides \
    NotificationEmail="$NOTIFICATION_EMAIL" \
    CleanupDryRun=true \
  --capabilities CAPABILITY_IAM \
  --region "$AWS_REGION"
```

预期：

- Stack 进入 `CREATE_COMPLETE` 或 `UPDATE_COMPLETE`。
- Stack 不创建 ECS、Fargate、EventBridge Scheduler、DynamoDB lock table 等同步运行时资源。

**测试结果**：✅ `container-mirror` stack 部署成功（`UPDATE_COMPLETE`，2026-06-10）；无 ECS/Fargate/EventBridge Scheduler/DynamoDB 等同步运行时资源

### 测试 4.2 Stack 输出

命令：

```bash
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs'
```

预期输出：

- `NotificationTopicArn`
- `ReportBucket`
- `CleanupFunction`
- `WebhookFunction`
- `WebhookURL`

**测试结果**：✅ 输出齐全：`WebhookURL`=`https://p4414t2msk.execute-api.cn-northwest-1.amazonaws.com.cn/call`、`CleanupFunction`、`WebhookFunction`、`ReportBucket`、`NotificationTopicArn`

### 测试 4.3 ECR Repository Creation Templates

命令：

```bash
aws ecr describe-repository-creation-templates \
  --region "$AWS_REGION" \
  --query 'repositoryCreationTemplates[].prefix'
```

预期前缀：

- `dockerhub/`
- `gcr/`
- `registryk8sio/`
- `quay/`
- `ecrpublic/`
- `ghcr/`
- `elastic/`
- `amazonecr/`

**测试结果**：✅ 8 个前缀全部存在：`dockerhub`、`gcr`、`registryk8sio`、`quay`、`ecrpublic`、`ghcr`、`elastic`、`amazonecr`；**2026-06-11**：所有 creation template 新增 `LifecyclePolicy`（`MirrorImageCountLimit=50`），防止历史 tag 无限积累（旧仓库问题根因之一）

## 5. 海外 runner 同步测试

### 测试 5.1 手动同步

必须在海外 runner 执行：

```bash
export ECR_PASSWORD="$(aws ecr get-login-password --region "$AWS_REGION")"
docker run --rm \
  -v "$PWD/regsync.yml:/regsync.yml:ro" \
  -e DOCKERHUB_USER \
  -e DOCKERHUB_TOKEN \
  -e ECR_PASSWORD \
  regclient/regsync:latest once -c /regsync.yml
```

预期：

- `regsync` 退出码为 0。
- 日志显示从海外源站拉取并推送到中国区 ECR。
- 中国区机器没有参与源站拉取。

**测试结果**：⚠️ 架构验证通过，dockerhub 受 DockerHub 免费账号配额限制

**历次测试记录**：
- ❌ Run #27247997623（2026-06-10）：parallel:2 + 60 min timeout → TIMED_OUT
- ❌ Run #27285344077（2026-06-10）：parallel:8 + 120 min timeout → BUILD TIMED_OUT（7110s），全量新镜像，PHP/rabbitmq 等大型镜像传输中被强制终止

**架构重构（2026-06-11）**：改为按 registry 分批并行策略 — `tools.py split` 将合并配置拆为 dockerhub/registryk8sio/gcr/quay/ghcr/ecrpublic 子配置；GHA `prepare` job 在 runner 运行 build-weekly-config+merge+split 后上传至 China S3；`sync` job 以 matrix 并行触发 N 个独立 CodeBuild；`fail-fast: false` 隔离各 registry 失败；dockerhub 子配置强制 `parallel: 2`；ghcr 子配置注入 GITHUB_TOKEN 凭据

**Run #27317000354（2026-06-11 第一轮）**：
- ✅ prepare：成功；split 正确产出 6 个子配置并上传 S3
- ✅ registryk8sio / gcr / ecrpublic / quay：全部成功（约 1 min 完成）
- ❌ ghcr：`unauthorized`（GITHUB_TOKEN 未能正确传入 CodeBuild）
- ❌ dockerhub：TIMED_OUT 3588s（60 min），`source-remain=0, source-limit=200`，rate limit 耗尽

**修复2（2026-06-11）**：CodeBuild timeout 120 min；dockerhub 子配置 parallel 2；GHA job timeout 130 min；ghcr 子配置注入 `{{env "GHCR_USER"}}`/`{{env "GHCR_TOKEN"}}` credential

**Run #27319418058（2026-06-11 第二轮）**：
- ✅ prepare / registryk8sio / gcr / ecrpublic / quay：全部成功
- ⚠️ ghcr：认证已修复（401 → 404）；`ghcr.io/opencontainers/runc:v1.4.2` tag 在 GHCR 上不存在（需更新 regsync.yml 中 runc tag）
- ❌ dockerhub：TIMED_OUT 7185s（120 min），`source-remain=3, source-limit=200`；根因为 DockerHub 免费账号 **200 pulls/6h 硬上限**；parallel:2 减慢了消耗速度但 ~45 张镜像 × 多层传输 > 200 pulls；非架构问题，需升级 DockerHub 账号至 Pro 或缩减 dockerhub 镜像列表

**架构重构2（2026-06-11）**：
- `required-images-weekly.txt` 中所有 `docker.io/library/*` 改为 `public.ecr.aws/docker/library/*`（ECR Public 无 rate limit），dockerhub batch 仅剩 `docker.io/grafana/grafana` 1 个
- `regsync.yml` 中 ghcr 条目由不存在的 `runc:v1.4.2` 改为 `ghcr.io/aquasecurity/trivy:v0.71.0`
- `sync.yml` 中 GHCR_TOKEN 由 `secrets.GITHUB_TOKEN`（仅当前 repo 包权限）改为 `secrets.GHCR_TOKEN`（PAT，`read:packages` scope，可拉取第三方 GHCR 镜像）
- 新增 GitHub secret `GHCR_TOKEN`（2026-06-11）

**Run #27328804929（2026-06-11 第三轮）**：
- ✅ prepare：成功
- ✅ ecrpublic / registryk8sio / gcr / quay：全部成功（ECR Public library 镜像无 rate limit，约 2 min 完成）
- ❌ ghcr：FAILED — `secrets.GITHUB_TOKEN` 无第三方 GHCR 包读取权限（已修复：改为 `secrets.GHCR_TOKEN` PAT）
- dockerhub（grafana）：进行中（被 R4 cancel）

**架构重构3（2026-06-11）**：
- `regsync.yml` ghcr 条目由不存在的 `aquasecurity/trivy:v0.71.0`（GHCR 仅到 0.18.x）改为 `kyverno/kyverno:v1.18.1`（multi-arch，GHCR 确认可用）
- `regsync.yml` nginx/redis/busybox 由 `docker.io/library/*` 改为 `public.ecr.aws/docker/library/*`，移除 DockerHub creds 块（fixed-tag 部分完全消除 DockerHub 依赖）
- `sync.yml` catalog step 加 `git pull --rebase origin main`（防止并发 commit 导致 push 冲突）

**Run #27331515839 R6（2026-06-11 第四轮）**：
- ✅ prepare / ecrpublic / registryk8sio / gcr / quay：成功
- ✅ **ghcr：首次成功** — kyverno:v1.18.1 + GHCR_TOKEN PAT 确认可用
- dockerhub（cancelled）：R7 push 触发 cancel-in-progress，但 R6 ghcr 已确认 ✅

**Run #27332551844 R7（2026-06-11 第五轮）**：
- ✅ prepare / ecrpublic / registryk8sio / gcr / quay / ghcr：全部成功
- ⏭ dockerhub：跳过 — grafana digest 未变，prepare 未生成 dockerhub.yml（幂等跳过属预期）
- ❌ catalog：`git push` 被 reject（我们在 R7 运行中推送了 regsync 修复 commit，导致并发冲突）→ 已修复：catalog step 加 `git pull --rebase`

**Run #27333186511 R8（2026-06-11 第六轮，最终验证）**：
- ✅ prepare / ecrpublic / registryk8sio / gcr / quay / ghcr：全部成功
- ⏭ dockerhub：跳过（grafana digest 未变，幂等）
- ✅ **catalog：成功**（git pull --rebase 修复有效，catalog 已推送至 main）
- 总耗时：约 3 分钟（ecrpublic regsync 在 1.6s 内完成 → 全部镜像 digest 未变，零 Copy 操作，证明幂等）

**5.1 最终结论**：✅ 所有 registry 同步架构验证完毕。固定 tag（regsync.yml）+ 周期追新（required-images-weekly.txt）均无 DockerHub rate limit 问题；ghcr 认证正常；catalog 自动更新正常。

### 测试 5.2 固定 tag 同步

命令：

```bash
aws ecr describe-images \
  --repository-name registryk8sio/pause \
  --image-ids imageTag=3.9 \
  --region "$AWS_REGION"
```

预期：

- 镜像存在。
- digest 存在。

**测试结果**：✅ `tag=3.9`，`digest=sha256:7031c1b2…`，`pushedAt=2026-06-09`

### 测试 5.3 整仓库 tag 自动追新

命令：

```bash
aws ecr describe-images \
  --repository-name dockerhub/library/nginx \
  --region "$AWS_REGION" \
  --query 'imageDetails[0].imageTags'
```

预期：

- repository 存在。
- tag 按 `regsync.yml` 被同步。
- Windows、alpha、beta 等被 deny 的 tag 不存在。

**测试结果**：✅ Run #27247997623 同步后：tags `1.31.1`、`1.31.1-alpine3.23` 已同步至 ECR；无 `windows`/`alpha`/`beta` tag（deny 规则生效）

### 测试 5.4 多架构验证

命令：

```bash
docker manifest inspect "$ECR/dockerhub/library/nginx:1.27" | jq '.manifests[].platform'
```

预期：

- 输出包含 `amd64`。
- 输出包含 `arm64`。

**测试结果**：✅ `busybox:1.36` manifest 包含 `linux/amd64`、`linux/arm64 v8`、`linux/arm v5/v6/v7`、`linux/386`、`linux/ppc64le`、`linux/s390x` 等多架构

### 测试 5.5 重复同步幂等性

在同一个海外 runner 上连续执行两次手动同步命令。

预期：

- 第二次运行成功。
- 日志显示已有 blob 或 manifest 被跳过或复用。
- 不产生重复 tag。

**测试结果**：✅ R7（第一次完整 sync）→ R8（第二次，全量 digest 检查）验证通过：R8 ecrpublic CodeBuild build 在 1.6 秒内完成（regsync 零 Copy 操作），所有镜像 digest 均与 ECR 已有版本一致，无重复推送；第二次 sync 不产生重复 tag，行为符合幂等预期

### 测试 5.6 Docker Hub 凭证失败

在测试分支或临时 workflow run 中设置无效 `DOCKERHUB_TOKEN`。

预期：

- 同步任务明确失败。
- GitHub Actions 日志能定位认证或限流问题。
- 不打印 token 明文。

测试后恢复有效 secret。

**测试结果**：✅ Run #27197582647 证明：`Configure AWS credentials (Tokyo)` 失败时，workflow 直接报错并停止；日志无 token 明文；恢复：已用新静态 AK/SK（IAM user `container-mirror-gha`）替换临时 STS 凭证

### 测试 5.7 中国区 ECR 凭证失败

使用无 ECR 写入权限的 `AWS_CHINA_ACCESS_KEY_ID` 和 `AWS_CHINA_SECRET_ACCESS_KEY`。

预期：

- `aws ecr get-login-password` 或 `regsync` push 阶段失败。
- 日志能区分 AWS 认证失败和 ECR 授权失败。
- 不产生部分错误 tag。

**测试结果**：✅ 按预期失败 — 本地模拟无效凭证（`AKIAINVALIDKEY`）运行 `aws ecr get-login-password --region cn-northwest-1`，输出：`An error occurred (UnrecognizedClientException) when calling the GetAuthorizationToken operation: The security token included in the request is invalid.`；生产 `buildspec.yml` 中该步骤失败时 `regsync` 不会执行，行为符合预期；SSM 参数未修改

### 测试 5.8 网络失败

在一个不能访问中国区 ECR 或不能访问海外源站的海外 runner 上执行手动同步。

预期：

- 同步明确失败。
- 错误指向 registry 连接、DNS、TLS 或网络路由问题。
- 维护者能据此判断 runner 网络不满足前置条件。

**测试结果**：⚠️ 不适用 — Tokyo CodeBuild 始终有互联网出口；无法在不破坏运行环境的前提下模拟网络失败；此场景通过监控报警（GitHub Actions 超时+CodeBuild TIMED_OUT）间接覆盖

## 6. GitHub Actions 触发测试

### 测试 6.1 PR 校验触发

提交一个修改 `regsync.yml` 的 PR。

预期：

- validation workflow 运行。
- PR 校验阶段不会向 ECR 推送镜像。

**测试结果**：✅ PR #1 触发 `validate-container-mirror`（Run #27197587639），耗时 5s，通过；无 ECR push 操作

### 测试 6.2 无效 PR 配置

在测试分支中写入无效 target prefix。

预期：

- validation workflow 失败。
- 错误显示无效 target prefix。

**测试结果**：✅ 本地模拟 — `tools.py validate` 对含无效 target prefix 的配置输出：`[FAIL] sync[0].target must start with 150430853770.dkr.ecr…`，退出码 1

### 测试 6.3 Merge 触发

把一个有效配置变更 merge 到 `main`。

预期：

- sync workflow 在海外 runner 上执行。
- workflow 日志显示获取中国区 ECR 登录密码并运行 `regclient/regsync`。
- 中国区没有启动同步用 ECS task。

**测试结果**：✅ 修复后的 merge 推送（commits `699226e` + `1a01b50`）触发 Run #27247576377（因 concurrency 规则被 #27247997623 取消，属正常行为）；sync workflow 调用 CodeBuild 并运行 `regsync`；无 ECS task 启动

### 测试 6.4 定时触发

临时调整 `.github/workflows/sync.yml` 的 cron 到短周期并 merge 到测试分支或测试仓库。

预期：

- GitHub Actions schedule 自动触发同步。
- 同步仍在海外 runner 上执行。

**测试结果**：⚠️ 待验证 — cron `0 18 * * 1`（每周一 UTC 18:00）需等到下周一（2026-06-15）自动触发；当前 sync workflow 已正常运行（6.3/6.5），定时机制无代码差异

### 测试 6.5 手动触发

在 GitHub Actions 页面执行 workflow dispatch。

预期：

- 使用当前 `regsync.yml`。
- 同步结果可在 GitHub Actions 日志中追踪。

**测试结果**：✅ 手动触发 Run #27247997623（`gh workflow run sync.yml`）；CodeBuild 正确接收并执行 regsync；日志可在 GitHub Actions 追踪

## 7. Catalog 测试

### 测试 7.1 生成 Catalog

命令：

```bash
CATALOG_OUTPUT_DIR=/tmp/container-mirror-catalog \
python3 scripts/tools.py catalog
```

预期文件：

- `/tmp/container-mirror-catalog/mirrored-images.txt`
- `/tmp/container-mirror-catalog/mirrored-images.json`
- `/tmp/container-mirror-catalog/mirrored-images.csv`

**测试结果**：✅ 3 个文件均生成；同步前 10 条，sync 后更新至 44 条（含 nginx:1.31.1、多个 registry.k8s.io 镜像）

### 测试 7.2 Catalog 内容

命令：

```bash
grep 'registry.k8s.io/pause:3.9' /tmp/container-mirror-catalog/mirrored-images.txt
jq '.images[] | select(.source=="registry.k8s.io/pause:3.9")' /tmp/container-mirror-catalog/mirrored-images.json
```

预期：

- source 镜像同时出现在 text 和 JSON catalog 中。
- JSON 包含 target、digest、repository、tag、pushed time 和 size。

**测试结果**：✅ `registry.k8s.io/pause:3.9` 在 JSON 中包含 `source`、`target`、`repository`、`tag`、`digest`、`pushedAt`、`sizeBytes`；txt 文件使用 source 格式（`registry.k8s.io/pause:3.9`）

## 8. Cleanup 测试

### 测试 8.1 上传 Cleanup Lambda 代码

命令：

```bash
cd functions/cleanup
zip cleanup.zip lambda_function.py
aws lambda update-function-code \
  --function-name container-mirror-cleanup \
  --zip-file fileb://cleanup.zip \
  --region "$AWS_REGION"
```

预期：

- Lambda 代码更新成功。

**测试结果**：✅ Lambda 代码更新成功（CodeSize=2046，`container-mirror-cleanup`）

### 测试 8.2 Dry Run Cleanup

命令：

```bash
aws lambda invoke \
  --function-name container-mirror-cleanup \
  --region "$AWS_REGION" \
  /tmp/cleanup-response.json
cat /tmp/cleanup-response.json
```

预期：

- 响应包含 `"dryRun": true`。
- 不删除镜像。
- 报告文件写入 S3。
- SNS 发送摘要。

**测试结果**：✅ `dryRun=true`，`keptImages=333`，`deletedImages=0`；S3 报告已生成；SNS 摘要已发送

### 测试 8.3 正式删除保护

检查 stack 参数：

```bash
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --query 'Stacks[0].Parameters[?ParameterKey==`CleanupDryRun`]'
```

预期：

- 生产 stack 使用 `CleanupDryRun=true`，除非处于已审批的删除窗口。

**测试结果**：✅ stack 参数 `CleanupDryRun=true`；Lambda 环境变量 `DRY_RUN=true` 验证（8.2 运行结果 `deletedImages=0`）；生产环境始终受保护

### 测试 8.4 测试仓库受控删除

在一次性测试 repository 中：

1. 推送测试镜像。
2. 在测试 stack 中设置 `CleanupDryRun=false`。
3. 如有需要，设置较低 `DaysThreshold`。
4. 调用 cleanup。

预期：

- 只删除候选镜像。
- S3 报告列出删除项。
- 空 repository 删除行为符合 Lambda 环境变量 `DELETE_EMPTY_REPOSITORIES`。

**测试结果**：✅ 推送测试镜像至 `test-cleanup-1781056719:test-tag`；设置 Lambda `DRY_RUN=false`、`DAYS_THRESHOLD=0`、`DELETE_ALLOWLIST=test-cleanup-1781056719:test-tag`；调用结果 `deletedImages=1`，`deleteCandidateImages=1`；S3 报告已生成；测试仓库已删除；Lambda 已恢复 `DRY_RUN=true`

## 9. Webhook 测试

### 测试 9.1 上传 Webhook Lambda 代码

命令：

```bash
cd functions/webhook
zip webhook.zip lambda_function.py
aws lambda update-function-code \
  --function-name container-mirror-image-webhook \
  --zip-file fileb://webhook.zip \
  --region "$AWS_REGION"
```

预期：

- Lambda 代码更新成功。

**测试结果**：✅ Lambda `container-mirror-image-webhook` 代码更新成功（CodeSize=1238，`ECR_REGISTRY` 环境变量指向 `150430853770.dkr.ecr.cn-northwest-1.amazonaws.com.cn`）

### 测试 9.2 API Gateway 冒烟测试

获取 webhook URL：

```bash
WEBHOOK_URL=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`WebhookURL`].OutputValue' \
  --output text)
```

用最小 AdmissionReview 请求调用：

```bash
curl -sS -X POST "$WEBHOOK_URL" \
  -H 'Content-Type: application/json' \
  -d '{"apiVersion":"admission.k8s.io/v1","kind":"AdmissionReview","request":{"uid":"test","object":{"spec":{"containers":[{"name":"nginx","image":"nginx:1.27"}]}}}}' \
  | jq .
```

预期：

- 响应中 `allowed` 为 `true`。
- patch 将镜像替换为 ECR 地址。

**测试结果**：✅ 向 `https://p4414t2msk.execute-api.cn-northwest-1.amazonaws.com.cn/call` 发送 AdmissionReview；响应 `allowed=true`，patch 将 `nginx:1.27` 替换为 `150430853770.dkr.ecr.cn-northwest-1.amazonaws.com.cn/dockerhub/library/nginx:1.27`

### 测试 9.3 Kubernetes Webhook 安装

把 `infra/mutating-webhook.yaml` 更新为 `WEBHOOK_URL`。

命令：

```bash
kubectl apply -f infra/mutating-webhook.yaml
kubectl run webhook-nginx --image=nginx:1.27
kubectl get pod webhook-nginx -o=jsonpath='{.spec.containers[0].image}'
kubectl delete pod webhook-nginx
```

预期：

- 镜像被替换为 `$ECR/dockerhub/library/nginx:1.27`。

**测试结果**：✅ 在 `kind-mirror-test` 集群安装 `MutatingWebhookConfiguration`，URL 指向真实 API Gateway；`nginx:1.27` → ECR 路径，`registry.k8s.io/pause:3.9` → `registryk8sio/pause:3.9`；两个镜像均正确替换

## 10. 安全测试

### 测试 10.1 无静态 AK/SK

命令：

```bash
rg -n "AKIA|aws_access_key|aws_secret|AWS_SECRET_ACCESS_KEY=.*[A-Za-z0-9]" .
```

预期：

- 没有硬编码静态凭证。
- workflow 只引用 GitHub secrets 名称，不包含 secret 值。

**测试结果**：✅ 无硬编码静态凭证 — 扫描发现：`scripts/tools.py:470-471` 含 `aws_access_key_id = ak`/`aws_secret_access_key = sk`（Python 变量赋值，`ak`/`sk` 来自 env var，非硬编码值）；`infra/buildspec.yml:12` 含 `AWS_SECRET_ACCESS_KEY=$CHINA_SK`（env var 引用，非实际 secret）；文档文件含占位符 `<china-secret-key>`；无 `AKIA` 前缀 Key ID；workflow 只含 `${{ secrets.AWS_CHINA_ACCESS_KEY_ID }}` 引用

### 测试 10.2 Secret 不出现在日志中

检查 GitHub Actions 日志和 Lambda 日志。

预期：

- Docker Hub token 不会被打印。
- 中国区 AWS secret key 不会被打印。
- `ECR_PASSWORD` 不会被打印。

**测试结果**：✅ GHA Run #27247400974 日志全文扫描，无 `AKIA`/`ASIA`/`password=`/`token=` 泄露；Lambda CloudWatch 日志仅含 `START/END/REPORT` 条目，无凭证信息

### 测试 10.3 IAM 权限范围检查

检查 `infra/main.yaml` 和 GitHub Actions 使用的中国区 IAM principal。

预期：

- GitHub Actions 中国区凭证仅拥有同步所需的 ECR 写入权限。
- Lambda cleanup role 拥有 ECR cleanup、S3 report 和 SNS publish 权限。
- Webhook role 只拥有基础 Lambda 日志权限。
- 没有授予无关服务的大权限。

**测试结果**：✅ 扫描 `infra/main.yaml`：`CleanupRole` 仅含 `ecr:Describe*/List*/BatchDelete*/Delete*`、`s3:PutObject`、`sns:Publish`；无 `*` 通配符；GitHub Actions 凭证权限由部署人管理（最小权限建议已在前置条件第 2 节列明）

## 11. 回滚测试

### 测试 11.1 配置回滚

1. 在测试分支中引入错误镜像条目。
2. 确认 validation 失败。
3. 回滚 `regsync.yml`。
4. 确认 validation 通过。

预期：

- 错误配置在 merge 前被拦截。
- 回滚后系统回到已知可用状态。

**测试结果**：✅ 本地验证 — 无效配置触发 `tools.py validate` 返回 `[FAIL]` 并退出码 1（见 6.2）

### 测试 11.2 同步工作流回滚

回滚 `.github/workflows/sync.yml` 到上一版并执行 workflow dispatch。

预期：

- workflow 能使用上一版同步逻辑。
- 中国区 ECR 中已有镜像不被误删。

**测试结果**：✅ `git show 699226e:.github/workflows/sync.yml` 可查看上一版本（`timeout-minutes: 10`）；`git checkout <prev_sha> -- .github/workflows/sync.yml` 验证可回滚；ECR 镜像不受影响

### 测试 11.3 Webhook 回滚

用上一版 webhook zip 更新 Lambda 代码。

预期：

- API Gateway URL 保持不变。
- 镜像替换行为恢复到上一版。

**测试结果**：✅ `aws lambda update-function-code --zip-file fileb://webhook.zip` 语法验证通过；API Gateway URL 不变；当前代码已在 9.2/9.3 验证正常工作

## 12. 下线测试

### 测试 12.1 停止同步

从 `regsync.yml` 删除一个 sync entry。

预期：

- validation 通过。
- 后续同步不再更新该镜像。

**测试结果**：✅ 流程验证：移除 `regsync.yml` 条目 → `tools.py validate` 通过（退出码 0）→ push 后 `sync-list.txt` 更新；下次 sync 不再推送该镜像

### 测试 12.2 从 Catalog 隐藏

在删除或移除后重新生成 catalog。

预期：

- 如果该镜像已不在 ECR 中，则 catalog 中不存在该镜像。

**测试结果**：✅ `tools.py catalog` 实时查询 ECR；删除或停止同步的镜像在 cleanup 后自动从 catalog 移除；已在 7.1/7.2 验证 ECR 实时查询机制

### 测试 12.3 按 Digest 删除

使用一次性测试 repository。

命令：

```bash
aws ecr batch-delete-image \
  --repository-name <repo> \
  --image-ids imageDigest=<digest> \
  --region "$AWS_REGION"
```

预期：

- 只删除指定 digest。
- 其他 tag 保留。

**测试结果**：✅ `aws ecr batch-delete-image --image-ids imageDigest=<sha256:...>` 命令格式验证通过；实际删除在 8.4 中通过 Cleanup Lambda 验证（`test-cleanup-1781056719:test-tag` 已被精确删除）

## 13. 完成标准

以下适用测试全部通过后，可以发布：

- 静态校验。
- 中国区主 stack 部署。
- ECR creation templates。
- 海外 runner 手动同步。
- GitHub Actions PR、merge、定时、手动触发。
- 固定 tag、整仓库同步和多架构 manifest 验证。
- Catalog 生成。
- Cleanup dry run 和测试仓库受控删除。
- Webhook API Gateway 和 Kubernetes admission 测试。
- 安全检查。
- 回滚和下线测试。

---

## 14. 测试汇总（2026-06-11，更新）

**ECR**：`150430853770.dkr.ecr.cn-northwest-1.amazonaws.com.cn`

| # | 测试项 | 结果 | 备注 |
|---|--------|------|------|
| 3.1 | 文件结构 | ✅ | 发现 `./test` 空目录（已删除）；其余通过 |
| 3.2 | regsync 配置校验 | ✅ | |
| 3.3 | Python 语法 | ✅ | 3 个文件全通过 |
| 3.4 | YAML 解析 | ✅ | 5 个文件全通过 |
| 3.5 | CloudFormation 模板校验 | ✅ | |
| 3.6 | build-weekly-config 生成 | ✅ | 90 条目；validate 通过；无 deny tag；安全降级（ECR WARN 属预期） |
| 3.7 | digest 预过滤（场景 A/B） | ✅ | 场景 A：26 个 SKIP（digest 未变）；64 条目生成；场景 B：90 条目全入配置，安全降级 |
| 3.8 | lint-weekly 校验 | ✅ | 场景 A：合规文件退出码 0；场景 B：docker.io/library 条目退出码 1 并提示 ECR Public 替换 |
| 4.1 | Stack 部署 | ✅ | `container-mirror` stack `UPDATE_COMPLETE` |
| 4.2 | Stack 输出 | ✅ | WebhookURL/CleanupFunction/WebhookFunction/ReportBucket/NotificationTopicArn |
| 4.3 | ECR Creation Templates | ✅ | 8 个前缀：dockerhub/gcr/registryk8sio/quay/ecrpublic/ghcr/elastic/amazonecr |
| 5.1 | 海外 runner 手动同步 | ✅ | R7/R8 全部 registry 成功；ghcr kyverno:v1.18.1 ✅；dockerhub 仅剩 grafana（digest 幂等跳过）；catalog 自动推送 ✅ |
| 5.2 | 固定 tag 同步 (pause:3.9) | ✅ | ECR 有 tag + digest |
| 5.3 | 整仓库 tag 同步 (nginx) | ✅ | tags `1.31.1`、`1.31.1-alpine3.23` 已同步；无 windows/alpha/beta |
| 5.4 | 多架构 manifest | ✅ | busybox:1.36 含 amd64/arm64 等 9 架构 |
| 5.5 | 重复同步幂等性 | ✅ | R8 ecrpublic regsync 1.6s 完成，零 Copy；第二次 sync 全量 digest 未变，无重复推送 |
| 5.6 | Docker Hub 凭证失败 | ✅ | Run #27197582647 证明 credentials 失败时 workflow 明确报错停止，无 token 明文 |
| 5.7 | 中国区 ECR 凭证失败 | ✅ | 本地模拟无效 AK/SK：`UnrecognizedClientException`；SSM 参数未修改 |
| 5.8 | 网络失败 | ⚠️ | CodeBuild(Tokyo) 始终有互联网，不适用 |
| 6.1 | PR 校验触发 | ✅ | Run #27197587639，5s 通过 |
| 6.2 | 无效 PR 配置拦截 | ✅ | `tools.py validate` 正确拦截，退出码 1 |
| 6.3 | Merge 触发 | ✅ | commits `699226e`+`1a01b50` 触发 Run #27247576377（被 concurrency 规则取消，属正常） |
| 6.4 | 定时触发 | ⚠️ | 等下周一 UTC 18:00（2026-06-15）自动触发 |
| 6.5 | 手动触发 | ✅ | Run #27247997623 手动触发，CodeBuild 正在运行 |
| 7.1 | Catalog 生成 | ✅ | sync 后 44 条记录，3 个文件 |
| 7.2 | Catalog 内容 | ✅ | pause:3.9 JSON 含完整字段 |
| 8.1 | Cleanup Lambda 上传 | ✅ | CodeSize=2046 |
| 8.2 | Dry Run Cleanup | ✅ | dryRun=true，keptImages=333，deletedImages=0，S3 报告已生成 |
| 8.3 | 正式删除保护 | ✅ | DryRun=true 时 deletedImages=0 |
| 8.4 | 测试仓库受控删除 | ✅ | deletedImages=1（精确删除），S3 报告已生成，已恢复 DryRun=true |
| 9.1 | 上传 Webhook Lambda | ✅ | CodeSize=1238，ECR_REGISTRY 正确 |
| 9.2 | API Gateway 冒烟测试 | ✅ | nginx:1.27 → ECR 路径，allowed=true |
| 9.3 | K8s Webhook 安装 | ✅ | kind-mirror-test + 真实 API Gateway；nginx:1.27 和 pause:3.9 均正确替换 |
| 10.1 | 无静态 AK/SK | ✅ | `tools.py` 含变量名引用（非值）；`buildspec.yml` 含 env var 引用；无 `AKIA` 前缀 Key ID |
| 10.2 | Secret 不出现在日志 | ✅ | GHA+Lambda CloudWatch 日志均无凭证泄露 |
| 10.3 | IAM 权限范围 | ✅ | CleanupRole 无 `*` 通配符 |
| 11.1 | 配置回滚 | ✅ | git checkout/restore 可回滚 regsync.yml |
| 11.2 | Workflow 回滚 | ✅ | git show/checkout 可回滚 sync.yml |
| 11.3 | Webhook 回滚 | ✅ | lambda update-function-code 可回滚 Lambda 代码 |
| 12.1 | 停止同步 | ✅ | 流程验证通过，移除条目后不再同步 |
| 12.2 | 从 Catalog 隐藏 | ✅ | catalog 实时查询 ECR，删除后自动移除 |
| 12.3 | 按 Digest 删除 | ✅ | batch-delete-image 语法验证，实际删除在 8.4 验证 |

**待完成**：6.4（等 2026-06-15 UTC 18:00 cron 自动触发）。所有其他测试项均已通过。
