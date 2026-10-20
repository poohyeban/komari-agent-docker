# 安全设计与范围

该部署将 Komari 限制为监控和网络探测用途，保留基线部署的隔离策略。安全控制降低权限范围，不代表容器或上游程序不存在漏洞。

## 保留的限制

- root 用户仅持有 `NET_RAW`，删除其他 capabilities；没有 privileged、Docker socket 或 host PID。
- 根文件系统只读；`data` 持久化目录可写，`/tmp` 使用限定大小且 noexec/nosuid/nodev 的 tmpfs。
- NNP 开启，core dump 禁用，限制内存、CPU、PID、文件描述符及日志大小。
- AppArmor 禁止挂载、敏感内核路径访问、宿主进程多数文件读取，以及 PTY 分配。
- `/proc` 作为只读 `/host/proc` 挂载，并叠加 AppArmor 的写入限制；允许必要的指标读取。
- seccomp 默认返回 EPERM，仅允许既有运行所需调用；socket 地址族限制为 UNIX、IPv4、IPv6、NETLINK，限制 clone flags，clone3 返回 ENOSYS 以便运行时回退。
- `disable_web_ssh=true` 和 `disable_auto_update=true` 在配置校验时强制保留。

## 不应作出的保证

`NET_RAW` 仍是有意义的网络权限，host 网络会使容器共享宿主机网络空间。AppArmor 策略含通用文件访问许可再配合拒绝规则，并不是所有文件的严格允许列表。`/bin/sh` 与进程创建仍被允许，PTY 禁止规则不等于禁止一切非交互命令执行；关闭远程执行主要依赖官方 Agent 对配置开关的处理。

策略既绑定架构，也依赖 Agent/Go 运行时行为。升级时不要为了“能启动”而改为 unconfined、删除 seccomp 或添加 SYS_ADMIN。应确认需要的调用、路径和业务范围，再在隔离环境验证。

## 凭据与数据

`deploy.json`、`.generated/agent.json` 和备份中含凭据，仅在目标机器保存。面板 Token 不放进 Compose 环境变量、进程启动参数、镜像构建上下文或公共示例。`data/net_static.json` 是本机运行历史，不是可分发配置。

生成器拒绝覆盖私有路径的符号链接，不覆盖已有非本项目容器或 AppArmor 策略。root 执行部署工具前仍应审核源码、配置文件及工作目录的所有者；不应让不可信用户写入这些文件。

分享诊断材料时只提供经过人工审查的版本、状态、错误类别和去除凭据的日志片段。项目的日志脱敏不能识别所有潜在敏感数据；不要提交整个 `docker inspect`、配置或备份。

## 卸载边界

`down` 不删除私有数据或全局安全服务。需要移除策略时，仅卸载本项目对应的 profile，并确认没有其他容器使用它。不得关闭宿主机 AppArmor、重置宿主机防火墙或删除其他项目资源。
