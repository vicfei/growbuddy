# 研究借鉴:微信 iLink 渠道(来自 Samsara 生产实现)

> 定位:M2「能连」渠道接入的设计输入。本文研读对象为同节点 Samsara(TS 路线)
> 的生产代码(`src/channel/wechat-ilink.ts` 等,2026-10 快照)与其研究文档
> `.context/design/research/wechat-ilink-channel.md`(Weknora 逆向分析)。
> 协议契约以 Samsara 实测代码为准;Naga 侧的采纳/改造决策为本文新增。
> 研究文档,非 R3 资产——冻结基线 `docs/design/` 不因本文改动。

## 一、iLink 协议契约速查(Naga 实现时的对照表)

基址 `https://ilinkai.weixin.qq.com`(绑定确认可返回专用 `baseurl`,须持久化并优先使用)。
除申请二维码外,所有请求需三头:

```
Authorization: Bearer <bot_token>
AuthorizationType: ilink_bot_token     ← 缺了就是 -14
X-WECHAT-UIN: base64(str(random_uint32))   ← 每次随机,服务端不校验具体值
```

| 接口 | 契约要点(实测坑) |
|---|---|
| `GET /ilink/bot/get_bot_qrcode?bot_type=3` | 返回 `qrcode`(轮询令牌)+ `qrcode_img_content`(二维码内容);二维码本地生成(`qrcode` 库),**不走公网第三方** |
| `GET /ilink/bot/get_qrcode_status?qrcode=…` | 长轮询 ~35s(客户端超时须 >38s);`wait→scaned→confirmed/expired`;**超时优雅折算为 wait**;confirmed 携带 bot_token/ilink_user_id/baseurl |
| `POST /ilink/bot/getupdates` | 长轮询收消息;**body 是 `{get_updates_buf, base_info}` 不是 `{timeout}`**;响应游标同名 `get_updates_buf`;首次传空串;消息在 `msgs[].item_list[]`,**文本在 `text_item.text` 不是 `content`**(Samsara 的 extractText 做了多字段兜底) |
| `POST /ilink/bot/sendmessage` | **必须回传 `context_token`**(消息串线);`msg.from_user_id` 留空、`to_user_id` 填对方;`message_type=2, message_state=2`;纯文本 `item_list:[{type:1}]`;单条 ~2000 字 |
| `POST /ilink/bot/getconfig` | **body 必须带 `ilink_user_id`,否则 ret=-2**(无文档记载);返回 typing_ticket |
| `POST /ilink/bot/sendtyping` | **status:1=开始/2=结束(不是 0)**;~60s 自动过期需周期重发;ticket 可缓存 ~10min |
| 媒体下载 | 不走上述接口:消息只带 `encrypt_query_param`,URL 由固定基址自构 `https://novac2c.cdn.weixin.qq.com/c2c/download?...`(**天然 SSRF 免疫**);AES-128-ECB + PKCS#7 解密;密钥三格式归一(hex32 / base64(16B) / base64(hex32));语音自带服务端转写(`voice_item.text` 直通) |
| 生命周期 | `errcode -14` = token 失效;Samsara 策略:连续 5 次暂停渠道并回调 onTokenExpired;token 无公布 TTL(数天到数周),-14 信号驱动重绑 |

## 二、架构经验映射(Samsara → Naga)

| Samsara 实现 | Naga 对应物 | 决策 |
|---|---|---|
| `ILinkClient`(fetch 构造注入,fake fetch 零网络测试) | `naga/llm.py` 的 DeepSeekAdapter 同模式 | **采纳**:渠道客户端构造函数注入 `fetch_impl: Callable`(httpx 风格),契约测试逐接口 fake |
| `WeChatChannel`(轮询/提取/信任/typing/分段;runner 回调注入而非完整 ChannelAdapter) | 接口 §5.2 ChannelAdapter(未实现) | **采纳 runner 注入模式**:渠道持有 `runner(goal, session_key, actor, channel_ctx)` 回调,与网关解耦——结构化类型,可测且不绑死 AgentLoop |
| `LaneQueue`(尾链 promise 串行) | gateway 已有 lane 队列(sha256(session_key) mod N + asyncio.Queue 消费者) | **复用网关车道**:渠道消息以 `wechat:dm:<peer>` 入网关同一 lane 结构——**会话路由单一权威**;注意 Samsara 尾链吞错,我们的逐任务隔离(批次 4 修)更强,保留 |
| `trust.ts`(allowlist 文件 + 渠道默认 + 派生来源) | §4.4/K.4 未实现 | **照搬形态**:`~/.naga/trust.json`(0600,渠道配置非账本事实;`trust_source` 随 session.open 入账);`DEFAULT_TRUST`:wechat=guest、webchat=owner(回环宪法条款)、未知渠道=untrusted 宁严勿宽;grantTrust/revokeTrust 幂等 |
| 绑定管理面 4 路由(bind/start、bind/poll/:qrcode、status、unbind)挂 WebChat HTTP | Naga 单网关(18789) | **采纳路由集,挂现有 aiohttp 网关**(`GET /wechat/*`),不需要 Samsara 的第二端口 18790;owner-only 回环(宪法层条款已由网关保证) |
| **热启动**:confirmed → 存凭据 → 授 owner → 立即 start 轮询(无需重启守护) | — | **采纳**:绑定确认即启动渠道实例 |
| 扫码绑定者自动授 owner(T3 配对审批的运行时形态) | — | **采纳**,note="QR 绑定自动授予";已有 allowlist 记录则不覆盖 |
| `context_token` per-peer 持久化(state 文件 0600,每消息直写) | — | **采纳**:重启后回复与主动推送仍可串线;发送时新 token 优先、持久态兜底 |
| typing 编排(ticket 缓存 540s + 45s 周期 status=1 + 结束补 status=2,**一切失败静默降级**) | — | **采纳**:best-effort 纪律——绝不阻断回复主流程 |
| 长回复分段(1900 字阈值,换行边界优先,(i/n) 序号,首条失败即停) | — | **采纳**:替代静默截断(批次二十四② 的教训) |
| errcode≠0 指数退避 1s→60s;-14 连续 5 次暂停 | — | **采纳** |
| 媒体:CDN 下载(≤20MB)→ AES 解密 → CAS 入档(`samsara-media/0`)→ 以档案引用文案入 lane,失败降级文案(不静默丢) | naga CAS 已有 | **采纳**,schema 命名 `naga-media/0`;文案形态照搬(`[图片] 用户发来图片 xxx(N B),已解密存档 CAS …`) |
| `abortedMessage`:预算耗尽转友好文案,内部错误串不直发用户 | — | **采纳**(批次二十三 C 的教训) |
| dbg 日志开关(SAMSARA_WECHAT_DEBUG=1 原始报文) | — | **采纳**(NAGA_WECHAT_DEBUG):逆向契约必备的观测缝 |

## 三、Naga 差异点(不照搬处)

1. **home**:一切落 `~/.naga`(issue #30);凭据 `credentials/wechat-ilink.json`、状态 `state/wechat-ilink-state.json`、`trust.json` 均随 home 解析(NAGA_HOME 可覆盖),不硬编码。
2. **入账纪律**:Samsara 绑定/解绑是纯文件操作;Naga 按自身纪律补账本事件(渠道启停列 R4,§7.2)——绑定确认/解绑各入一账(`channel.bound`/`channel.unbound`,additive,随 #23 批 RFC 入枚举),凭据本体仍只落 0600 文件不入账。
3. **消息与轨迹**:入站微信消息全文进 ReplayBundle(K.5)前过我们的两级脱敏闸(已有);微信昵称/peer_id 属 PII,redact 已有规则兜底。Samsara 的 `knownPeers` 昵称缓存采纳,但只用于显示与 trace,不入对外日志明文。
4. **K.3 隔离**:wechat-ilink 属第一方内置渠道(非外部 MCP/市场来源),M2 进程内合法;外部来源插件仍一律 worker 隔离(出口标准不变)。
5. **R 级与信任矩阵**:渠道对端 guest 默认 → agent.run 走 R0 封顶(§10.1);语义记忆写入门禁(§6.5)在 M2 记忆批次落地时一并接上 `actor.trust`。

## 四、M2 微信渠道切片建议

| 切片 | 内容 | 出口 |
|---|---|---|
| M2-W1 | trust.json 框架 + ILinkClient(8 接口,fake httpx 契约测试)+ 绑定管理面路由 + 热启动 + 文本收发全链路(经网关 lane → AgentLoop → sendMessage) | 微信发文本 → 回复送达,trace/bundle 落账 |
| M2-W2 | typing 编排 + context_token 持久化 + 分段发送 + -14 暂停/重绑 + 退避 | 长回复分段、重启串线、token 失效自愈路径演示 |
| M2-W3 | 媒体(CDN+AES 三格式密钥+20MB 上限+CAS 入档)+ 语音转写直通 | 图片/文件/语音消息端到端 |

## 五、常量清单(候选;随 #23 批 RFC 入注册表,先以代码常量+authority 注释)

| 常量 | 值 | 语义 |
|---|---|---|
| wechat_typing_refresh_sec | 45 | status=1 约 60s 过期,45s 重发留 15s 余量 |
| wechat_typing_ticket_ttl_sec | 540 | ticket 官方 ~10min,9min 保守续取 |
| wechat_media_max_bytes | 20971520 | 媒体密文上限(超限拒收) |
| wechat_reply_chunk_chars | 1900 | 分段阈值(单条上限 ~2000) |
| wechat_err14_pause_threshold | 5 | 连续 -14 次数 → 暂停渠道 |
| wechat_poll_backoff_max_sec | 60 | 轮询错误指数退避上限(1s 起) |

## 六、测试策略(照搬 Samsara 的成功模式)

- fake fetch/httpx 注入,零外网;契约测试逐接口断言 body/头(尤其 AuthorizationType、getconfig 的 ilink_user_id、typing status 值);
- 编排测试:typing 周期与降级、context_token 重启兜底、分段序号、-14 暂停、媒体解密(固定密文向量);
- 全链路:微信消息(带 token)→ lane → runner(fake)→ 回复分段送达断言。

---
*来源与致谢:契约逆向来自 Weknora 项目(longpoll.go/adapter.go/crypto.go),集成经验来自 Samsara 生产代码与其评审批次(十三/十七/二十三 C/二十四②/二十五)。Naga 侧映射与决策为本文新增。*
