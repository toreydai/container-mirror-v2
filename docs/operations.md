# 运维

## 自部署

1. Fork 或 clone 仓库。
2. 编辑 `regsync.yml` 中的目标 registry（`TARGET_REGISTRY`）。
3. 配置 GitHub Actions variables：

   ```text
   ECR_REGISTRY
   ```

4. 配置 GitHub Actions secrets：

   ```text
   DOCKERHUB_USER
   DOCKERHUB_TOKEN
   AWS_CHINA_ACCESS_KEY_ID
   AWS_CHINA_SECRET_ACCESS_KEY
   AWS_GLOBAL_ACCESS_KEY_ID
   AWS_GLOBAL_SECRET_ACCESS_KEY
   GHCR_TOKEN
   ```

   - `AWS_CHINA_*`：中国区 IAM 用户，需要 ECR 写入权限（见测试手册前置条件）。
   - `AWS_GLOBAL_*`：Tokyo 区 IAM 用户，需要 CodeBuild 触发权限。
   - `DOCKERHUB_*`：用于拉取 `docker.io/grafana/` 等 ECR Public 不覆盖的镜像。
   - `GHCR_TOKEN`：GitHub Personal Access Token，需要 `read:packages` scope，用于拉取第三方 GHCR 镜像（如 `ghcr.io/kyverno/kyverno`）。注意：`GITHUB_TOKEN` 仅有当前 repo 的包读取权限，无法拉取其他 org/user 的 GHCR 镜像，必须使用 PAT。

5. 部署中国区主 stack：

   ```bash
   aws cloudformation deploy \
     --stack-name container-mirror \
     --template-file infra/main.yaml \
     --parameter-overrides \
       NotificationEmail=you@example.com \
       EcrRegistry=<account>.dkr.ecr.cn-northwest-1.amazonaws.com.cn \
       CleanupScheduleState=DISABLED \
       MirrorImageCountLimit=50 \
     --capabilities CAPABILITY_IAM \
     --region cn-northwest-1
   ```

   `MirrorImageCountLimit`（默认 50）：每个 mirror repository 保留的最大镜像数，超出后由 ECR lifecycle policy 自动清除旧 tag，防止无上限积累。

6. 上传 cleanup 和 webhook Lambda 代码：

   ```bash
   cd functions/cleanup && zip cleanup.zip lambda_function.py
   aws lambda update-function-code \
     --function-name container-mirror-cleanup \
     --zip-file fileb://cleanup.zip --region cn-northwest-1

   cd ../webhook && zip webhook.zip lambda_function.py
   aws lambda update-function-code \
     --function-name container-mirror-image-webhook \
     --zip-file fileb://webhook.zip --region cn-northwest-1
   ```

7. 通过 GitHub workflow dispatch 运行一次手动同步，验证端到端流程。
8. dry-run cleanup 审查通过后，把 `CleanupScheduleState` 改为 `ENABLED`。

## 手动同步

通过 GitHub Actions 页面执行 workflow dispatch，或用 CLI 触发：

```bash
gh workflow run sync.yml
```

同步在 Tokyo CodeBuild 中执行，日志可在 GitHub Actions 页面追踪。不支持直接在本地运行完整同步（需要 CodeBuild 环境变量和 S3 配置）；如需本地调试单个镜像，可手动运行：

```bash
export ECR_PASSWORD="$(aws ecr get-login-password --region cn-northwest-1)"
docker run --rm \
  -v "$PWD/regsync.yml:/regsync.yml:ro" \
  -e DOCKERHUB_USER \
  -e DOCKERHUB_TOKEN \
  -e ECR_PASSWORD \
  regclient/regsync:latest once -c /regsync.yml
```

## 管理 required-images-weekly.txt

每周自动追版的镜像清单。规则：

- 只写镜像名，不带 tag。
- Docker Hub 官方库镜像必须使用 `public.ecr.aws/docker/library/<name>`，不能写 `docker.io/library/<name>`（validate workflow 会拦截）。
- 非 library 的 Docker Hub 镜像（如 `docker.io/grafana/grafana`）正常写 docker.io 源。
- 删除一行即停止追版；已同步到 ECR 的镜像不会被自动清理，需要通过 cleanup 流程处理。

## 回滚

把 `regsync.yml` 回滚到已知可用的 commit，然后触发手动同步：

```bash
git checkout <good-commit> -- regsync.yml
git commit -m "revert: rollback regsync.yml to <good-commit>"
git push
```

## 主动下线

1. 从 `regsync.yml` 或 `required-images-weekly.txt` 删除对应条目并 merge。
2. 以 dry-run 模式运行 cleanup，审查报告。
3. 确认后把待删除项加入 `CleanupDeleteAllowlist`，再关闭 dry-run：

   ```bash
   aws cloudformation deploy \
     --stack-name container-mirror \
     --template-file infra/main.yaml \
     --parameter-overrides \
       NotificationEmail=you@example.com \
       CleanupDryRun=false \
       CleanupDeleteAllowlist=registryk8sio/pause:3.9 \
     --capabilities CAPABILITY_IAM \
     --region cn-northwest-1
   ```

   allowlist 支持 `repository`、`repository:tag`、`repository@digest`。

4. 操作完成后将 `CleanupDryRun` 恢复为 `true`。记录操作人、原因、repository、tag/digest 和时间戳。

## 清理和受控删除

清理有两层：

- **ECR lifecycle policy**：处理简单的按数量/时间归档规则。
- **Lambda cleanup**：负责报告、dry run、显式 allowlist 删除和空 repository 清理。

调用 cleanup Lambda（dry run）：

```bash
aws lambda invoke \
  --function-name container-mirror-cleanup \
  --region cn-northwest-1 \
  /tmp/cleanup-response.json
cat /tmp/cleanup-response.json
```

更新 stack 参数：

```bash
aws cloudformation deploy \
  --stack-name container-mirror \
  --template-file infra/main.yaml \
  --parameter-overrides \
    NotificationEmail=you@example.com \
    CleanupDryRun=true \
  --capabilities CAPABILITY_IAM \
  --region cn-northwest-1
```

在报告完成审查前保持 `CleanupDryRun=true`。
