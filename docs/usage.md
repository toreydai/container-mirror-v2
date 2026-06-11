# 使用方式

ECR 私有 registry 需要认证。用户需要以下拉取权限：

```json
[
  "ecr:GetAuthorizationToken",
  "ecr:GetDownloadUrlForLayer",
  "ecr:BatchGetImage",
  "ecr:BatchCheckLayerAvailability"
]
```

## Docker 和 docker-compose

登录：

```bash
aws ecr get-login-password --region cn-northwest-1 \
  | docker login --username AWS --password-stdin \
    <account-id>.dkr.ecr.cn-northwest-1.amazonaws.com.cn
```

Docker：

```bash
docker run --rm \
  <account-id>.dkr.ecr.cn-northwest-1.amazonaws.com.cn/dockerhub/library/busybox:1.36 \
  echo ok
```

docker-compose：

```yaml
services:
  web:
    image: <account-id>.dkr.ecr.cn-northwest-1.amazonaws.com.cn/dockerhub/library/nginx:1.27
    ports:
      - "8080:80"
```

## Kubernetes YAML

把 `image` 字段替换为镜像后的 ECR 地址：

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: busybox-ecr-demo
spec:
  containers:
    - name: busybox
      image: <account-id>.dkr.ecr.cn-northwest-1.amazonaws.com.cn/dockerhub/library/busybox:1.36
      command: ["/bin/sh", "-c", "echo ok && sleep 3600"]
```

验证：

```bash
kubectl apply -f busybox-demo.yaml
kubectl get pod busybox-ecr-demo -o=jsonpath='{.spec.containers[0].image}'
```

## Helm

大多数 chart 支持通过 values 覆盖镜像仓库和 tag，具体 key 名称由 chart 决定：

```bash
helm install my-release repo/chart \
  --set image.registry=<account-id>.dkr.ecr.cn-northwest-1.amazonaws.com.cn \
  --set image.repository=dockerhub/library/nginx \
  --set image.tag=1.27
```

部分 chart 要求 registry 和 repository 合并为一个字段：

```bash
helm install my-release repo/chart \
  --set image.repository=<account-id>.dkr.ecr.cn-northwest-1.amazonaws.com.cn/registryk8sio/pause \
  --set image.tag=3.9
```

验证最终 Pod 镜像：

```bash
kubectl get pod -A -o jsonpath='{range .items[*]}{.metadata.namespace}{" "}{.metadata.name}{" "}{.spec.containers[*].image}{"\n"}{end}'
```

## Kustomize

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

预览并应用：

```bash
kubectl kustomize .
kubectl apply -k .
```

## ECS 和 Fargate

在 task definition 中替换 `containerDefinitions[].image`：

```json
{
  "containerDefinitions": [
    {
      "name": "web",
      "image": "<account-id>.dkr.ecr.cn-northwest-1.amazonaws.com.cn/dockerhub/library/nginx:1.27",
      "essential": true,
      "portMappings": [{ "containerPort": 80, "protocol": "tcp" }]
    }
  ]
}
```

ECS task execution role 需要具备 ECR 拉取权限。

验证 task 镜像：

```bash
aws ecs describe-task-definition \
  --task-definition <task-definition> \
  --region cn-northwest-1 \
  --query 'taskDefinition.containerDefinitions[*].image'
```

## Kubernetes Mutating Webhook

webhook 会自动把 Pod 中的源 registry 地址改写为中国区 ECR 路径，无需修改 YAML。

### 安装

```bash
kubectl apply -f infra/mutating-webhook.yaml
```

`infra/mutating-webhook.yaml` 中的 `clientConfig.url` 需指向 API Gateway 输出的 `WebhookURL`：

```bash
aws cloudformation describe-stacks \
  --stack-name container-mirror \
  --region cn-northwest-1 \
  --query 'Stacks[0].Outputs[?OutputKey==`WebhookURL`].OutputValue' \
  --output text
```

### 镜像改写规则

| 原始镜像 | 改写后 |
|---|---|
| `nginx:1.27` | `ECR/dockerhub/library/nginx:1.27` |
| `docker.io/library/nginx:1.27` | `ECR/dockerhub/library/nginx:1.27` |
| `registry.k8s.io/pause:3.9` | `ECR/registryk8sio/pause:3.9` |
| `quay.io/prometheus/prometheus:v3.12.0` | `ECR/quay/prometheus/prometheus:v3.12.0` |
| `direct.to/busybox:latest` | `busybox:latest`（绕过改写） |

### 绕过改写

如需使用原始地址，在镜像名前加 `direct.to/` 前缀：

```yaml
image: direct.to/busybox:latest
```

### 更新 webhook Lambda

```bash
cd functions/webhook
zip webhook.zip lambda_function.py
aws lambda update-function-code \
  --function-name container-mirror-image-webhook \
  --zip-file fileb://webhook.zip \
  --region cn-northwest-1
```

如果部署时设置了 `WebhookToken`，webhook URL 需附加 token 参数：

```text
https://<api-id>.execute-api.cn-northwest-1.amazonaws.com.cn/call?token=<WebhookToken>
```
