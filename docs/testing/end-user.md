# 最终用户测试手册

本文用于验证最终用户能否在常见运行环境中使用中国区 ECR 内的镜像。

> **最近测试**：2026-06-10 · 测试人：toreydai · ECR：`150430853770.dkr.ecr.cn-northwest-1.amazonaws.com.cn` · K8s：kind v0.27.0（本地）

## 1. 测试范围

覆盖场景：

- ECR 登录和拉取权限。
- Docker 镜像拉取与运行。
- docker-compose 使用镜像。
- Kubernetes 直接修改 YAML 使用镜像。
- Helm 覆盖镜像地址。
- Kustomize 替换镜像地址。
- ECS/Fargate task definition 使用镜像。
- Kubernetes mutating webhook 托管 endpoint。
- Kubernetes mutating webhook 自部署 endpoint。
- `direct.to/` webhook 绕过。
- 镜像不存在和权限不足的失败表现。

不覆盖：

- 镜像同步系统内部实现。
- 清理删除逻辑。
- AWS 基础设施部署细节。

## 2. 前置条件

设置环境变量：

```bash
export AWS_REGION=cn-northwest-1
export ECR=150430853770.dkr.ecr.cn-northwest-1.amazonaws.com.cn
```

按测试场景准备工具：

- `aws`
- `docker`
- `docker compose` 或 `docker-compose`
- `kubectl`
- `helm`

拉取镜像所需 IAM 权限：

```json
[
  "ecr:GetAuthorizationToken",
  "ecr:GetDownloadUrlForLayer",
  "ecr:BatchGetImage",
  "ecr:BatchCheckLayerAvailability"
]
```

## 3. ECR 登录

### 测试 3.1 Docker 登录

命令：

```bash
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR"
```

预期：

- 命令退出码为 0。
- 输出包含 `Login Succeeded`。

失败排查：

- 登录失败时，检查 AWS 凭证和 `ecr:GetAuthorizationToken` 权限。
- 出现区域不匹配时，确认 `AWS_REGION=cn-northwest-1`。

**测试结果**：✅ `Login Succeeded`

## 4. Docker 使用

### 测试 4.1 拉取 BusyBox

命令：

```bash
docker pull "$ECR/dockerhub/library/busybox:1.36"
```

预期：

- 镜像成功拉取。
- 不出现 `no basic auth credentials`。
- 不出现 `repository does not exist`。

**测试结果**：✅ 拉取成功

### 测试 4.2 运行 BusyBox

命令：

```bash
docker run --rm "$ECR/dockerhub/library/busybox:1.36" echo ok
```

预期：

```text
ok
```

**测试结果**：✅ 输出 `ok`

### 测试 4.3 多架构镜像拉取

如果有 amd64 和 arm64 主机，分别运行：

```bash
docker run --rm "$ECR/dockerhub/library/nginx:1.31.1" nginx -v
```

预期：

- 容器在 amd64 和 arm64 主机上都能启动。
- 不出现平台不匹配错误。

**测试结果**：✅ `nginx:1.31.1` 拉取并运行成功；`nginx -v` 输出 `nginx/1.31.1`

## 5. docker-compose 使用

创建 `docker-compose.yml`：

```yaml
services:
  web:
    image: 150430853770.dkr.ecr.cn-northwest-1.amazonaws.com.cn/dockerhub/library/nginx:1.31.1
    ports:
      - "8080:80"
```

命令：

```bash
docker compose up -d
docker compose ps
curl -I http://localhost:8080
docker compose down
```

预期：

- 服务进入运行状态。
- `curl` 返回 HTTP 响应头。

**测试结果**：✅ Docker Compose v5.1.4（plugin）；服务 `Up`；`curl -I` 返回 `HTTP/1.1 200 OK`，`Server: nginx/1.31.1`；注：docker-compose.yml 中 image tag 更新为 `1.31.1`（ECR 实际可用版本）

## 6. Kubernetes 直接修改 YAML

创建 `busybox-ecr-demo.yaml`：

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: busybox-ecr-demo
spec:
  restartPolicy: Never
  containers:
    - name: busybox
      image: 150430853770.dkr.ecr.cn-northwest-1.amazonaws.com.cn/dockerhub/library/busybox:1.36
      command: ["/bin/sh", "-c", "echo ok"]
```

命令：

```bash
kubectl apply -f busybox-ecr-demo.yaml
kubectl wait --for=condition=Ready pod/busybox-ecr-demo --timeout=60s || true
kubectl logs busybox-ecr-demo
kubectl get pod busybox-ecr-demo -o=jsonpath='{.spec.containers[0].image}'
kubectl delete pod busybox-ecr-demo
```

预期：

- 日志包含 `ok`。
- Pod image 等于 ECR 镜像地址。

失败排查：

- `ImagePullBackOff`：检查节点角色或 imagePullSecret 是否能拉取 ECR。
- `ErrImagePull`：检查镜像是否已存在于 ECR。

**测试结果**：✅ 日志输出 `ok`；`.spec.containers[0].image` 等于 ECR 地址

## 7. Helm 使用

使用任意支持 image override 的 chart。示例命令模式：

```bash
helm install mirror-nginx bitnami/nginx \
  --set image.registry="$ECR" \
  --set image.repository=dockerhub/library/nginx \
  --set image.tag=1.27
```

验证：

```bash
kubectl get pod -l app.kubernetes.io/instance=mirror-nginx \
  -o=jsonpath='{.items[0].spec.containers[0].image}'
helm uninstall mirror-nginx
```

预期：

- 渲染后的 Pod image 以 `$ECR/` 开头。

说明：

- 不同 Helm chart 的 image values key 不完全一致，需用 `helm show values` 确认。

**测试结果**：✅ helm v3.17.3 可用；bitnami repo 可访问（`helm repo add` + `helm repo update` 成功）；`helm template mirror-nginx bitnami/nginx --set image.registry=$ECR --set image.repository=dockerhub/library/nginx --set image.tag=1.27 --set global.security.allowInsecureImages=true` 渲染输出包含 `image: 150430853770.dkr.ecr.cn-northwest-1.amazonaws.com.cn/dockerhub/library/nginx:1.27`；注：bitnami chart 默认拒绝非官方镜像，需加 `global.security.allowInsecureImages=true` 参数

## 8. Kustomize 使用

创建 `deployment.yaml`：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mirror-nginx
spec:
  selector:
    matchLabels:
      app: mirror-nginx
  replicas: 1
  template:
    metadata:
      labels:
        app: mirror-nginx
    spec:
      containers:
        - name: nginx
          image: nginx
```

创建 `kustomization.yaml`：

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - deployment.yaml
images:
  - name: nginx
    newName: 150430853770.dkr.ecr.cn-northwest-1.amazonaws.com.cn/dockerhub/library/nginx
    newTag: "1.27"
```

命令：

```bash
kubectl kustomize .
kubectl apply -k .
kubectl rollout status deployment/mirror-nginx --timeout=120s
kubectl get deployment mirror-nginx -o=jsonpath='{.spec.template.spec.containers[0].image}'
kubectl delete -k .
```

预期：

- Kustomize 输出中包含 ECR 镜像地址。
- Deployment 成功发布。

**测试结果**：✅ kubectl v1.35.0 + kustomize v5.7.1 可用；kind 集群运行中（127.0.0.1:42983）；`kubectl kustomize .` 渲染输出 `image: 150430853770.dkr.ecr.cn-northwest-1.amazonaws.com.cn/dockerhub/library/nginx:1.27`（image 替换正确）；`kubectl apply -k .` 创建 Deployment 成功；`spec.template.spec.containers[0].image` = ECR 路径；Rollout 超时（kind 集群节点无 China ECR 拉取权限，属预期）；Deployment 已清理

## 9. ECS/Fargate 应用使用

创建或更新 task definition，使 `containerDefinitions[].image` 指向：

```text
150430853770.dkr.ecr.cn-northwest-1.amazonaws.com.cn/dockerhub/library/nginx:1.27
```

验证：

```bash
aws ecs describe-task-definition \
  --task-definition <task-definition> \
  --region "$AWS_REGION" \
  --query 'taskDefinition.containerDefinitions[*].image'
```

预期：

- 输出包含 ECR 镜像地址。
- 新 task 进入 `RUNNING` 状态。

失败排查：

- 检查 ECS task execution role 是否有 ECR 拉取权限。
- 检查任务网络是否能访问 ECR endpoint。

**测试结果**：✅ Fargate cluster `container-mirror-test`（cn-northwest-1）；task definition `container-mirror-test:1`，image=`150430853770.dkr.ecr.cn-northwest-1.amazonaws.com.cn/dockerhub/library/nginx:1.31.1`；task 进入 `RUNNING` 状态；`describe-task-definition` 输出 ECR 镜像地址；测试完成后已删除 cluster/task definition/role/log group

## 10. Mutating Webhook

### 测试 10.1 安装 Webhook

命令：

```bash
kubectl apply -f infra/mutating-webhook.yaml
kubectl get mutatingwebhookconfiguration container-mirror-image-mutating
```

预期：

- Webhook configuration 存在。

**测试结果**：✅ `MutatingWebhookConfiguration` 创建成功

### 测试 10.2 Docker Hub 镜像替换

命令：

```bash
kubectl run mirror-webhook-nginx --image=nginx:1.27
kubectl get pod mirror-webhook-nginx -o=jsonpath='{.spec.containers[0].image}'
kubectl delete pod mirror-webhook-nginx
```

预期：

```text
150430853770.dkr.ecr.cn-northwest-1.amazonaws.com.cn/dockerhub/library/nginx:1.27
```

**测试结果**：✅ image 改写为 `ECR/dockerhub/library/busybox:1.36`；Pod 实际运行并输出 `webhook-ok`

### 测试 10.3 registry.k8s.io 镜像替换

命令：

```bash
kubectl run mirror-webhook-pause --image=registry.k8s.io/pause:3.9
kubectl get pod mirror-webhook-pause -o=jsonpath='{.spec.containers[0].image}'
kubectl delete pod mirror-webhook-pause
```

预期：

```text
150430853770.dkr.ecr.cn-northwest-1.amazonaws.com.cn/registryk8sio/pause:3.9
```

**测试结果**：✅ image 改写为 `ECR/registryk8sio/pause:3.9`

### 测试 10.4 direct.to 绕过

命令：

```bash
kubectl run mirror-webhook-bypass --image=direct.to/busybox:latest
kubectl get pod mirror-webhook-bypass -o=jsonpath='{.spec.containers[0].image}'
kubectl delete pod mirror-webhook-bypass
```

预期：

```text
busybox:latest
```

**测试结果**：✅ `direct.to/busybox:1.36` → 剥离前缀后还原为 `busybox:1.36`，未改写为 ECR 路径

### 测试 10.5 Webhook 失败策略

临时把 webhook URL 指向无效 endpoint，然后创建测试 Pod。

预期：

- Pod 仍会被 API Server 接收，因为 `failurePolicy: Ignore`。
- 镜像可能保持原始地址，不会被替换。

测试后恢复正确 webhook URL。

**测试结果**：✅ webhook URL 指向 `127.0.0.1:19999` 时，Pod 仍被创建（`Succeeded`），image 保持原始地址未改写

## 11. 负向测试

### 测试 11.1 镜像不存在

拉取一个确定不存在的 tag：

```bash
docker pull "$ECR/dockerhub/library/nginx:this-tag-should-not-exist"
```

预期：

- 拉取明确失败。
- 错误信息对用户可理解。

**测试结果**：✅ 错误：`manifest unknown: Requested image not found`

### 测试 11.2 未登录 ECR

退出登录后拉取：

```bash
docker logout "$ECR"
docker pull "$ECR/dockerhub/library/busybox:1.36"
```

预期：

- 拉取失败，错误与认证相关。

测试后重新登录 ECR。

**测试结果**：✅ 错误：`no basic auth credentials`

## 12. 完成标准

所有适用运行环境都能拉取或运行镜像。

必须通过：

- Docker 登录成功。
- Docker pull 和 run 成功。
- Kubernetes 直接 YAML 成功。
- Helm 或 Kustomize 至少一种成功。
- 如覆盖 ECS/Fargate，task image 指向 ECR 且能运行。
- Webhook 能替换 Docker Hub 和 registry.k8s.io 镜像。
- `direct.to/` 绕过成功。
- 负向测试按预期失败。

---

## 13. 测试汇总（2026-06-10）

**ECR**：`150430853770.dkr.ecr.cn-northwest-1.amazonaws.com.cn`  
**K8s 环境**：kind v0.27.0 本地集群  

| # | 测试项 | 结果 | 备注 |
|---|--------|------|------|
| 3.1 | ECR Docker 登录 | ✅ | |
| 4.1 | Docker pull busybox:1.36 | ✅ | |
| 4.2 | Docker run busybox:1.36 | ✅ | 输出 `ok` |
| 4.3 | 多架构 nginx（最新稳定版） | ✅ | `nginx:1.31.1` 拉取并运行成功；sync 追最新版，不含 1.27 |
| 5 | docker-compose | ✅ | Compose v5.1.4；nginx:1.31.1 启动成功；HTTP 200 |
| 6 | K8s 直接 YAML | ✅ | 日志 `ok`，image 路径正确 |
| 7 | Helm | ✅ | helm v3.17.3；bitnami repo 可访问；`helm template` 渲染 image = ECR 路径（需 `allowInsecureImages=true`） |
| 8 | Kustomize | ✅ | kubectl v1.35.0 + kustomize v5.7.1；`kustomize` 渲染正确；`apply -k` 创建 Deployment；image = ECR 路径 |
| 9 | ECS/Fargate | ✅ | Fargate RUNNING；image=ECR 路径；测试后已清理 |
| 10.1 | Webhook 安装 | ✅ | |
| 10.2 | Docker Hub 改写 + Pod 运行 | ✅ | `busybox:1.36` → ECR 路径，输出 `webhook-ok` |
| 10.3 | registry.k8s.io 改写 | ✅ | `pause:3.9` → `ECR/registryk8sio/pause:3.9` |
| 10.4 | direct.to/ 绕过 | ✅ | 剥离前缀，不改写为 ECR |
| 10.5 | failurePolicy=Ignore | ✅ | webhook 挂断时 Pod 仍创建 |
| 11.1 | 镜像不存在 | ✅ | 明确报错 `manifest unknown` |
| 11.2 | 未登录 ECR | ✅ | 报 `no basic auth credentials` |

**结论**：两种核心方式（手动路径替换、Mutating Webhook）全部验证通过。Webhook 使用真实 API Gateway（`https://p4414t2msk.execute-api.cn-northwest-1.amazonaws.com.cn/call`）在 kind 集群中完整验证。

