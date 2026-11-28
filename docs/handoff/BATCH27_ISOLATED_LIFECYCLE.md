# 第27批受控音乐会话资源回收

本批以 `cd9fe290a00c2990f506aadd2a01e84fe724fc60` 为施工基线。第26批复核确认，主进程被强制终止后，插件 Job 内的进程和环回监听很快退出，但当时未加入 Job 的独立 MPV、专用 IPC、两份 AppContainer profile 和会话目录在 60.125 秒的末次观测仍存在。

## 进程与 IPC

- 只有明确启用隔离音乐宿主时，音乐控制器才切换独立 MPV 的启动方式。MPV 在创建进程时通过 Windows `PROC_THREAD_ATTRIBUTE_JOB_LIST` 进入专用 Job。Job 句柄仅由受控入口持有，最后一个句柄关闭时由 Windows 终止成员。每一代 MPV 单独生成 IPC 名称。现有控制器继续负责正常停止和异常后的重启。
- 插件进程在创建挂起线程时也已加入自己的 Job。进程仍须通过来源原有的令牌、SID、capability 和 Job 检查，才可恢复线程。启动失败按原有失败路径返回。 [Microsoft Job Objects](https://learn.microsoft.com/windows/win32/procthread/job-objects) · [Microsoft 进程创建属性](https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute)

## 临时资源与受控重启

- 宿主在配置指定的信任目录内保存会话收据。创建运行时和注册 profile 前先写入收据。收据记录当前用户 SID、进程 PID 和 Windows 创建时间、工作父目录及目录的文件身份。profile 在创建前检查同名 SID 尚未注册，再记录精确名称和 SID。
- 正常关闭仍由宿主和会话释放资源。异常退出后的下一次受控启动以收据为入口，核对旧进程已退出并认领清理操作。它用 Windows API 删除登记的 profile，确认映射消失后移除会话目录，最后删除收据。收据与会话目录分离，便于中断清理后继续。 [Microsoft DeleteAppContainerProfile](https://learn.microsoft.com/windows/win32/api/userenv/nf-userenv-deleteappcontainerprofile)
- 错误的文件身份、超出配置目录的路径以及重解析点均拒绝回收。旧版会话缺少收据，或启动在提交收据前中断，只保留原目录。收据提交前未启动任何挂载在该目录的运行时及 profile。若 ACL、磁盘错误或系统 API 阻止清理，保留剩余收据与日志供复核。
- MPV 单独异常退出后，应用重新启动专用播放器，可重新选择在线歌曲。沿用既有在线播放恢复策略。验收涵盖已签名自编样本、正式可见 Qt 音乐面板和独立 MPV，商业曲库仍需单独验收。原有手改交接文件保持其原始版本，本批记录追加到此独立文档。
