# 最终用户测试手册

本文用于验证最终用户能否在常见运行环境中使用中国区 ECR 内的镜像。

## 1. 测试范围

覆盖场景：ECR 登录和拉取权限 / Docker 镜像拉取与运行 / docker-compose / Kubernetes 直接修改 YAML / Helm / Kustomize / ECS/Fargate / Kubernetes mutating webhook / `direct.to/` 绕过 / 镜像不存在和权限不足的失败表现

不覆盖：镜像同步系统内部实现 / 清理删除逻辑 / AWS 基础设施部署细节

## 2. 前置条件

```bash
export AWS_REGION=cn-northwest-1
export ECR=<account-id>.dkr.ecr.cn-northwest-1.amazonaws.com.cn
```

工具：`aws` / `docker` / `docker compose` / `kubectl` / `helm`

拉取镜像所需 IAM 权限：`ecr:GetAuthorizationToken` / `ecr:GetDownloadUrlForLayer` / `ecr:BatchGetImage` / `ecr:BatchCheckLayerAvailability`

## 3. ECR 登录

### 3.1 Docker 登录

```bash
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR"
```

预期：退出码为 0；输出包含 `Login Succeeded`。

失败排查：检查 AWS 凭证和 `ecr:GetAuthorizationToken` 权限；确认 `AWS_REGION=cn-northwest-1`。

## 4. Docker 使用

### 4.1 拉取 BusyBox

```bash
docker pull "$ECR/dockerhub/library/busybox:1.36"
```

预期：镜像成功拉取；不出现 `no basic auth credentials` 或 `repository does not exist`。

### 4.2 运行 BusyBox

```bash
docker run --rm "$ECR/dockerhub/library/busybox:1.36" echo ok
```

预期：输出 `ok`。

### 4.3 多架构镜像拉取

在 amd64 和 arm64 主机上分别运行：

```bash
docker run --rm "$ECR/dockerhub/library/nginx:1.27" nginx -v
```

预期：容器在两种架构上都能启动；不出现平台不匹配错误。

## 5. docker-compose 使用

创建 `docker-compose.yml`：

```yaml
services:
  web:
    image: <account-id>.dkr.ecr.cn-northwest-1.amazonaws.com.cn/dockerhub/library/nginx:1.27
    ports:
      - "8080:80"
```

```bash
docker compose up -d
docker compose ps
curl -I http://localhost:8080
docker compose down
```

预期：服务进入运行状态；`curl` 返回 HTTP 响应头。

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
      image: <account-id>.dkr.ecr.cn-northwest-1.amazonaws.com.cn/dockerhub/library/busybox:1.36
      command: ["/bin/sh", "-c", "echo ok"]
```

```bash
kubectl apply -f busybox-ecr-demo.yaml
kubectl wait --for=condition=Ready pod/busybox-ecr-demo --timeout=60s || true
kubectl logs busybox-ecr-demo
kubectl get pod busybox-ecr-demo -o=jsonpath='{.spec.containers[0].image}'
kubectl delete pod busybox-ecr-demo
```

预期：日志包含 `ok`；Pod image 等于 ECR 镜像地址。

失败排查：`ImagePullBackOff` 检查节点角色或 imagePullSecret；`ErrImagePull` 检查镜像是否已同步至 ECR。

## 7. Helm 使用

```bash
helm install mirror-nginx bitnami/nginx \
  --set image.registry="$ECR" \
  --set image.repository=dockerhub/library/nginx \
  --set image.tag=1.27

kubectl get pod -l app.kubernetes.io/instance=mirror-nginx \
  -o=jsonpath='{.items[0].spec.containers[0].image}'
helm uninstall mirror-nginx
```

预期：渲染后的 Pod image 以 `$ECR/` 开头。

说明：不同 chart 的 image values key 不同，需用 `helm show values` 确认。

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
    newName: <account-id>.dkr.ecr.cn-northwest-1.amazonaws.com.cn/dockerhub/library/nginx
    newTag: "1.27"
```

```bash
kubectl kustomize .
kubectl apply -k .
kubectl rollout status deployment/mirror-nginx --timeout=120s
kubectl get deployment mirror-nginx -o=jsonpath='{.spec.template.spec.containers[0].image}'
kubectl delete -k .
```

预期：Kustomize 输出中包含 ECR 镜像地址；Deployment 成功发布。

## 9. ECS/Fargate 使用

创建或更新 task definition，使 `containerDefinitions[].image` 指向：

```text
<account-id>.dkr.ecr.cn-northwest-1.amazonaws.com.cn/dockerhub/library/nginx:1.27
```

```bash
aws ecs describe-task-definition \
  --task-definition <task-definition> \
  --region "$AWS_REGION" \
  --query 'taskDefinition.containerDefinitions[*].image'
```

预期：输出包含 ECR 镜像地址；新 task 进入 `RUNNING` 状态。

失败排查：检查 ECS task execution role 是否有 ECR 拉取权限；检查任务网络是否能访问 ECR endpoint。

## 10. Mutating Webhook

### 10.1 安装 Webhook

```bash
kubectl apply -f infra/mutating-webhook.yaml
kubectl get mutatingwebhookconfiguration container-mirror-image-mutating
```

预期：Webhook configuration 存在。

### 10.2 Docker Hub 镜像替换

```bash
kubectl run mirror-webhook-nginx --image=nginx:1.27
kubectl get pod mirror-webhook-nginx -o=jsonpath='{.spec.containers[0].image}'
kubectl delete pod mirror-webhook-nginx
```

预期：image 被替换为 `<account-id>.dkr.ecr.cn-northwest-1.amazonaws.com.cn/dockerhub/library/nginx:1.27`。

### 10.3 registry.k8s.io 镜像替换

```bash
kubectl run mirror-webhook-pause --image=registry.k8s.io/pause:3.9
kubectl get pod mirror-webhook-pause -o=jsonpath='{.spec.containers[0].image}'
kubectl delete pod mirror-webhook-pause
```

预期：image 被替换为 `<account-id>.dkr.ecr.cn-northwest-1.amazonaws.com.cn/registryk8sio/pause:3.9`。

### 10.4 direct.to 绕过

```bash
kubectl run mirror-webhook-bypass --image=direct.to/busybox:latest
kubectl get pod mirror-webhook-bypass -o=jsonpath='{.spec.containers[0].image}'
kubectl delete pod mirror-webhook-bypass
```

预期：image 为 `busybox:latest`（剥离 `direct.to/` 前缀，不改写为 ECR 路径）。

### 10.5 Webhook 失败策略

临时将 webhook URL 指向无效 endpoint，创建测试 Pod。

预期：Pod 仍被 API Server 接收（`failurePolicy: Ignore`）；镜像保持原始地址。

测试后恢复正确 webhook URL。

## 11. 负向测试

### 11.1 镜像不存在

```bash
docker pull "$ECR/dockerhub/library/nginx:this-tag-should-not-exist"
```

预期：拉取明确失败；错误信息对用户可理解。

### 11.2 未登录 ECR

```bash
docker logout "$ECR"
docker pull "$ECR/dockerhub/library/busybox:1.36"
```

预期：拉取失败，错误与认证相关。测试后重新登录 ECR。

## 12. 完成标准

- [ ] Docker 登录成功
- [ ] Docker pull 和 run 成功
- [ ] Kubernetes 直接 YAML 成功
- [ ] Helm 或 Kustomize 至少一种成功
- [ ] ECS/Fargate task image 指向 ECR 且能运行
- [ ] Webhook 能替换 Docker Hub 和 registry.k8s.io 镜像
- [ ] `direct.to/` 绕过成功
- [ ] 负向测试按预期失败
