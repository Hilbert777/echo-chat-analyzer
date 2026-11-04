# BUG-02：QQ Direct DB 捕获协议修复记录

本次仅使用人工构造的数据。未启动 QQ，未访问真实数据库，未提交、推送、发布或修改版本号。
BUG-01 的 Header 页数修复作为开始工作时的基线保留，不计为 BUG-02 的成果。
现场版本按已核实的 Echo v0.3.0、QQ 9.9.30.48517、Node v22.16.0 处理；没有把本机 Node 或其他 QQ 安装的版本当作现场运行端版本。

## 根因与证据边界

已确认的实现缺陷是：原捕获流程在采样 SHM 后，全量读取 Main，随后才读取选定边界 B 的 WAL，整个窗口没有参与 SQLite 的源数据库锁协议。SHM 两份 Header 一致、短时间重复读取稳定，以及 Echo workspace lease，都不能阻止后续 checkpoint 和 WAL reset/reuse。

这解释了捕获为什么会跨越合法的数据库生命周期变化，而不是说明 salt 或提交边界校验应该放宽。现场约 6.1 GiB 的 Main 基线读取使 SHM 采样与 WAL 读取之间存在约 6.1～6.7 秒的窗口。

| 错误 | 源码触发条件 | 本次实验 | 现场证据的限制 |
| --- | --- | --- | --- |
| `wal_frame_salt_invalid` | `readCommittedWal` 比较 WAL Header 的两项 salt 与每个 Frame Header 的 salt | SQLite 合法 RESTART 后新代际只有 1 帧，物理尾部保留旧代际；旧 B=3 扫描到旧尾帧而失败 | 现场没有记录失败帧号、实际 WAL 代际；不能证明三次都发生了 reset |
| `wal_boundary_not_committed` | 完成 B 帧扫描后，`lastCommitFrame !== B` | 旧 B=3 落入新代际 B=8 的多页事务非提交帧；所有已读帧 salt/checksum 合法，但旧边界不是新提交边界 | 现场 B=41；没有记录实际 lastCommitFrame，不能确定具体事务 |
| `main_changed_during_read` | 路径状态字符串、句柄/路径身份或完整 SHA-256 任一不一致；保持原短路顺序 | 分别注入元数据变化、相同内容的文件替换、内容变化，均拒绝并清理 | 现场不能区分具体子条件，不能仅凭错误码认定 QQ 正在写 Main |

因此，两类 WAL 错误已有同一个 reset/reuse 机制的确定性复现；现场是否全部同源仍未知。checkpoint 也可以使 Main 变化，但 Main 的身份变化、外部内容修改、时间戳变化可以独立触发第三类错误。没有把五次失败都归结为同一个竞态。

原逻辑的对照实验先于生产代码修改执行：稳定捕获成功；真实 SQLite 3.54.1 RESTART 生成两个新 WAL 代际，再在既有交错入口按顺序发布生成的人工文件，得到上述两类 WAL 错误。它证明跨代读取的机制，不是对历史现场的完整重演。事件顺序、旧/新边界、代际是否变化均保存在本地实验记录中，不保存原始 salt。

另外确认了独立的取消缺陷：捕获使用同步长循环，RPC 超时/断开只丢弃最终响应，不能中止正在进行的捕获。

## 协议选择及依据

| 方案 | 安全性与可行性 | 决策 |
| --- | --- | --- |
| 调整读取顺序、提高速度、增加重试 | 缩短窗口，不能固定 Main/WAL 的一致性边界 | 不作为一致性方案 |
| 原生 SQLite 连接/backup | 正常 SQLite 可用；现有入口缺少能直接读取 QQ 自定义前缀及加密格式的受支持只读连接 | 本次不新增密钥接管或自定义 VFS |
| 捕获全程持有 writer 排他锁 | 可以冻结写入，但大文件捕获期间阻塞普通提交 | 不采用 |
| Windows 原生共享 CKPT + WAL 读槽锁 | CKPT 锁阻止 Main checkpoint；读槽锁阻止 WAL 重用，允许尾部追加 | 采用，并保留全部校验 |

依据为 SQLite **version-3.54.1** 的 [`wal.c`](https://github.com/sqlite/sqlite/blob/version-3.54.1/src/wal.c)：`sqlite3WalCheckpoint` 获取排他 CKPT 锁，`walRestartLog` 重用 WAL 前获取全部非零读槽的排他锁；`sqlite3WalSnapshotCheck` 也使用共享 CKPT 锁保护 checkpoint 边界。Windows 锁字节来自同版 [`os_win.c`](https://github.com/sqlite/sqlite/blob/version-3.54.1/src/os_win.c) 的 `winShmLock`、`WIN_SHM_BASE`、DMS 与 `winGetReadLock`。锁协议的一般说明见 [SQLite WAL-index 格式](https://www.sqlite.org/walformat.html)。

专用辅助进程仅以只读句柄打开 Main/WAL/SHM，并拒绝文件删除/替换共享。按顺序获取 Main 的共享锁、SHM DMS 共享锁、CKPT 共享锁（字节 121）、READ_LOCK(1) 共享锁（字节 124）。获取任一锁失败就释放已持有的锁并失败关闭，不在部分持锁状态等待。

这个读槽锁只负责阻止 WAL 重用；没有改写 `aReadMark`，也不把它冒充一个完整的 SQLite 读事务。阻止 Main checkpoint 的职责由 CKPT 锁承担。初始 witness 在全部锁就绪后采样，B 只选一次；新增提交只允许在 B 后追加。

SHM 通过只读内存映射采样，因为 [Windows 文档](https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-mapviewoffile) 不保证普通文件读取与映射写入的视图一致。仍然执行原有双 Header、重复采样、checksum、身份与最终 witness 校验；并未用映射或锁替代校验。

已用固定哈希的 SQLite 3.54.1 DLL，在两个真实进程之间验证生产锁辅助程序：普通更新可以提交，Main 字节保持不变，PASSIVE/FULL/RESTART/TRUNCATE checkpoint 均返回 busy，完全 backfill 的 WAL 也不重用；释放后 checkpoint 和新代际重用恢复成功。适用前提是源连接遵循 SQLite Windows VFS 协议；未宣称已经验证所有 QQ 自定义 VFS 行为。

## 实现与保护

- `source_guard.ps1` / `source_guard.mjs`：持有 SQLite 源锁、通过匿名管道返回内存映射 witness、确认锁和源句柄释放后再返回成功。EOF、到期、异常均释放资源；不终止 QQ。PowerShell 使用后台任务读取管道，避免 .NET Framework 的同步 Console reader 使超时失效；显式使用无 BOM 的 UTF-8 管道编码。中文源路径在本机默认 CP936 下先确定性失败，修正编码后通过。
- `main_wal.mjs`：捕获与完整摘要扫描改为异步分批执行，约每 1 MiB 让出事件循环并检查取消/截止时间；Main 合并读写也按最多 256 页批处理。原两次 Main 摘要读取保留，没有增加诊断性全量读取。
- `snapshot.mjs`：源锁先于 SHM 采样，贯穿完整捕获与复核；必须收到释放确认且辅助进程正常退出，才允许解密。失败/取消删除本次暂存产物。workspace lease 仍单独管理输出所有权。
- `bridge.mjs`：将内部 AbortSignal 和绝对截止时间传入 acquire；客户端断开或原预算耗尽时取消。没有延长 65 秒上限。已进入原生解密时，没有安全中断接口，等待返回后丢弃，禁止发布。已取消的排队请求不释放其他消费者的 workspace lease；响应最终刷入 socket 期间仍保留断开后的回收处理。
- runtime pins / 两份 manifest：加入两份辅助文件，固定变更模板的实际哈希，并加入 Windows 的显式复制清单，防止发行时缺文件或混用模板；打包测试验证两份文件均按原字节复制。
- PoC 调用和既有夹具适配异步入口；SHM 追加夹具改用内存映射写入，符合 SQLite 的发布方式。新增专项测试注册到 QQ 域，保留原有其他任务的注册项。`test_qq_runtime_wiring.py` 的人工发行目录也包含两份新辅助文件，继续验证插件的资源与路径准入。

salt、提交标记、滚动 checksum、缺失页面、Page 1 来源与 salt、文件长度、Main 元数据/身份/内容、完整 WAL 前缀复读、SHM 稳定性、明文校验、失败清理均保留。生产 API 没有无锁 fallback。无锁 PoC 入口继续用于构造破坏性对照实验。

本次共涉及以下 17 个文件；独立补丁以开始时已包含 BUG-01 的工作区为基线，不把其已有修改算入 BUG-02：

| 范围 | 文件 |
| --- | --- |
| 捕获 | `scripts/qq_direct_db_snapshot/main_wal.mjs`、`snapshot.mjs`、`main_wal_poc.mjs`、`source_guard.mjs`、`source_guard.ps1`（均在同目录） |
| RPC | `scripts/qq_napcat_plugin/bridge.mjs` |
| 发行清单与哈希 | `scripts/qq_napcat_runtime_pins.json`、`scripts/qq_napcat_runtime_manifest.json`、`scripts/windows_runtime_manifest.json` |
| 测试 | `tests/conftest.py`、`tests/test_qq_direct_capture_protocol.py`、`tests/test_qq_direct_main_wal_poc.py`、`tests/test_qq_direct_main_wal_acquisition.py`、`tests/test_napcat_qq_provider.py`、`tests/test_portable_runtime_copy.py`、`tests/test_qq_runtime_wiring.py` |
| 本记录 | 本文件 |

## 验证与性能

最终测试结果在本次交付说明中列出；原始实验记录、修改前基线及独立审查补丁留在本地审计目录，不作为发行资产。专项覆盖真实 SQLite 锁协调、旧边界跨代失败、三类 Main 变化、捕获中取消、RPC 超时/断开、释放确认缺失、部分持锁失败、父进程 EOF、辅助进程到期、排队取消的 lease 归属及中文路径编码。

| 验证 | 结果 |
| --- | --- |
| 修改前基线实验 | 稳定对照通过；两类 WAL 竞态与三类 Main 变化按预期拒绝，记录顺序及边界关系 |
| Node v24.16.0 全量回归 | **2331 passed，22 skipped，0 failed**；QQ Direct DB Snapshot、恢复、解密、取消及核心回归均包含在内 |
| Node v22.16.0 捕获与桥接专项 | **97 passed**；随后辅助进程关闭时改为等待 stdout 排空的 `close` 事件，最终源锁专项 **14 passed** |
| QQ 核心快速回归 | **647 passed，2 skipped**；最终源码由全量回归再次覆盖 |
| 官方 NapCat 4.18.34 归档与资源验证 | **4 passed**；显式指定本地官方归档，补齐全量中对应的四项环境跳过 |
| 打包复制、清单及 bootstrap 定向验证 | **51 passed，1 skipped**；两份新增辅助文件的实际复制断言也在最终全量通过 |
| BUG-01 与资产 | Header 校验函数、原有 BUG-01 测试及诊断文档不变，11 份候选包及验证文件哈希匹配 |
| 差异检查 | `git diff --check` 通过；独立 BUG-02 补丁的反向只读应用检查通过 |

全量的其余跳过包含：11 项缺少当前 Frozen 构建、1 项缺少已构建便携运行时、4 项 Windows 符号链接权限不足，以及 2 项未指定原生 WeChat CLI。没有把跳过项列为通过。本次未生成新的 Frozen/Setup 产物，已有 BUG-01 候选包也不包含本次 BUG-02 变更。

除本机 Node v24.16.0 外，还在本地隔离目录使用 [官方 Node v22.16.0](https://nodejs.org/en/blog/release/v22.16.0) 执行捕获及桥接专项回归，下载 ZIP 与官方 SHASUMS256 一致；没有安装到系统或改动项目依赖。这验证的是独立 Node 下的人工数据路径，不能代替 QQ 内嵌运行端验收。

人工约 128 MiB 数据的三轮对照测量，旧捕获本体中位数约 498 ms，新捕获本体约 304 ms；新增辅助进程启动约 502～571 ms，新流程总耗时中位数约 912 ms。测试与其他回归并行时有明显抖动，不能据此预测真实 6.1 GiB 的耗时，也不能宣称总耗时一定降低。

捕获期间 checkpoint 被延迟，普通 WAL 提交继续，因此 WAL 可能增长。持锁时间覆盖捕获和复核，不覆盖解密及消费；原始完整摘要保护保留，所以较慢机器仍可能在原预算内无法完成大文件捕获，届时取消并失败关闭。同步的单次文件系统调用若被内核阻塞，不能承诺硬实时取消；不会因此强杀 QQ。

## BUG-01、资产与验收边界

开始前保存已有修改、分支/HEAD、BUG-01 四文件基线及候选安装包/验证记录的哈希。BUG-01 的 `finalizeStandaloneSnapshot` 函数与开始时完全一致，其既有测试保留；同一 Main 模块的 runtime hash 随 BUG-02 改动更新，旧候选包不覆盖、不重新签发。

分支仍为开始时的 `main`，HEAD 仍为 `51cc7cac29a3d3059dc228ff567250c652e031b3`；没有暂存或执行 Git 写操作。修改及本地审计资产保留供本次修复审核与验收使用，结束后再按负责人的明确授权归档或清理，不自动删除。

BUG-02 的源码与人工数据验证不能代替真实 QQ 验收。跨机器完成前仍需要经负责人另行授权的真实环境验收，尤其是源 QQ VFS 是否遵循所用锁协议、持续写入下的延迟、WAL 增长、取消及清理。本次不联系或要求原同学重新测试，也不把这一待验收项算作已通过。
