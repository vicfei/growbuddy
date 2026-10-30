# K.3 特权决策进程 + IPC 设计稿(RFC,待签核)

> 依据:主文档 K.3 时刻表 M5 行("特权决策进程独立:信任解析、R 闸、宪法校验
> 经 IPC 调用,宿主进程无法伪造")+ R6-B12(INV-5 落地路径:"R5 集合的校验
> 与裁定在独立进程内执行,内核进程即使被完全控制也无法改写宪法")。
> 本稿为实现批的签核前置;签核后随批执行宪法重锚定(E'' 裁决预定的
> "记忆写闸入宪"并入本次,避免二次重锚)。

## 1. 问题与目标

现状:全部特权决策(信任派生、预算授予、R 闸、记忆写闸、任务创建闸、
宪法校验)都在 daemon 宿主进程内执行。威胁:进程内任意代码执行
(恶意插件/供应链/技能注入逃逸)即等于全权失陷——可改 trust.json、
可绕过一切闸、可在下次日巡检前篡改运行时策略值。

目标:决策权与执行权分离。宿主进程被完全控制时,攻击者能得到的最坏
结果是**拒绝服务**(杀掉特权进程→fail-closed),而**不是提权**——
无法伪造信任解析、无法解锁预算与工具、无法绕过记忆写闸、无法改写
宪法裁定。

## 2. 进程边界

**特权进程(authority,`naga.authority`)**——独占持有并裁定:

| 决策 | 现位置 | 迁移后 |
|---|---|---|
| 信任派生 derive_trust(trust.json 独占读写) | 宿主 | authority |
| 信任授予/撤销 grant/revoke(含扫码自动授级) | 宿主 | authority |
| 预算授予 task_budget(§10.1 封顶) | 宿主(锚定值) | authority |
| R 闸 tools_for_trust / rCeiling | 宿主(锚定值) | authority |
| 记忆写闸(semantic 仅 owner) | MemoryStore 单点 | authority 裁定 |
| 定时任务创建闸(guest 拒绝) | gateway | authority 裁定 |
| 宪法校验+熔断命令 | 宿主(每日) | authority(每日+按需) |

**宿主进程(daemon)保留**:车道调度、渠道、agent 回路、账本追加与投影、
CAS、记忆密钥与存储、worker 子进程、WebChat/WS 面——即一切"执行"。

**边界裁决(须签核 D1)**:账本追加留在宿主。理由:K.3 冻结范围是
"决策经 IPC";账本防篡改由哈希链+宪法锚定承担(宿主伪造事件可被链上
审计发现,但伪造不了宪法锚)。把账本写也移入 authority 会让 IPC 面
膨胀一个数量级,收益边际。

## 3. IPC 设计

- **传输**:Unix domain socket `~/.naga/authority.sock`(0600;目录 0700)。
  协议:JSON 行(请求/响应),复用 worker.py 的行协议模式。
- **消息面(v1)**:
  - `trust.derive {channel, peer}` → `{trust, source}`
  - `trust.grant {channel, peer, trust, note}` → `{ok}`(扫码授级、CLI 升级都走此面)
  - `task.authorize {actor_trust, requested_budget}` → `{budget, tool_ceiling}`
  - `memory.writegate {layer, actor_trust}` → `{allow}`
  - `job.gate {actor_trust}` → `{allow}`
  - `constitution.verify` → `{ok, sha, violations}`(authority 每日自跑 + 宿主按需)
  - `fuse.shutdown {reason}` → authority 检出违宪时命令宿主优雅停机
  - `ping` → `{ok, uptime}`
- **宿主接入**:`AuthorityClient`(连接管理/超时/重连);所有闸的现有
  单点函数(task_budget/tools_for_trust/写闸判定)改为经 client 调用,
  签名不变——调用方(agent/gateway/memory)零改动。

## 4. 父进程与生命周期(须签核 A)

- **v1:宿主派生子进程**(worker 同款:daemon 启动时拉起 authority,
  崩溃自动重建、指数退避;stdio 日志归 daemon.log)。
  诚实边界:被完全控制的宿主可以**杀死** authority(=DoS→fail-closed,
  可检),但**无法让它说谎**(裁定逻辑与 trust.json/宪法锚不在宿主
  内存里)。"无法伪造"兑现;"无法拒绝服务"不承诺(见故障语义)。
- **v2 升级路径(记档不实施)**:systemd 独立托管 authority,宿主仅客户端。
  本节点可后续一条 unit 文件切换,IPC 面不变。

## 5. 故障语义:fail-closed 矩阵(须签核 B)

| 场景 | 行为 |
|---|---|
| authority 不可达 | 新任务拒绝(明确文案"安全裁定期不可用");job 触发跳过(记 job.missed);semantic 记忆写入拒绝;扫码绑定失败(授级走 authority);WebChat 对话拒绝 |
| authority 崩溃 | 宿主重建(退避),期间同上;在飞请求按拒绝处理 |
| authority 检出违宪 | 停止应答 + `fuse.shutdown` 命令宿主优雅停机(P0 由 soak 下次启动核对入账) |
| 宿主被杀 | authority 检测 socket 断开超过宽限 → 自灭(无宿主无意义),不留孤儿 |

原则:**失效即安全**。可用性损失(assistant 暂时拒答)是安全代价,
与宪法层"绝不带病运行"同姿态。

## 6. 宪法增补与重锚定(须签核 C)

新增规则(活体锚点在 authority 模块内,宿主无法触碰):

- **C6a** 记忆写闸:semantic 仅 owner(authority 常量锚定)——兑现 E''
  裁决"写闸入宪并入 K.3"。
- **C6b** 任务创建闸:guest/untrusted 拒绝。
- **C7** fail-closed:authority 不可达时一切受闸动作拒绝(authority
  行为常量锚定)。
- 现有 C2/C3(预算/R 档)锚点**迁至 authority 模块**(值不变,锚点
  位置变——宿主里的常量降级为"参考副本",裁定以 authority 为准)。

重锚定走合法路径:`constitution.reanchored` 事件入账(note 记 K.3 批),
随实现批执行。

## 7. 测试策略

- 协议测试:每消息面的正确/拒绝/畸形输入(worker 协议同款)。
- 闸矩阵经 IPC 复跑:现有 guest/known/owner 闸测试改为经 client 路径
  (矩阵即代码不变,通路变)。
- fail-closed:杀 authority 进程→任务拒绝/绑定失败/semantic 拒绝;
  重启→恢复。
- 熔断:注入宪法漂移→fuse.shutdown→宿主优雅停机断言。
- 崩溃模糊:随机杀进程对(宿主/authority)重放一致性。

## 8. 发布与回退

- 上线开关 `NAGA_AUTHORITY_MODE=ipc`(默认)|`inproc`(应急回退,
  行为=现状;回退动作记录于登记册)。
- 烧机期(本批部署后 48h)每日双跑 constitution.verify(宿主+authority)。
- soak:时钟继续(用户已解除窗口纪律;本批若引发 P0,按出口标准重起算,
  归因复杂度已在解除决策中接受)。

## 9. 明确不做(v1)

- 外部哈希锚定心跳(K.3 机制清单里的第二设备公证)——需第二台设备,
  用户动作,另批。
- systemd 托管(v2)。
- 账本写移入 authority(D1 裁决)。
- OS 级沙箱/seccomp。

## 10. 待签核裁决点

| # | 裁决 | 建议 |
|---|---|---|
| A | authority 父进程:宿主派生(v1)+systemd(v2) vs 直接 systemd | 建议前者(测试与部署简,升级路径清晰) |
| B | fail-closed 严格度:全拒绝 vs 降级运行(如 guest 任务放行) | 建议全拒绝(与宪法同姿态;可用性损失短暂) |
| C | 宪法增补:C6a/C6b/C7 + C2/C3 锚点迁移,一次重锚定 | 建议如稿 |
| D1 | 账本追加留宿主 | 建议留(理由见 §2) |
| D2 | 回退开关 NAGA_AUTHORITY_MODE 形态 | 建议保留,回退须登记 |
