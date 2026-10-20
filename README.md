# Komari Agent Docker

使用[官方 Komari Agent 镜像](https://github.com/komari-monitor/komari-agent)，提供可复现的 Compose 部署、独立 JSON 配置、AppArmor/seccomp 限制及部署检查。项目不修改或重新编译官方 Agent。

**唯一用户配置入口是私有的 `deploy.json`**：镜像版本与摘要、面板地址、节点 Token、网卡、流量结算日和资源限制都在这里。Compose 和 Agent JSON 由脚本生成，不需要维护多份配置。

这是一套**关闭远程控制的监控部署方案**：支持指标上报及 ICMP/TCP/HTTP 探测任务，关闭 Web SSH、远程执行与 Agent 自更新。不用于从面板管理宿主机文件或执行命令。

## 支持范围

- 原生 Linux **amd64**，本机 rootful Docker Engine、Docker Compose v2 或更高版本。
- Python 3.9+，仅使用标准库；无需 pip、Go 或自行构建镜像。
- AppArmor 已启用，`apparmor_parser` 可用，`apparmor.service` 开机启用且处于 active；Docker 支持 AppArmor 和 seccomp。
- 当前磁盘采集方案只支持根文件系统 `/`，Docker 数据目录须位于同一文件系统。
- 不支持 rootless、userns-remap、远程 Docker daemon，以及尚未验证的 ARM64/SELinux-only 环境。检查失败会明确停止，不自动关闭安全策略。

默认镜像固定为 `1.2.60` 与对应仓库摘要。这是已验证部署的基线，**不是声称它是官方最新版**。镜像引用可以修改；新版本仍需兼容性验证，见 [COMPATIBILITY.md](COMPATIBILITY.md)。不声明支持任意未来版本或任意 Linux 机器。

## 目录与配置

```text
deploy.example.json            # 可公开的配置模板，无真实凭据
deploy.json                    # 用户配置，0600，Git 忽略
manage.py                      # 配置校验、生成、部署、检查与生命周期操作
security/apparmor/             # AppArmor 模板
security/seccomp/amd64.json    # amd64 系统调用允许列表
.generated/                    # 自动生成的 Compose、Agent 配置和策略，Git 忽略
data/net_static.json           # 每台机器自己的流量历史，Git 忽略
backups/                       # 私有备份，可自行创建，Git 忽略
```

`deploy.json` 含节点凭据；`.generated/agent.json` 是它的派生文件，权限为 `0400`。生成目录和数据目录为 `0700`。不要提交、展示或上传这两个配置文件、数据目录、备份或未经审查的日志。

| 配置 | 含义 |
| --- | --- |
| `project_name` | Compose 项目标识；同一主机上必须唯一，部署后保持不变 |
| `image` | 官方镜像完整引用：`ghcr.io/komari-monitor/komari-agent:版本@sha256:仓库摘要` |
| `agent.endpoint` / `agent.token` | HTTPS 面板地址和该机器独立的节点 Token |
| `agent.include_nics` | 实际宿主机网卡；多个用逗号分隔，支持模式匹配；不要照搬另一台机器的 `eth0` |
| `agent.include_mountpoints` | 当前方案固定 `/`；额外磁盘需要另行设计只读挂载和验证 |
| `agent.month_rotate` | 每月流量结算日，1–31；0 关闭月度统计，模板默认为 0 |
| `agent.interval` | 指标采集间隔，秒，默认 3 |
| `agent.info_report_interval` | 基本信息上报间隔，分钟，默认 5 |
| `agent.max_retries` / `agent.reconnect_interval` | 重试次数 / 重连间隔秒数 |
| `agent.disable_web_ssh` / `agent.disable_auto_update` | 本方案强制为 `true`，不能通过配置取消 |
| `resources` | 内存、CPU、PID、临时空间、文件描述符、停止等待时间及日志轮转 |

默认限制：内存 128 MiB、内存与 Swap 合计 128 MiB、CPU 0.5、PID 64、临时目录 16 MiB、文件描述符 1024、停止等待 20 秒、日志 `10 MiB × 3`。这些值都由本项目声明，不依赖宿主机 Docker 全局配置。配置校验拒绝未知字段、重复键、错误类型和未替换的占位符。

## 首次部署

以下命令在目标机器执行。先按操作系统和 Docker 官方文档准备依赖；本项目不会安装软件包、修改 Docker daemon 配置或重启 Docker。

```sh
git clone https://github.com/poohyeban/komari-agent-docker.git
cd komari-agent-docker
python3 manage.py init
```

编辑 `deploy.json`，至少填写面板地址、该节点的 Token 和实际网卡。不要把 Token 放进公开命令、issue、聊天或 shell 历史。可先用 `ip -brief link` 和 `ip route show default` 检查网卡，但不要自动把 Docker bridge、veth 等虚拟网卡计入公网流量。

```sh
python3 manage.py validate
sudo python3 manage.py check
sudo python3 manage.py up
sudo python3 manage.py verify --seconds 60
sudo python3 manage.py status
sudo python3 manage.py logs
```

`check` 只读检查宿主机条件和部署冲突。`up` 先拉取和检查指定镜像，再生成私有文件、校验 Compose、安装/加载本项目 AppArmor 策略，最后只重建本项目 `agent` 服务。首次启动和升级可能短暂中断本 Agent 的上报。

首次加载的宿主机策略文件为 `/etc/apparmor.d/komari-agent-docker-<project_name>`，使用实际项目名替换占位符。该文件是持久化副本，由宿主机 AppArmor 服务在启动时加载；不会批量重载其他 AppArmor 策略。部署需要宿主机具备这些权限，不能只复制 Compose 就省略策略准备。

执行后还需在 Komari 面板检查：节点最近上报时间持续更新、CPU/内存/磁盘/网卡数据符合宿主机实际情况、需要的 ICMP/TCP/HTTP 任务有结果。`verify` 只验证本地运行、身份配置、重启/OOM、AppArmor、seccomp 和 capabilities，**不能证明面板已接收数据**。本方案没有虚构一个 Docker HTTP healthcheck；容器 `running` 也不等于上报正常。

脚本默认观察约 60 秒，可用 `--seconds 0` 做即时检查。用户指定观察时长时按其要求执行，不擅自延长。`logs` 会遮蔽当前 Token、面板 URL 和常见 URL/Token 表达，但仍需人工检查其他可能敏感的日志内容。

## 给部署 Agent 的执行约定

1. **先读取 README、兼容性与安全说明。** 确认用户指定的机器、目录、节点、网络采集范围。已有明确要求优先，不重复询问。
2. **先检查再操作。** 核对本机 Docker、架构、AppArmor、已有 Agent、网卡和磁盘。不要把本项目的默认项目名用于接管已有容器；脚本会拒绝覆盖不属于本目录的项目。
3. **只编辑 `deploy.json`。** 不编辑 `.generated`，不添加覆盖其含义的环境变量或单独 Compose override。模板有意不包含已经被新版移除的 `protocol_version`。
4. **凭据留在目标机器。** 使用用户提供的该机器节点 Token；每台机器使用自己的身份。不要读取现有 Agent 的 Token 后放进仓库或部署到别的机器。新机器不复制旧机器的流量历史。
5. **保留安全限制。** 检查不通过时说明原因；不要取消 AppArmor/seccomp、增加 privileged、挂载 Docker socket、共享 host PID 或扩大 capabilities 来绕过失败。
6. **尊重宿主机边界。** 本项目使用 host 网络读取宿主机网络指标及运行探测，不添加端口发布、宿主机防火墙、路由或 DNS 设置。AppArmor 的本项目策略安装是唯一内置宿主机准备步骤。没有自动安装定时器或 Watchtower。
7. **先验收再清理。** 报告实际镜像、来源配置、检查时间和结果。失败时保留原始私有备份；不得删除历史数据或全局 prune。日志中的探测失败与安全策略拒绝、面板连接失败要分开判断。

已有手工部署不会自动转换为本项目。如果以后迁移，先单独规划停止旧实例、备份、项目归属、数据和策略安装，避免同一 Token 同时运行两个 Agent。不要为了通过冲突检查直接删除旧容器。

## 网络、指标与安全边界

Agent 使用 **host 网络**，这与 SOCKS 代理容器的桥接方案不同。它主动连接面板并执行下发的探测任务；没有 `ports` 映射，但 host 网络本身不是网络隔离边界。不要根据没有端口映射就推断程序绝不可能监听宿主机端口。

通过只读 `/proc:/host/proc` 和 `HOST_PROC=/host/proc` 提供宿主机信息，并只读挂载 `/etc/os-release`。AppArmor 禁止读取多数宿主进程文件，但允许枚举进程数量及所需的 `net`、`mountinfo`。宿主机根目录没有整体挂载给 Agent；因此额外磁盘、特殊容器存储和 LXC 等环境必须额外核对指标。

以 root 运行是为了保留现有官方原始 ICMP 探测路径所需的有效 `NET_RAW`，同时丢弃其他 capabilities。它并不等同于 privileged 容器。根文件系统只读、NNP、AppArmor、seccomp 和 PTY 禁止规则共同限制权限；具体能力与局限见 [SECURITY.md](SECURITY.md)。

## 配置变更、升级和回退

修改 `deploy.json` 不会自动改变运行中的 Agent，也不会自动拉取新镜像。执行 `up` 会重新生成并强制重建容器，确保只读配置绑定也指向新文件；单纯 `docker restart` 不能代替这个流程。

选择官方新版本时，先查看发布说明和 [COMPATIBILITY.md](COMPATIBILITY.md)。在目标机上查询目标镜像的 `RepoDigests`、版本和源码 revision，例如：

```sh
docker pull ghcr.io/komari-monitor/komari-agent:1.2.60
docker image inspect ghcr.io/komari-monitor/komari-agent:1.2.60 \
  --format 'digests={{json .RepoDigests}} version={{index .Config.Labels "org.opencontainers.image.version"}} revision={{index .Config.Labels "org.opencontainers.image.revision"}}'
```

升级时把示例版本替换为计划升级的版本，将实际仓库摘要写进 `deploy.json` 的 `image`。不要拿源码 SHA、容器 ID 或示例占位符充当镜像摘要。`latest` 等移动标签不被此部署工具接受。

升级前保存旧配置和数据。以下在项目目录、拥有目录权限的同一管理用户下执行；root 创建的数据备份通常也需用 root 操作：

```sh
set -eu
umask 077
backup_dir="backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup_dir"
python3 manage.py stop
tar -czf "$backup_dir/state.tgz" deploy.json data
# 此时编辑 deploy.json 中的 image，保留其他业务配置。
# 如需同时更新部署工具源码，另外记录旧 Git 提交并审查策略变化。
python3 manage.py up
python3 manage.py verify --seconds 60
```

停止再备份可让 Agent 完成流量数据刷新。备份含凭据，应保持私有；升级失败也不要上传整个备份排障。配置使用 `month_rotate=0` 时，可能没有流量历史文件，但仍保留 `data` 目录。

回退时恢复备份中的旧 `deploy.json`，根据新旧数据格式兼容情况决定是否恢复 `data`。若需恢复数据，先停止容器。旧镜像保留在本机时使用：

```sh
python3 manage.py validate
python3 manage.py up --pull never
python3 manage.py verify --seconds 60
```

不要在验证前删旧镜像。新版本确认可用且不再需要本地回退后，使用 `docker image rm` 按准确旧引用清理，不加 `--force`，不执行全局镜像/卷清理。该项目直接拉取镜像，没有需要定期清理的本地构建缓存。

同一次升级不要无检查地同时改变 Agent 版本、部署工具版本、策略和面板版本。推荐在独立测试节点验证新镜像，确认指标、探测和安全限制后再推广。

## 常用命令和移除

| 命令 | 行为 |
| --- | --- |
| `init` | 创建私有配置模板，不覆盖已有文件 |
| `validate` | 校验配置，不连接 Docker，不打印凭据 |
| `render` | 仅生成本地私有配置及数据目录，不操作 Docker/宿主机策略 |
| `check` | 只读检查本机部署前提与项目冲突 |
| `up` | 拉取、准备策略并创建/重建本项目容器 |
| `verify` | 检查运行与安全状态，默认约 60 秒 |
| `status` / `logs` | 查看状态 / 经过有限脱敏的最近日志 |
| `stop` | 停止本项目容器，保留数据 |
| `down` | 移除本项目容器，保留配置、数据和 AppArmor 策略 |

完整移除时先 `down`，确认没有其他容器使用本项目 AppArmor profile，然后只卸载并删除对应 `/etc/apparmor.d/komari-agent-docker-<project_name>` 文件。配置、数据和备份是否删除由用户明确决定。不要关闭 AppArmor 服务或修改其他项目策略。

不要直接移动运行中的部署目录或修改 `project_name`；绝对挂载、管理归属标签和已安装策略与该部署位置关联。

## 开发与验证

```sh
python3 -m unittest discover -s tests -v
python3 scripts/check_public_files.py
git diff --check
```

GitHub Actions 在独立 Linux runner 中检查配置生成和公开文件清单，再拉取固定的基线镜像、解析 Compose、加载一次专用 AppArmor profile，并用 `--network none` 检查帮助命令、本地 ICMP 和权限限制。测试只使用占位凭据，不启动连接面板的 Agent，也不访问现有 VPS，结束时卸载测试策略。

CI 的隔离测试不等于真实面板端到端验证。新增官方版本和架构应分别记录检查结果，不能只修改矩阵名称就宣称已兼容。仓库文件发布前仍需人工审查；`.gitignore` 和凭据模式检查无法证明不存在任意形式的秘密。
