# 兼容性

部署工具版本、官方 Agent 版本、宿主机平台是三个独立维度。镜像可以在 `deploy.json` 中更换，本项目没有把业务逻辑编译进官方镜像，但也不承诺所有未来镜像无需适配。

## 当前范围

| 对象 | 已知情况 |
| --- | --- |
| Agent 1.2.60 / Linux amd64 | 基线部署已实际运行：指标与任务、持久化、只读文件系统、NET_RAW、NNP、AppArmor、seccomp；原部署日志仍有网络任务失败及断线重连，不能据此承诺链路永不失败 |
| 本仓库生成器 | 单元测试覆盖配置校验、凭据文件权限、生成结果、安全边界和项目冲突；集成检查结果见 GitHub Actions |
| Debian 13 amd64 | 原部署验证环境；其他发行版需通过宿主机预检和实际验收 |
| Ubuntu 24.04 amd64 | GitHub Actions 使用的隔离检查环境；不连接真实面板 |
| Agent 1.5.11 | 做过源码对比；未以当前完整安全策略完成真实面板运行验证，不能宣称端到端兼容 |
| ARM64 / rootless / userns-remap / 无 AppArmor | 当前不支持，工具会拒绝运行，不降级安全策略 |

## 版本相关依赖

- **启动与路径**：镜像需继续提供 `/app/komari-agent --config FILE`，以及读取 `/host/proc` 的能力。
- **配置语义**：只生成监控所需的已知公共字段。上游新字段不会自动成为本项目支持项；应审查后明确添加。
- **上报协议**：1.2.60 默认使用 v2；1.5.11 移除 `protocol_version` 和 v1 协议回退。因此模板不生成该字段，面板需支持 v2。不能把未知字段被 JSON 忽略当作该选项仍然生效。
- **系统调用和路径**：自定义 seccomp 默认拒绝未列出的调用，AppArmor 限制部分路径。Go 运行时、官方功能或依赖变动可能需要调整策略；不能预先把所有限制放开。
- **可写目录和资源**：Agent 需能在指定工作目录保存流量数据，不应依赖写入镜像根文件系统。新功能可能增加内存、线程或临时空间需求。
- **升级与数据**：数据格式变化可能影响降级；先备份，按版本验证。
- **镜像架构**：官方发布流程有 amd64/arm64 镜像并不意味着本项目的 amd64 syscall 策略可以照搬到 ARM64。

1.5.11 新增的文件操作入口仍检查 `disable_web_ssh`；未来新增远程控制入口也需审查是否遵守该开关。参考[官方差异](https://github.com/komari-monitor/komari-agent/compare/1.2.60...1.5.11)、[配置字段](https://github.com/komari-monitor/komari-agent/blob/1.5.11/cmd/flags/flag.go)、[文件操作开关](https://github.com/komari-monitor/komari-agent/blob/1.5.11/server/files.go)。

## 新版本验收记录应包含

记录官方 tag、实际 RepoDigest、源码 revision、CPU 架构、操作系统、Docker/Compose 版本和所用策略提交。分别验证：

1. 镜像启动、配置生效和面板持续接收 v2 数据。
2. CPU/内存/磁盘/网卡读数与宿主机相符。
3. 实际 ICMP/TCP/HTTP 探测；探测目标本身失败不应直接归因于容器权限。
4. 重启后 Token 不变，流量历史可恢复。
5. AppArmor enforce、seccomp、NNP、仅 NET_RAW，远程控制维持关闭。
6. 无新增拒绝日志、持续崩溃或 OOM；保留可回退配置和数据。

镜像版本更新与本项目推荐版本更新是显式操作。`disable_auto_update=true` 和 Watchtower 排除标签有助于保持这一点；管理员仍需避免外部自动化替换容器。
