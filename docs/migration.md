# 部署手册：在现有账号全新安装 v2

本手册适用于在账号 `048912060910`（cn-northwest-1）全新部署 container-mirror v2。v2 作为独立系统运行，与账号内现有的同步机制完全无关，不需要任何迁移或切换操作。

---

## 1. 整体思路

v2 部署完成后，账号内同时存在两套系统：

- **旧同步**：继续运行，管理现有 172 个 ECR 仓库
- **v2**：独立运行，新建的 ECR 仓库自动带 lifecycle policy，只同步显式声明的镜像

两套系统共享同一个 ECR registry，路径前缀相同（`dockerhub/library/nginx:1.27` 等），但仓库创建和 tag 管理完全独立。待 v2 稳定后，再决定是否停用旧同步并清理存量仓库，与 v2 部署解耦。

---

## 2. 前置条件

### 2.1 中国区 IAM 用户（048912060910，cn-northwest-1）

创建一个 IAM 用户，附加以下权限：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ecr:GetAuthorizationToken",
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
        "ecr:PutImage",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:DescribeImages",
        "ecr:DescribeRepositories"
      ],
      "Resource": "*"
    }
  ]
}
```

保存 Access Key ID 和 Secret Access Key。

### 2.2 全球区 IAM 用户（ap-northeast-1，Tokyo）

创建一个 IAM 用户，附加以下权限：

```json
{
  "Effect": "Allow",
  "Action": [
    "codebuild:StartBuild",
    "codebuild:BatchGetBuilds",
    "s3:PutObject",
    "s3:GetObject",
    "s3:DeleteObject",
    "s3:ListBucket"
  ],
  "Resource": "*"
}
```

### 2.3 ECR RepositoryCreationTemplate 冲突检查

旧账号可能已有 Creation Template。部署前检查：

```bash
aws ecr describe-registry --region cn-northwest-1 --profile <048912060910 profile>
```

若 `repositoryCreationTemplates` 中已有 `dockerhub/`、`gcr/`、`ghcr/` 等 prefix，需先删除：

```bash
for prefix in dockerhub/ gcr/ ghcr/ quay/ registryk8sio/ ecrpublic/ elastic/ amazonecr/; do
  aws ecr delete-repository-creation-template \
    --prefix "$prefix" \
    --region cn-northwest-1 --profile <048912060910 profile> 2>/dev/null && echo "deleted: $prefix"
done
```

### 2.4 Token 准备

- **DockerHub PAT**：`read` 权限，用于拉取 `docker.io/grafana/` 等非 library 镜像
- **GitHub PAT**：`read:packages` scope，用于拉取第三方 GHCR 镜像

---

## 3. 部署基础设施

### 3.1 中国区主 Stack

```bash
aws cloudformation deploy \
  --stack-name container-mirror \
  --template-file infra/main.yaml \
  --parameter-overrides \
    NotificationEmail=<your-email> \
    EcrRegistry=048912060910.dkr.ecr.cn-northwest-1.amazonaws.com.cn \
    CleanupScheduleState=DISABLED \
    CleanupDryRun=true \
    MirrorImageCountLimit=50 \
  --capabilities CAPABILITY_IAM \
  --region cn-northwest-1 \
  --profile <048912060910 profile>
```

创建内容：8 个 ECR Repository Creation Template（含 lifecycle policy）、cleanup Lambda、webhook Lambda + API Gateway、SNS Topic、S3 报告桶。

### 3.2 上传 Lambda 代码

```bash
cd functions/cleanup
zip cleanup.zip lambda_function.py
aws lambda update-function-code \
  --function-name container-mirror-cleanup \
  --zip-file fileb://cleanup.zip \
  --region cn-northwest-1 --profile <048912060910 profile>

cd ../webhook
zip webhook.zip lambda_function.py
aws lambda update-function-code \
  --function-name container-mirror-image-webhook \
  --zip-file fileb://webhook.zip \
  --region cn-northwest-1 --profile <048912060910 profile>
```

### 3.3 Tokyo CodeBuild Stack

```bash
aws cloudformation deploy \
  --stack-name container-mirror-codebuild \
  --template-file infra/codebuild.yaml \
  --parameter-overrides \
    GitHubRepo=https://github.com/<your-org>/container-mirror-v2 \
    EcrRegistry=048912060910.dkr.ecr.cn-northwest-1.amazonaws.com.cn \
  --capabilities CAPABILITY_IAM \
  --region ap-northeast-1 \
  --profile <全球区 profile>
```

### 3.4 SSM 参数（Tokyo，全球区）

```bash
aws ssm put-parameter --name /container-mirror/china-ak \
  --value <中国区 AK> --type SecureString \
  --region ap-northeast-1 --profile <全球区 profile>

aws ssm put-parameter --name /container-mirror/china-sk \
  --value <中国区 SK> --type SecureString \
  --region ap-northeast-1 --profile <全球区 profile>

aws ssm put-parameter --name /container-mirror/dockerhub-user \
  --value <DockerHub 用户名> --type SecureString \
  --region ap-northeast-1 --profile <全球区 profile>

aws ssm put-parameter --name /container-mirror/dockerhub-token \
  --value <DockerHub PAT> --type SecureString \
  --region ap-northeast-1 --profile <全球区 profile>
```

---

## 4. 配置 GitHub

**Secrets**：

| 名称 | 值 |
|---|---|
| `AWS_CHINA_ACCESS_KEY_ID` | 中国区 IAM 用户 AK |
| `AWS_CHINA_SECRET_ACCESS_KEY` | 中国区 IAM 用户 SK |
| `AWS_GLOBAL_ACCESS_KEY_ID` | Tokyo IAM 用户 AK |
| `AWS_GLOBAL_SECRET_ACCESS_KEY` | Tokyo IAM 用户 SK |
| `DOCKERHUB_USER` | DockerHub 用户名 |
| `DOCKERHUB_TOKEN` | DockerHub PAT |
| `GHCR_TOKEN` | GitHub PAT（read:packages） |

**Variables**：

| 名称 | 值 |
|---|---|
| `ECR_REGISTRY` | `048912060910.dkr.ecr.cn-northwest-1.amazonaws.com.cn` |

---

## 5. 声明同步镜像

v2 从零开始，只同步显式声明的镜像。按实际需求填写，不需要照搬旧配置。

**固定 tag**（`regsync.yml`）：

```yaml
- source: public.ecr.aws/docker/library/nginx:1.27
  target: ${ECR_REGISTRY}/dockerhub/library/nginx:1.27
  type: image
```

**自动追版**（`required-images-weekly.txt`）：

```
public.ecr.aws/docker/library/nginx
quay.io/prometheus/prometheus
```

提 PR 后 validate workflow 自动检查格式合规性。

---

## 6. 验证

触发一次手动同步：

```bash
gh workflow run sync.yml
```

检查：
- [ ] prepare job 正常完成，S3 中有子配置文件
- [ ] 各 registry CodeBuild 成功
- [ ] 中国区 ECR 新仓库存在，镜像 digest 正确
- [ ] 新仓库自动带 lifecycle policy（ECR 控制台 → 仓库 → Lifecycle policies）
- [ ] catalog/ 目录自动更新

---

## 7. 旧同步机制与存量仓库

v2 部署后不需要立即处理旧系统，两者独立运行不冲突。待 v2 稳定后可以：

1. **停用旧同步**：禁用旧仓库 GitHub Actions，或 Archive 旧仓库
2. **清理存量仓库**：启用 cleanup Lambda（`CleanupScheduleState=ENABLED`），dry run 审查报告后执行删除
3. **补加 lifecycle policy**：存量 172 个仓库未受 Creation Template 覆盖，可批量补加：

```bash
POLICY='{"rules":[{"rulePriority":1,"description":"Limit to 50 images","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":50},"action":{"type":"expire"}}]}'
for repo in $(aws ecr describe-repositories \
  --region cn-northwest-1 --profile <048912060910 profile> \
  --query 'repositories[].repositoryName' --output text); do
  aws ecr put-lifecycle-policy \
    --repository-name "$repo" \
    --lifecycle-policy-text "$POLICY" \
    --region cn-northwest-1 --profile <048912060910 profile>
done
```

以上步骤与 v2 部署完全解耦，可在任意时间执行。
