# 升级背景与设计说明

container-mirror 2.0 是对原有镜像同步方案的系统性重建，核心原则是**让不规范的操作在技术上无法发生**，而不是依赖人工约定。

## 1. 为什么升级

原有方案暴露出三类结构性问题：

1. **存储失控**：`type: repository` 全量 mirror，php 累积 4,448 个 tag，drupal 3,943 个，近 5 TiB 中 87% 从未被任何环境拉取
2. **可靠性差**：所有 registry 串行同步，一个超时拖垮全部；无 digest 预检，每次全量 push 不论内容是否变化
3. **缺乏治理**：无 PR 审查、无 lifecycle policy、无镜像清单，运维人员不清楚当前同步了什么

## 2. 设计思路

### 2.1 双路径同步，显式声明

放弃 `type: repository`，所有条目精确到 `image:tag`。两条独立同步路径：

1. **固定 tag**（`regsync.yml`）：精确锁定版本，merge 后立即触发，适合生产组件
2. **自动追版**（`required-images-weekly.txt`）：只写镜像名，工具每周自动筛选最新稳定版，适合跟进社区版本

### 2.2 tag 过滤内置在工具层

`tools.py build-weekly-config` 强制执行三层过滤：

1. **白名单**：`^v?\d+\.\d+(\.\d+)?(-alpine[\d.]*)?$`，只允许语义化版本和 alpine 变体
2. **黑名单**：`alpha|beta|rc|windowsservercore|nanoserver|slim|otel|perl`
3. **版本上限**：每个镜像最多保留最新 2 个 minor 版本

### 2.3 PR 阶段双重合规检查

每个 PR merge 前必须通过：

1. `tools.py validate`：校验 target prefix 格式、tag 必须显式指定、ECR 地址匹配当前账号
2. `tools.py lint-weekly`：检测是否误用 `docker.io/library/*`，必须改用 ECR Public 源

不合规变更在 CI 阶段报错并阻断 merge，永远不会进入同步流程。

### 2.4 消除 DockerHub rate limit

Docker Hub 官方库镜像（`library/*`）全部迁移到 `public.ecr.aws/docker/library/*`。ECR Public 是 DockerHub library 的无限速官方镜像，内容完全一致，写入 ECR 的路径相同（`dockerhub/library/`），下游无感知。`lint-weekly` 在 PR 阶段持续拦截，防止误写回 `docker.io/library/*`。

### 2.5 按 registry 并行，失败互相隔离

prepare job 将配置按 registry 拆分上传 S3，sync job 启动 6 个独立 CodeBuild 并行运行（`fail-fast: false`）。每次同步前对每个 digest 做预检，目标 ECR 已有相同 digest 直接跳过。DockerHub 单独设置 `parallel: 2` 限速。

### 2.6 lifecycle policy 内置在基础设施中

`infra/main.yaml` 所有 8 个 `AWS::ECR::RepositoryCreationTemplate` 资源均内置 lifecycle policy，新仓库创建时自动生效，默认上限 50 个镜像（`MirrorImageCountLimit` 参数可调）。cleanup Lambda 作为第二层：按"N 天未拉取"维度定期审查，dry run 输出 S3 报告 + SNS 通知。

## 3. 技术亮点

1. **ECR RepositoryCreationTemplate + LifecyclePolicy**：利用 `AWS::ECR::RepositoryCreationTemplate` 的 `LifecyclePolicy` 属性，在仓库创建时自动注入清理策略，无需逐仓库手动配置。这是 ECR 较新的特性，覆盖 8 个 prefix 前缀（dockerhub、gcr、ghcr 等），对后续新增仓库同样生效。

2. **S3 中转 + CodeBuild matrix 并行**：prepare job 将合并配置拆分为独立子文件上传 S3，sync job 通过 GitHub Actions matrix 并行触发各 registry 的 CodeBuild。Tokyo CodeBuild 同时具备访问海外 registry 和写入中国区 ECR 的能力，是两个网络区域的天然桥接点。

3. **digest 级幂等同步**：regsync 在拉取前检查目标 registry 的 digest，与源端一致则跳过整个 pull/push 流程。首次同步后，重复运行 ecrpublic batch 仅耗时 1.6 秒、zero Copy，证明增量检查开销接近零。

4. **policy-as-code：lint-weekly CI 门禁**：DockerHub rate limit 是运行时故障，难以在出现前感知。将"禁止 docker.io/library"规则编码为 CI check，将运行时风险前移到 PR 阶段，确保限速问题永远不会进入同步流程。

5. **Kubernetes MutatingWebhook 自动改写**：API Gateway + Lambda 实现 K8s admission webhook，Pod 创建时自动将海外镜像地址改写为中国区 ECR 路径，集群侧零配置，存量 workload 无需修改 YAML。

6. **双 IAM 账号隔离**：Tokyo CodeBuild 使用全球区 IAM（触发执行），中国区 ECR 写入使用独立 China IAM（最小权限），两套凭证通过 GitHub Secrets 独立管理，互不混用。

## 4. 优化对比

| 维度 | 原方案 | v2 |
|---|---|---|
| 同步范围 | `type: repository` 全量 | `type: image` 显式 tag |
| tag 过滤 | 无 | semver 白名单 + deny 黑名单 + max_minor=2 |
| PR 合规检查 | 无 | validate + lint-weekly 双重拦截 |
| DockerHub 限速 | 频繁触发，导致超时 | library 镜像切 ECR Public，已消除 |
| 同步去重 | 无 digest 预检 | digest 预检，已同步直接跳过 |
| 并行与隔离 | 单 batch 串行，一失败全失败 | 6 registry 独立并行，失败互不影响 |
| ECR lifecycle policy | 无（需人工补加） | 内置 RepositoryCreationTemplate |
| 存量清理 | 无自动机制 | cleanup Lambda + dry run + SNS |
| 镜像可见性 | 无清单 | catalog 自动生成 txt/json/csv |
| 认证管理 | GITHUB_TOKEN 权限不足 | DockerHub PAT + GHCR PAT 独立配置 |
| K8s 接入 | 手动修改 image 地址 | MutatingWebhook 自动改写 |

## 5. 实际效果

1. **首次同步**（R7/R8，2026-06-11）：6 registry 并行，总耗时约 8 分钟；DockerHub grafana 因 digest 未变被跳过，预检去重生效
2. **幂等性验证**：R8 重复同步 ecrpublic，1.6 秒完成，zero Copy 操作，增量检查开销接近零
3. **存储规模**：原方案无治理状态接近 5 TiB（87% 从未拉取）；v2 当前 ~50 个 tag，ECR 存量预估 10–30 GiB，lifecycle policy 保证不会无上限增长
