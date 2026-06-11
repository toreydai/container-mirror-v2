# 维护者测试手册

本文用于从维护者视角验证 container-mirror 2.0 的完整能力。新方案的关键前提是：中国区机器不访问海外公共镜像站，镜像同步必须在可访问海外 registry 且可写入中国区 ECR 的海外 runner 上执行；中国区只承载 ECR、清理、报告、通知和可选 webhook。

## 1. 测试范围

覆盖场景：

- 本地仓库静态校验
- CloudFormation 模板校验和中国区控制面部署
- ECR Repository Creation Template 行为
- 海外 runner 手动同步
- GitHub Actions PR 校验、merge 触发、定时触发、手动触发
- `regsync` 固定 tag、整仓库 tag 追新和多架构 manifest 同步
- 海外源 registry 失败、中国区 ECR 凭证失败、Docker Hub 凭证失败
- Catalog 生成
- Cleanup dry run 和受控删除
- Webhook Lambda 和 Kubernetes webhook 行为
- 安全、secret 处理、回滚和主动下线

## 2. 前置条件

本地维护环境工具：`aws` / `docker` / `python3` / `kubectl` / `jq` / `zip` / `rg`

海外 runner 要求：

- 能访问 Docker Hub、GCR、registry.k8s.io、Quay、GHCR、ECR Public 等海外源站
- 能访问 `*.amazonaws.com.cn` 并写入目标中国区 ECR
- 能运行 Docker
- 不在中国区 VPC、ECS、Fargate、EC2 或其他无法稳定访问海外源站的机器上执行同步

环境变量：

```bash
export AWS_REGION=cn-northwest-1
export ECR=<account-id>.dkr.ecr.cn-northwest-1.amazonaws.com.cn
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
GHCR_TOKEN
```

中国区 AWS 凭证最小权限：`ecr:GetAuthorizationToken` / `ecr:CreateRepository` / `ecr:DescribeRepositories` / `ecr:DescribeImages` / `ecr:BatchCheckLayerAvailability` / `ecr:InitiateLayerUpload` / `ecr:UploadLayerPart` / `ecr:CompleteLayerUpload` / `ecr:PutImage` / `ecr:BatchGetImage`

## 3. 静态测试

### 3.1 文件结构

```bash
find . -maxdepth 4 -type f -not -path './.git/*' | sort
find . -type d -empty -not -path './.git/*' -print
```

预期：目录结构含 `scripts/`、`infra/`、`functions/`、`docs/`；无空目录；`infra/main.yaml` 是唯一 CloudFormation 模板；不存在同步运行镜像 Dockerfile。

### 3.2 regsync 配置校验

```bash
ECR_REGISTRY=$ECR python3 scripts/tools.py validate regsync.yml
```

预期：`[OK] regsync.yml passed container-mirror validation`

### 3.3 Python 语法

```bash
python3 -m py_compile \
  scripts/tools.py \
  functions/cleanup/lambda_function.py \
  functions/webhook/lambda_function.py
```

预期：退出码为 0。

### 3.4 YAML 解析

```bash
python3 - <<'PY'
import pathlib, yaml
class Loader(yaml.SafeLoader): pass
def cfn(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode): return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode): return loader.construct_sequence(node)
    return loader.construct_mapping(node)
Loader.add_multi_constructor('!', cfn)
for suffix in ('*.yml', '*.yaml'):
    for path in sorted(pathlib.Path('.').rglob(suffix)):
        if '.git' in path.parts: continue
        with open(path, encoding='utf-8') as f: yaml.load(f, Loader=Loader)
        print('[OK]', path)
PY
```

预期：所有 YAML 文件成功解析。

### 3.5 CloudFormation 校验

```bash
aws cloudformation validate-template \
  --template-body file://infra/main.yaml \
  --region "$AWS_REGION"
```

预期：模板校验通过，输出参数列表含 `MirrorImageCountLimit`。

### 3.6 build-weekly-config 生成

```bash
pip install pyyaml -q
ECR_REGISTRY=$ECR python3 scripts/tools.py build-weekly-config required-images-weekly.txt /tmp/regsync-weekly.yml
ECR_REGISTRY=$ECR python3 scripts/tools.py validate /tmp/regsync-weekly.yml
```

预期：

- 退出码均为 0，`/tmp/regsync-weekly.yml` 已生成
- validate 输出 `[OK]`
- 生成条目不含 `windowsservercore`、`alpha`、`beta`、`rc` 等 deny tag
- 每个镜像最多出现 2 个 minor 版本（plain + alpine 变体）
- `public.ecr.aws/docker/library/*` 来源的 target 路径为 `dockerhub/library/<name>`

### 3.7 digest 预过滤行为

**场景 A：有效 China ECR 凭证**

```bash
export CHINA_AK=<china-ak>
export CHINA_SK=<china-sk>
ECR_REGISTRY=$ECR python3 scripts/tools.py build-weekly-config \
  required-images-weekly.txt /tmp/regsync-weekly-filtered.yml 2>&1 \
  | grep -E '\[SKIP\]|\[OK\].*条目'
```

预期：digest 未变的镜像输出 `[SKIP]`；`[OK]` 行显示跳过数量 M > 0（ECR 有已同步镜像时）；validate 通过。

**场景 B：无 ECR 凭证（安全降级）**

```bash
unset CHINA_AK CHINA_SK
ECR_REGISTRY=$ECR python3 scripts/tools.py build-weekly-config \
  required-images-weekly.txt /tmp/regsync-weekly-nofilter.yml 2>&1 \
  | grep -E '\[SKIP\]|\[WARN\].*ECR|\[OK\].*条目'
```

预期：不出现任何 `[SKIP]`；可能出现 `[WARN] ECR check error`；退出码仍为 0。

### 3.8 lint-weekly 校验

**场景 A：合规文件**

```bash
python3 scripts/tools.py lint-weekly required-images-weekly.txt
```

预期：`[OK] required-images-weekly.txt 无 docker.io/library 条目`

**场景 B：含违规条目**

```bash
echo "docker.io/library/nginx" > /tmp/bad-weekly.txt
python3 scripts/tools.py lint-weekly /tmp/bad-weekly.txt
```

预期：退出码为 1；输出 `[FAIL]` 并提示改用 `public.ecr.aws/docker/library/nginx`。

## 4. 中国区控制面部署测试

### 4.1 部署主 Stack

```bash
aws cloudformation deploy \
  --stack-name "$STACK_NAME" \
  --template-file infra/main.yaml \
  --parameter-overrides \
    NotificationEmail="$NOTIFICATION_EMAIL" \
    EcrRegistry="$ECR" \
    CleanupDryRun=true \
    MirrorImageCountLimit=50 \
  --capabilities CAPABILITY_IAM \
  --region "$AWS_REGION"
```

预期：Stack 进入 `CREATE_COMPLETE` 或 `UPDATE_COMPLETE`；无 ECS、Fargate、EventBridge Scheduler、DynamoDB 等同步运行时资源。

### 4.2 Stack 输出

```bash
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs'
```

预期输出含：`NotificationTopicArn` / `ReportBucket` / `CleanupFunction` / `WebhookFunction` / `WebhookURL`

### 4.3 ECR Repository Creation Templates

```bash
aws ecr describe-repository-creation-templates \
  --region "$AWS_REGION" \
  --query 'repositoryCreationTemplates[].prefix'
```

预期：8 个前缀全部存在：`dockerhub/` / `gcr/` / `registryk8sio/` / `quay/` / `ecrpublic/` / `ghcr/` / `elastic/` / `amazonecr/`；每个 template 含 lifecycle policy（`MirrorImageCountLimit=50`）。

## 5. 海外 runner 同步测试

### 5.1 手动同步

必须在海外 runner 执行：

```bash
gh workflow run sync.yml
```

或本地单镜像调试：

```bash
export ECR_PASSWORD="$(aws ecr get-login-password --region "$AWS_REGION")"
sed "s|\${ECR_REGISTRY}|$ECR|g" regsync.yml > /tmp/regsync-subst.yml
docker run --rm \
  -v "/tmp/regsync-subst.yml:/regsync.yml:ro" \
  -e DOCKERHUB_USER \
  -e DOCKERHUB_TOKEN \
  -e ECR_PASSWORD \
  regclient/regsync:latest once -c /regsync.yml
```

预期：

- 各 registry CodeBuild 成功（6 个并行批次）
- 日志显示从海外源站拉取并推送到中国区 ECR
- 第二次运行 ecrpublic 批次在数秒内完成（digest 未变，零 Copy）

### 5.2 固定 tag 同步

```bash
aws ecr describe-images \
  --repository-name registryk8sio/pause \
  --image-ids imageTag=3.9 \
  --region "$AWS_REGION"
```

预期：镜像存在，digest 存在。

### 5.3 整仓库 tag 自动追新

```bash
aws ecr describe-images \
  --repository-name dockerhub/library/nginx \
  --region "$AWS_REGION" \
  --query 'imageDetails[].imageTags'
```

预期：tag 按 `regsync.yml` 被同步；无 `windowsservercore`、`alpha`、`beta` 等 deny tag。

### 5.4 多架构验证

```bash
docker manifest inspect "$ECR/dockerhub/library/nginx:1.27" | jq '.manifests[].platform'
```

预期：输出包含 `amd64` 和 `arm64`。

### 5.5 重复同步幂等性

连续执行两次 `gh workflow run sync.yml`。

预期：第二次 ecrpublic 批次在数秒内完成；日志零 Copy 操作；不产生重复 tag。

### 5.6 Docker Hub 凭证失败

在测试分支中设置无效 `DOCKERHUB_TOKEN`，触发同步。

预期：同步明确失败；日志能定位认证问题；不打印 token 明文。测试后恢复有效 secret。

### 5.7 中国区 ECR 凭证失败

使用无 ECR 写入权限的凭证。

预期：`aws ecr get-login-password` 或 regsync push 阶段明确失败；日志能区分 AWS 认证失败和 ECR 授权失败；不产生部分错误 tag。

### 5.8 网络失败

在不能访问中国区 ECR 或不能访问海外源站的 runner 上执行手动同步。

预期：同步明确失败；错误指向 registry 连接、DNS、TLS 或网络路由问题。

## 6. GitHub Actions 触发测试

### 6.1 PR 校验触发

提交一个修改 `regsync.yml` 的 PR。

预期：validate workflow 运行；PR 校验阶段不向 ECR 推送镜像。

### 6.2 无效 PR 配置

在测试分支中写入无效 target prefix，提交 PR。

预期：validate workflow 失败；错误显示无效 target prefix。

### 6.3 Merge 触发

将有效配置变更 merge 到 `main`。

预期：sync workflow 在海外 runner 上执行；日志显示获取中国区 ECR 登录密码并运行 `regclient/regsync`；无 ECS task 启动。

### 6.4 定时触发

确认 `.github/workflows/sync.yml` 的 cron 配置（每周一 UTC 18:00）。

预期：GitHub Actions schedule 自动触发同步；同步在海外 runner 上执行。

### 6.5 手动触发

在 GitHub Actions 页面执行 workflow dispatch。

预期：使用当前 `regsync.yml`；同步结果可在 GitHub Actions 日志中追踪。

## 7. Catalog 测试

### 7.1 生成 Catalog

```bash
CATALOG_OUTPUT_DIR=/tmp/container-mirror-catalog \
ECR_REGISTRY=$ECR python3 scripts/tools.py catalog
```

预期文件：

- `/tmp/container-mirror-catalog/mirrored-images.txt`
- `/tmp/container-mirror-catalog/mirrored-images.json`
- `/tmp/container-mirror-catalog/mirrored-images.csv`

### 7.2 Catalog 内容

```bash
grep 'registry.k8s.io/pause:3.9' /tmp/container-mirror-catalog/mirrored-images.txt
jq '.images[] | select(.source=="registry.k8s.io/pause:3.9")' \
  /tmp/container-mirror-catalog/mirrored-images.json
```

预期：source 镜像同时出现在 txt 和 JSON 中；JSON 包含 target、digest、repository、tag、pushedAt、sizeBytes。

## 8. Cleanup 测试

### 8.1 上传 Cleanup Lambda 代码

```bash
cd functions/cleanup
zip cleanup.zip lambda_function.py
aws lambda update-function-code \
  --function-name container-mirror-cleanup \
  --zip-file fileb://cleanup.zip \
  --region "$AWS_REGION"
```

预期：Lambda 代码更新成功。

### 8.2 Dry Run Cleanup

```bash
aws lambda invoke \
  --function-name container-mirror-cleanup \
  --region "$AWS_REGION" \
  /tmp/cleanup-response.json
cat /tmp/cleanup-response.json
```

预期：响应包含 `"dryRun": true`；不删除镜像；报告写入 S3；SNS 发送摘要。

### 8.3 正式删除保护

```bash
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --query 'Stacks[0].Parameters[?ParameterKey==`CleanupDryRun`]'
```

预期：生产 stack 使用 `CleanupDryRun=true`，除非处于已审批的删除窗口。

### 8.4 测试仓库受控删除

1. 推送测试镜像至一次性 repository
2. 在测试 stack 中设置 `CleanupDryRun=false`，适当调低 `DaysThreshold`
3. 调用 cleanup Lambda
4. 验证后恢复 `CleanupDryRun=true`

预期：只删除候选镜像；S3 报告列出删除项；空 repository 删除行为符合 `DELETE_EMPTY_REPOSITORIES` 设置。

## 9. Webhook 测试

### 9.1 上传 Webhook Lambda 代码

```bash
cd functions/webhook
zip webhook.zip lambda_function.py
aws lambda update-function-code \
  --function-name container-mirror-image-webhook \
  --zip-file fileb://webhook.zip \
  --region "$AWS_REGION"
```

预期：Lambda 代码更新成功。

### 9.2 API Gateway 冒烟测试

```bash
WEBHOOK_URL=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`WebhookURL`].OutputValue' \
  --output text)

curl -sS -X POST "$WEBHOOK_URL" \
  -H 'Content-Type: application/json' \
  -d '{"apiVersion":"admission.k8s.io/v1","kind":"AdmissionReview","request":{"uid":"test","object":{"spec":{"containers":[{"name":"nginx","image":"nginx:1.27"}]}}}}' \
  | jq .
```

预期：响应 `allowed=true`；patch 将 `nginx:1.27` 替换为 ECR 路径。

### 9.3 Kubernetes Webhook 安装

```bash
kubectl apply -f infra/mutating-webhook.yaml
kubectl run webhook-nginx --image=nginx:1.27
kubectl get pod webhook-nginx -o=jsonpath='{.spec.containers[0].image}'
kubectl delete pod webhook-nginx
```

预期：镜像被替换为 `$ECR/dockerhub/library/nginx:1.27`。

## 10. 安全测试

### 10.1 无静态 AK/SK

```bash
rg -n "AKIA|aws_access_key|aws_secret|AWS_SECRET_ACCESS_KEY=.*[A-Za-z0-9]" .
```

预期：无硬编码静态凭证；workflow 只引用 GitHub secrets 名称，不包含 secret 值。

### 10.2 Secret 不出现在日志中

检查 GitHub Actions 日志和 Lambda 日志。

预期：DockerHub token、AWS secret key、`ECR_PASSWORD` 均不被打印。

### 10.3 IAM 权限范围检查

检查 `infra/main.yaml` 中 Lambda role 的权限定义。

预期：cleanup role 仅含 ECR cleanup、S3 report、SNS publish 权限；webhook role 只含基础 Lambda 日志权限；无 `*` 通配符大权限。

## 11. 回滚测试

### 11.1 配置回滚

1. 在测试分支中引入错误镜像条目
2. 确认 validate workflow 失败
3. 回滚 `regsync.yml`
4. 确认 validate 通过

预期：错误配置在 merge 前被拦截；回滚后系统回到已知可用状态。

### 11.2 同步工作流回滚

```bash
git checkout <prev-commit> -- .github/workflows/sync.yml
git commit -m "revert: rollback sync.yml"
git push
```

预期：workflow 使用上一版同步逻辑；中国区 ECR 中已有镜像不被误删。

### 11.3 Webhook 回滚

用上一版 webhook zip 更新 Lambda 代码。

预期：API Gateway URL 保持不变；镜像替换行为恢复到上一版。

## 12. 下线测试

### 12.1 停止同步

从 `regsync.yml` 删除一个 sync entry，merge 后验证。

预期：validate 通过；后续同步不再更新该镜像。

### 12.2 从 Catalog 隐藏

删除或移除条目后重新生成 catalog。

预期：如果该镜像已不在 ECR 中，则 catalog 中不存在该镜像。

### 12.3 按 Digest 删除

```bash
aws ecr batch-delete-image \
  --repository-name <repo> \
  --image-ids imageDigest=<digest> \
  --region "$AWS_REGION"
```

预期：只删除指定 digest；其他 tag 保留。

## 13. 完成标准

以下所有项通过后可以发布：

- [ ] 静态校验（3.1–3.8）
- [ ] 中国区主 stack 部署（4.1–4.3）
- [ ] 海外 runner 手动同步（5.1–5.5）
- [ ] 凭证失败和网络失败（5.6–5.8）
- [ ] GitHub Actions 各触发方式（6.1–6.5）
- [ ] Catalog 生成（7.1–7.2）
- [ ] Cleanup dry run 和受控删除（8.1–8.4）
- [ ] Webhook API Gateway 和 K8s admission（9.1–9.3）
- [ ] 安全检查（10.1–10.3）
- [ ] 回滚和下线（11.1–12.3）
