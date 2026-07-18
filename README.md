# container-mirror-v2

将 Docker Hub、registry.k8s.io、public.ecr.aws、Quay、GHCR、GCR 等公共镜像自动同步至 AWS 中国区 ECR（`<account-id>.dkr.ecr.cn-northwest-1.amazonaws.com.cn`）。

## 声明

您理解并同意：本镜像站收集并供您下载的镜像文件是按"原样"提供的，即我们无法控制或修改镜像文件，可能会出现由于开发者未及时更新或该镜像文件本身存在异常导致该镜像文件损坏或其他不可用状态，我们也不提供有关文件（内容）的任何保证，不会对镜像文件及其相关的信息或文档的可用性、可靠性、正确性或更新、升级等提供任何明示或默示的承诺或保证，镜像文件的下载和使用完全由您自主决定并自行承担风险，由此带来的任何损失，您同意在法律允许的范围内放弃追究我们的责任。

如果您是 container image 的权利人，不允许相关镜像同步至 AWS 中国区 ECR，请发送邮件至 nwcd_labs@nwcdcloud.cn。

## ECR 镜像路径

ECR Registry：`<account-id>.dkr.ecr.cn-northwest-1.amazonaws.com.cn`

| 源 | 原始路径示例 | ECR 路径前缀 |
|----|------------|------------|
| Docker Hub（library） | `nginx:1.27` | `dockerhub/library/nginx:1.27` |
| Docker Hub（org） | `grafana/grafana:11.0.0` | `dockerhub/grafana/grafana:11.0.0` |
| registry.k8s.io | `registry.k8s.io/pause:3.9` | `registryk8sio/pause:3.9` |
| public.ecr.aws | `public.ecr.aws/amazonlinux/amazonlinux:2023` | `ecrpublic/amazonlinux/amazonlinux:2023` |
| Quay | `quay.io/prometheus/prometheus:v3.12.0` | `quay/prometheus/prometheus:v3.12.0` |
| GHCR | `ghcr.io/kyverno/kyverno:v1.18.1` | `ghcr/kyverno/kyverno:v1.18.1` |
| GCR | `gcr.io/distroless/base-debian12:latest` | `gcr/distroless/base-debian12:latest` |

已同步镜像清单见 [catalog/mirrored-images.txt](catalog/mirrored-images.txt)（每次同步后自动更新）。

## 使用

先登录 ECR，再按原有方式使用镜像（Docker / K8s / Helm / Kustomize / ECS）：

```bash
aws ecr get-login-password --region cn-northwest-1 | \
  docker login --username AWS --password-stdin \
  <account-id>.dkr.ecr.cn-northwest-1.amazonaws.com.cn
```

> EKS / Fargate 节点角色有 ECR 权限时无需手动登录。详细使用示例见 [docs/usage.md](docs/usage.md)。

## 增加或更新镜像

**固定 tag**（锁定特定版本，适合生产组件）：编辑 `regsync.yml`，添加 sync entry，提交 PR：

```yaml
- source: public.ecr.aws/docker/library/nginx:1.27
  target: <account-id>.dkr.ecr.cn-northwest-1.amazonaws.com.cn/dockerhub/library/nginx:1.27
  type: image
```

**自动追最新版**（持续跟进社区版本，适合基础镜像）：在 `required-images-weekly.txt` 添加镜像名（不带 tag），提交 PR：

```
public.ecr.aws/docker/library/nginx
quay.io/prometheus/prometheus
```

> Docker Hub 官方库镜像必须使用 `public.ecr.aws/docker/library/` 源以避免限速，PR validate 会自动检查。

## 自动同步

- **定时**：每周一 UTC 18:00（北京时间周二 02:00）
- **变更触发**：`regsync.yml` 或 `required-images-weekly.txt` 合并到 main 后立即执行
- **手动**：GitHub Actions → `sync-container-mirror` → Run workflow

## 文档

| 文档 | 说明 |
|---|---|
| [docs/deployment.md](docs/deployment.md) | 在现有账号全新部署 v2 的操作手册 |
| [docs/background.md](docs/background.md) | 与老仓库的关系、升级起因、设计思路和效果对比 |
| [docs/architecture.md](docs/architecture.md) | 架构、两条同步流程、registry 映射 |
| [docs/usage.md](docs/usage.md) | 镜像使用方式（Docker / K8s / Helm / ECS / Webhook） |
| [docs/operations.md](docs/operations.md) | 自部署、运维、清理和回滚 |
| [docs/testing/maintainer.md](docs/testing/maintainer.md) | 维护者测试手册 |
| [docs/testing/end-user.md](docs/testing/end-user.md) | 最终用户测试手册 |

## 目录结构

```
├── regsync.yml                        # 固定 tag 同步配置
├── required-images-weekly.txt         # 每周自动追版清单（只写镜像名）
├── sync-list.txt                      # 当前同步快照（自动生成，只读）
├── catalog/                           # 已同步镜像目录（自动生成，只读）
│   ├── mirrored-images.txt
│   ├── mirrored-images.json
│   └── mirrored-images.csv
├── .github/workflows/
│   ├── sync.yml                       # 触发 CodeBuild 执行同步
│   └── validate.yml                   # PR 时校验配置格式
├── infra/
│   ├── main.yaml                      # CloudFormation：中国区 ECR、清理、通知、webhook
│   ├── buildspec.yml                  # CodeBuild 构建规范
│   ├── codebuild.yaml                 # CloudFormation：Tokyo CodeBuild + IAM Role
│   └── mutating-webhook.yaml          # Kubernetes MutatingWebhook 资源清单
├── functions/
│   ├── cleanup/lambda_function.py     # Lambda：清理 ECR 过期镜像
│   └── webhook/lambda_function.py     # Lambda：K8s admission webhook，改写 image 路径
├── scripts/
│   └── tools.py                       # validate / build-weekly-config / split / catalog 工具
└── docs/                              # 文档
    ├── architecture.md
    ├── usage.md
    ├── operations.md
    └── testing/
        ├── maintainer.md
        └── end-user.md
```

## License

MIT - see the [LICENSE](LICENSE) file for details.

## 免责声明

- 本项目仅供学习与技术参考，不构成生产部署方案。
- 运行过程中会创建 AWS 资源并产生费用，请在实验结束后及时清理。
- 作者不对因使用本项目产生的任何费用或损失承担责任。
- 本项目与 Amazon Web Services 无官方关联，相关服务的可用性与定价以 AWS 官方文档为准。
- 生产环境使用前请根据实际需求进行安全评估与调整。
