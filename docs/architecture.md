# 架构与同步流程

container-mirror 2.0 是一套把海外公共镜像同步到中国区 ECR 的自动化平台。同步分两条独立流程：

- **固定 tag 同步**：维护者编辑 `regsync.yml` 提 PR，merge 后立即触发。
- **每周自动追版**：维护者在 `required-images-weekly.txt` 中登记镜像名，每周自动查询最新稳定版本并同步。

## 架构图

```
  ┌─────────────────────────────────────────────────────────────────┐
  │  海外（GitHub Actions + Tokyo CodeBuild）                        │
  │                                                                 │
  │   PR / merge                                                    │
  │   weekly cron ──▶ validate ──▶ prepare job                      │
  │   manual dispatch            │  · build-weekly-config           │
  │                              │  · merge + split by registry     │
  │                              │  · upload sub-configs → S3       │
  │                              │                                  │
  │                    ┌─────────┼──────────────────┐               │
  │                    ▼         ▼                  ▼               │
  │             ┌──────────┐ ┌──────────┐    ┌──────────┐          │
  │             │CodeBuild │ │CodeBuild │ .. │CodeBuild │          │
  │             │ecrpublic │ │  quay    │    │  ghcr    │          │
  │             └────┬─────┘ └────┬─────┘    └────┬─────┘          │
  │                  │            │               │                 │
  │   pull from:     │            │               │                 │
  │   ECR Public ────┘            │               │                 │
  │   DockerHub ──────────────────┘               │                 │
  │   registry.k8s.io / Quay / GHCR / GCR ────────┘                │
  └──────────────────────────┬──────────────────────────────────────┘
                             │ push（海外 CodeBuild 直接写入中国区 ECR）
                             ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  中国区（cn-northwest-1）                                         │
  │                                                                 │
  │  ┌──────────────────────────────────────────────────────────┐   │
  │  │  ECR Private Registry                                    │   │
  │  │  150430853770.dkr.ecr.cn-northwest-1.amazonaws.com.cn   │   │
  │  │                                                          │   │
  │  │  dockerhub/library/   dockerhub/<org>/   ecrpublic/      │   │
  │  │  registryk8sio/       gcr/   quay/   ghcr/               │   │
  │  └──────────────────────────────────────────────────────────┘   │
  │            │                          │                         │
  │            ▼                          ▼                         │
  │   Lambda Cleanup               API Gateway + Lambda             │
  │   月度清理过期镜像               Kubernetes MutatingWebhook       │
  │   S3 报告 + SNS 通知            自动改写 Pod image 路径           │
  └─────────────────────────────────────────────────────────────────┘
                             │
                             ▼ 消费方
           Docker  ·  Kubernetes  ·  ECS/Fargate  ·  Helm  ·  Kustomize
```

## 关键设计决策

**为什么同步在海外执行？**
中国区机器无法稳定访问 Docker Hub、GHCR、GCR 等海外 registry；Tokyo CodeBuild 同时具备海外访问能力和到中国区 ECR 的写入路径，是唯一可行的执行点。中国区只承载 ECR、清理、通知和 Webhook，不参与拉取。

**为什么按 registry 拆分并行？**
不同 registry 的限速和认证策略差异大（DockerHub 200 pulls/6h、GHCR 需 PAT），串行执行一个 batch 超时会拖累全部。按 registry 拆分后各 batch 独立 CodeBuild，失败互不影响（`fail-fast: false`）。

**为什么 library 镜像优先用 ECR Public？**
`public.ecr.aws/docker/library/*` 是 Docker Hub 官方库镜像的无限速镜像，与 `docker.io/library/*` 内容完全一致，写入 ECR 的路径也相同（`dockerhub/library/`），下游透明。使用 ECR Public 彻底消除 DockerHub rate limit 对 library 镜像的影响。

**ECR 如何防止 tag 无限积累？**
所有 ECR Repository Creation Template 内置 lifecycle policy，每个仓库最多保留 50 个镜像（`MirrorImageCountLimit` 参数可调）；cleanup Lambda 另做第二层清理，删除超过阈值天数未拉取的镜像。

## 运行流程

```
Pull request  → 仅校验（validate workflow）
Merge 到 main → prepare → split → 各 registry 并行 CodeBuild → 中国区 ECR
Manual dispatch → 同上
Weekly cron   → 同上（额外执行 build-weekly-config 生成每周配置）
```

所有同步在 Tokyo CodeBuild 执行，中国区机器无法稳定访问海外公共 registry，不参与拉取。

---

## 流程一：固定镜像同步（PR 触发）

**适用场景**：需要长期固定同步某个具体 tag（如 `pause:3.9`、`kyverno:v1.18.1`）。

### 操作步骤

1. 编辑 `regsync.yml`，添加 sync entry：

   ```yaml
   - source: public.ecr.aws/docker/library/nginx:1.27
     target: 150430853770.dkr.ecr.cn-northwest-1.amazonaws.com.cn/dockerhub/library/nginx:1.27
     type: image
   ```

2. 提交 PR，validate workflow 自动运行：
   - `tools.py validate regsync.yml` — 校验格式、target prefix、tag 必须显式指定
   - `tools.py lint-weekly required-images-weekly.txt` — 检查是否误用了 docker.io/library 源

3. PR merge 到 main 后，sync workflow 触发（`regsync.yml` 有变更时）：
   - `prepare` job：读取 `regsync.yml`，合并 weekly config，拆分为各 registry 子配置上传 S3
   - `sync` job：matrix 并行启动各 registry 对应的 CodeBuild，由 Tokyo CodeBuild 执行 `regsync` 拉取并推送到中国区 ECR
   - `catalog` job：同步完成后更新 `catalog/` 下的镜像清单

---

## 流程二：每周自动追版（定时触发）

**适用场景**：持续跟进某个镜像的最新稳定版本，无需人工更新 tag。

### 操作步骤

1. 编辑 `required-images-weekly.txt`，只写镜像名，不带 tag：

   ```
   public.ecr.aws/docker/library/nginx
   quay.io/prometheus/prometheus
   ```

2. 提交 PR，validate workflow 同样运行，包括 `lint-weekly` 检查（见下方规则）。

3. 每周一 UTC 18:00（北京周二 02:00）自动触发，或手动 workflow dispatch：
   - `build-weekly-config`：对每个镜像查询 registry API 获取最新稳定 tag，生成临时 regsync 配置
     - 每个镜像最多保留 2 个 minor 版本（含 alpine 变体）
     - 跳过 `alpha`/`beta`/`rc`/`windowsservercore` 等 deny tag
     - 已在中国区 ECR 且 digest 未变的镜像自动跳过（仅 docker.io 来源有此优化）
   - 后续流程与流程一相同（split → CodeBuild → catalog）

### 源 registry 选择规则

`required-images-weekly.txt` 中 **必须使用 ECR Public 作为 Docker Hub 官方库镜像的来源**：

| 情况 | 正确写法 | 错误写法 |
|---|---|---|
| Docker Hub 官方库镜像（`library/`） | `public.ecr.aws/docker/library/nginx` | `docker.io/library/nginx` ❌ |
| 非官方 Docker Hub 镜像 | `docker.io/grafana/grafana` | — |
| 其他 registry | 直接写原始地址 | — |

**原因**：`docker.io/library/*` 受 DockerHub 免费账号 200 pulls/6h 限制；`public.ecr.aws/docker/library/*` 无限速，两者写入 ECR 的路径相同（均为 `dockerhub/library/<image>`），下游透明。

违反此规则时，`lint-weekly` 在 PR validate 阶段报错并阻断 merge：

```
[FAIL] required-images-weekly.txt 中有 1 条 docker.io/library 条目，应改为 ECR Public 源：
  line 9: 'docker.io/library/nginx' — 请改用 'public.ecr.aws/docker/library/nginx'（ECR Public 无 rate limit）
```

---

## 两条流程对比

| | 流程一（固定 tag） | 流程二（每周自动追版） |
|---|---|---|
| 配置文件 | `regsync.yml` | `required-images-weekly.txt` |
| tag 管理 | 手动指定 | 自动查询最新稳定版 |
| 触发方式 | PR merge（路径匹配）| 每周 cron + 手动 dispatch |
| 适用场景 | 需要固定版本的生产组件 | 跟进社区最新版本的基础镜像 |
| DockerHub library 来源 | **推荐改用 ECR Public**（无限速风险） | **强制**使用 ECR Public（`lint-weekly` 拦截） |
| DockerHub 非 library 来源 | 允许（如 `docker.io/grafana/grafana`） | 允许 |

两条流程的输出都写入同一个中国区 ECR，路径格式相同，下游 webhook 和用户无感知。

---

## AWS 资源

| 资源 | 说明 |
|---|---|
| **ECR Repository Creation Templates** | push 时自动按 prefix 创建仓库；包含 lifecycle policy（默认最多 50 images/repo） |
| **CodeBuild**（Tokyo） | 执行 `regsync` 拉取海外镜像并推送到中国区 ECR |
| **S3** | 存放 split 后的子配置和清理报告 |
| **SNS** | 发送清理通知 |
| **Lambda + API Gateway** | Kubernetes mutating webhook，自动改写 Pod 镜像地址 |
| **Lambda**（cleanup） | 定期清理超过阈值天数未拉取的镜像，默认 dry run |

## Registry 映射

| 源 registry | ECR 目标前缀 | 备注 |
|---|---|---|
| `public.ecr.aws/docker/library/*` | `dockerhub/library/` | **推荐**：library 镜像首选，无 rate limit |
| `docker.io/library/*` | `dockerhub/library/` | 同路径；受 DockerHub 限速，不推荐 |
| `docker.io/<org>/*` | `dockerhub/<org>/` | 非 library 的 DockerHub 镜像 |
| `public.ecr.aws/*` | `ecrpublic/` | |
| `registry.k8s.io/*` | `registryk8sio/` | |
| `gcr.io/*` | `gcr/` | |
| `quay.io/*` | `quay/` | |
| `ghcr.io/*` | `ghcr/` | |
| `docker.elastic.co/*` | `elastic/` | |
| Global ECR（`602401143452.*`） | `amazonecr/` | |

## Catalog

同步完成后自动生成到 `catalog/` 目录，也可手动生成（需要中国区 AWS 凭证）：

```bash
python3 scripts/tools.py catalog
```

## 相关文件

| 文件 | 用途 |
|---|---|
| `regsync.yml` | 固定 tag 同步清单 |
| `required-images-weekly.txt` | 每周自动追版镜像清单（不带 tag） |
| `sync-list.txt` | 自动生成的当前同步镜像快照，只读 |
| `catalog/` | 自动生成的已同步镜像目录，只读 |
| `scripts/tools.py` | 所有构建/校验/拆分逻辑 |
| `infra/main.yaml` | CloudFormation：中国区 ECR、清理、通知、webhook 资源 |
| `infra/buildspec.yml` | CodeBuild 构建规范 |
| `.github/workflows/validate.yml` | PR 校验 workflow |
| `.github/workflows/sync.yml` | 同步执行 workflow |
