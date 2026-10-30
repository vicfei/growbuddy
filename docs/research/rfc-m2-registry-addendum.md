# RFC 候选:M2 实现期注册表增补提案(kind 枚举 / spec-constants / 矩阵代码化说明)

> 定位:issue #23 的批量设计 RFC 素材汇总。冻结基线 `docs/design/`(R3 资产)**不因本文改动**;
> 本文按 K.0.2 纪律整理代码先行落地的 additive 增补,供评审后并入注册表。
> 证据形态:每条注明代码落点与真实运行佐证。截至 2026-10-10(M2 批次 4.5)。
>
> **签核状态:2026-10-11 设计所有者批准——"我都按建议来"。应用批已执行:**
> 枚举六 kind 入 §3.1(DM v1.4)、§5.6 归一映射注(IF v1.6)、spec-constants v3(11 条;
> 派生域 3 条权威=主文档 §5.4,渠道/默认 8 条 RFC 权威类)、检查器 R04/R09 扩展;
> 裁决记录 closure-ledger.yaml RFC23-B1。C 节三偏差按建议:摘要近似=接受、
> 会话绑定=推迟、write→R2=维持并已入 §5.6 注。

## A. kind 枚举 additive 增补(数据模型 §3.1)

§3.1 现有枚举已含 `session.open|close`、`skill.commit|promote|quarantine`、`effect.apply` 族
(Naga 细化形态的映射注记亦已在案)。代码先行新增、枚举缺失的共 4 条:

| kind | 落地批次 | 语义 | 裁决点 |
|---|---|---|---|
| `trace.record` | M1 批次 1(#23 已登记) | 任务轨迹完成入账(traces 投影来源) | 命名:trace.record vs trace.completed |
| `skill.use` | M1 收官 | 技能命中计量(检索注入/uses 计数;投影确定性) | 是否并入 skill.commit 载荷 |
| `channel.bound` | M2-W1 | 渠道绑定确认(载荷:channel/ilink_user_id/baseurl;凭据本体不入账) | 与 `channel.fallback`(已在枚举)同族扩列 |
| `channel.unbound` | M2-W1 | 渠道解绑 | 同上 |

kernel 种子条目(effect.register/apply_begin/apply_ok、fiber.*、event.emit 等)已有 §3.1
映射注记,不属缺口。

## B. spec-constants 注册候选(7 条,均为 wechat 渠道域)

以下常量以代码常量 + authority 注释先行(yaml 不动),全部有真实流量佐证:

| constant_id | value | 语义 / 依据 | 代码落点 |
|---|---|---|---|
| `wechat_typing_refresh_sec` | 45 | typing status=1 约 60s 自过期,留 15s 余量(Weknora 实测) | wechat.py |
| `wechat_typing_ticket_ttl_sec` | 540 | typing_ticket 官方 ~10min,9min 保守续取 | wechat.py |
| `wechat_reply_chunk_chars` | 1900 | 单条上限 ~2000 的分段阈值;真实 4705 字 3 段送达实证 | wechat.py |
| `wechat_media_max_bytes` | 20971520 | 媒体密文上限(超限拒收;K.7 大对象分层属 M3) | wechat.py |
| `wechat_media_download_timeout_sec` | 240 | CDN 慢滴流总额限(逐读超时无效:460KB 实测拖 197s) | wechat.py |
| `wechat_err14_pause_threshold` | 5 | 连续 -14(token 失效)→ 暂停渠道 | wechat.py |
| `wechat_poll_backoff_max_sec` | 60 | 轮询错误指数退避上限(1s 起) | wechat.py |

注:`guest_task_token_budget`(50,000)/`known_task_token_budget`(500,000)已在冻结注册表,
批次 4 代码值与之一致,不属增补。§10.1 其余 guest 限流项(并发 1/速率 10rpm+突发 3/存储
100MB·7天)未在 M2 实现,实现时按注册表现值。

## C. §4.1 事件可见性矩阵:代码化形态说明

批次 4.5 将矩阵全行落地为表驱动(`naga/gateway.py`):

- **事件类归类**:session(agent./session.)、asset(skill./promotion./memory.)、ops(job./shadow./health.)、
  governance(rollback./trust./constitution.)、node(node.*)、other(未登记前缀,宁严勿宽仅 owner)
- **决策四态**:none / own(本会话,需 session_key 匹配)/ summary(状态变更摘要,投递时裁剪为
  status·kind·name·version·outcome + summarized 标记)/ full
- **逐格与规格一致**:asset 行 guest=summary;governance 行 known=summary、node=own;node 行
  仅 owner=full、node=own
- **实现偏差(留待裁决)**:①"summary 仅状态变更摘要"以字段白名单近似,语义裁剪粒度未做;
  ②viewer_session 在 M2 为 None(WS 连接未绑定会话身份,回环即 owner)——非 owner 连接的
  会话绑定形态随渠道扩展批次裁决;③node 类事件 M2 尚无产生源,行为按规格占位。

## D. §5.6 cap→R 映射:代码化形态说明

批次 4.5 将 cap→R 全映射落地(`naga/gateway.py`):

- R0..R4 常量与 §5.6 表一致;`TRUST_R_CEILING`:guest=R0 / known=R1 / owner=无封顶(以 R4 表达)
- `tool_risk` 优先级:显式 `risk_level` > 旧 `side_effect` 归一(read→R0,write→R2)> 未声明
  默认 R4(§5.6"新 cap 未声明类别默认最高档")
- `tools_for_trust` 按 §10.1 档位过滤;未知信任级无工具
- **实现偏差(留待裁决)**:①side_effect="write" 归一为 R2(工作区外写)而非 R1——泛写
  声明无工作区边界信息,宁严勿宽;②工作区 trust_ceiling 收紧参数(K.0.2"可收紧不可放宽")
  未引入,当前仅信任级档位;③审批升档(R2 confirm/R3 审批队列/R4 双人复核)属模式系统(M4),
  映射表已按 R 级就位。

## E'. 追加积压(M4a-2,2026-10-11)→ 已随第二单裁决

- **第二单签核:2026-10-18 设计所有者批准("按建议来")**。应用批已执行:
  枚举五 kind 入 §3.1(DM v1.5)、spec-constants v4 补注册 17 条
  (workflow_compile_min_uses 权威=主文档附录 F.1;其余 16 条 RFC 权威类:
  内容注入/触发阈值/影子三参/GEPA 三参/路由三参/派生地板/工作流二参/
  降级预算/soak 二参)、检查器 R04/R09 第二单扩展、裁决记录
  closure-ledger.yaml RFC23-B2。原积压条目(skill.stale/archived、
  skill_promotion_trigger_uses)及其后 M4a-3/M4b/M5-1 新增候选全部随单裁决。

## E''. 第三单积压(记忆系统 §6.5,2026-10-18,issue #81)→ 已随第三单裁决

- **第三单签核:2026-10-18 设计所有者批准("按建议来")**。裁决:
  - A 节三 kind(memory.use/stale/archived)additive 入 §3.1(DM v1.6);
  - B 节六常量入 spec-constants v5(RFC 权威类,RFC23-B3);
  - C 节偏差四条——①关键词召回**接受为现状形态**,embedding 召回+重排推迟;
    ②semantic 非 owner 直接拒绝**接受**,人审队列形态推迟;③回滚恢复为
    active **接受**(Curator 按闲置重新归档);④写闸入宪法锚点**推迟**
    (与 K.3 特权进程硬化批次一并考虑,避免二次重锚定)。

- **additive kind 三条**(DM §3.1 枚举扩展候选,与 skill.use/stale/archived
  同路径):`memory.use`(召回计量,检索质量进 L3)、`memory.stale`、
  `memory.archived`(Curator 保鲜状态机,DM B.3 R6-B10)。既有
  `memory.write|forget|forget.rollback` 已在冻结枚举,无需裁决。
- **spec-constants 六条**(RFC 权威类候选):memory_max_bytes=16384 /
  memory_inject_max_chars=2000 / memory_recall_limit=5 /
  memory_distill_min_tool_calls=1 / memory_consolidate_min_recurrence=3 /
  memory_consolidate_similarity=0.5(实现值见 naga/memory.py 模块头)。
- **实现偏差记录**(供裁决):①读路径为确定性关键词召回,embedding 召回+
  中小模型重排(§6.5 原文形态)留待嵌入供应方落地;②semantic 非 owner
  写入当前为直接拒绝,"人审"路径(晋升请求队列形态)待模式系统接入;
  ③遗忘回滚将条目恢复为 active(而非遗忘前原状态,Curator 会按闲置重新
  归档);④记忆写闸门未入宪法锚点(避免重锚定,候选 R5 条款)。

## E'''. 第三单追加(定时任务附录 C,2026-10-18,issue #83)→ 已随第三单裁决

- **第三单签核:2026-10-18 设计所有者批准("按建议来")**。裁决:
  - A 节五 kind(job.created/paused/resumed/deleted/renewed)additive 入
    §3.1(DM v1.6;job.fire|missed 本在冻结枚举);
  - B 节四常量入 spec-constants v5(job_wall_ms_default 权威=主文档附录
    C.2 值字面在位;其余三条 RFC 权威类,RFC23-B3);
  - C 节偏差四条——①rCeiling 工具帽形态**维持**(宪法锚点不动,单向更严);
    ②smart=立即广播**接受为现状**,注意力路由时机(晨报)推迟;③微信
    任务结果投递**推迟**(待会话注册表批次);④misfire 精度=tick 粒度
    **接受**。
  - 应用批:issue #89;检查器 R04/R09 第三单扩展(规则数 20 不变);
    closure-ledger RFC23-B3(verified)。

- **additive kind 五条**(同上路径):`job.created` / `job.paused` /
  `job.resumed` / `job.deleted` / `job.renewed`。既有 `job.fire|missed`
  已在冻结枚举,无需裁决。
- **spec-constants 四条**(RFC 权威类候选):job_scheduler_tick_seconds=30 /
  job_default_budget_tokens=200000 / job_goal_max_bytes=4096 /
  job_wall_ms_default=1800000(C.2 示例值;实现值见 naga/jobs.py 模块头)。
- **实现偏差记录**(供裁决):①rCeiling 以任务级工具集收紧实现(叠在
  宪法信任档位之上,单向更严),宪法锚点未动;②smart 通知当前=立即
  broadcast job.completed(注意力路由器裁决时机属后续;凌晨进晨报未做);
  ③微信渠道的任务结果投递待会话注册表(session→channel 映射)批次;
  ④守护停机错过窗口以账本 last-fire 推算,精度=调度 tick 粒度。

## E''''. 第四单积压(会话注册表,2026-10-20,issue #91)——待批

- **additive kind**:无(session.open|close 本在冻结枚举;last_active 经
  trace.record 投影重建,未新增计量类 kind)。
- **spec-constants 两条**(RFC 权威类候选):session_idle_minutes=60 /
  session_idle_poll_seconds=60(实现值见 naga/sessions.py 模块头)。
- **实现偏差记录**(供裁决):①sessions 表按冻结 §5 DDL 原文落列,但
  branches/workspaces 两张 FK 目标表在实现中不存在(SQLite 默认不强制
  FK,列值恒 NULL——与实现侧简化 schema 的一贯偏差);②存量
  session.open 事件(无 lane_id)回放时 lane_id 记 "legacy";③关闭后
  重开=新一次 session.open(created_at 重置;微信渠道自身的带
  trust_source 首开保留,重开由 gateway 补发 source=runtime-reopen);
  ④job:* 会话不入注册表;⑤会话级常驻分支未做(每任务分支维持,
  收尾整合蒸馏覆盖记忆角度;常驻形态的崩溃/物化时机设计另批)。

## E'''' 追加(自省挂载,2026-10-31,issue #95)——待批

- **候选 additive kind**:notify.delivered/refused(推送三态目前持久化
  于 state/notify-log.json 状态文件,非账本——入账则可重建,候选记档)。
- **候选 spec-constants**:notify_pending_max=5(已在 naga/notify 落值)。
- **偏差记录**:①system_status 对 guest 可见(R0)但内容按 §10 矩阵过滤
  ops 细节(工具可见性与内容可见性两级);②job_create 工具默认预算/墙钟
  取 job_default_* 常量,不经 WS 面的参数覆盖;③元事实注入为静态两行
  (L3 元对象清单的最小形态,动态清单待元对象资产化)。

## E. 建议处置

1. 本 RFC 并入 #23 批设计评审:A 节 4 条 kind 按 additive 入 §3.1 枚举;B 节 7 条入
   spec-constants.yaml v3;C/D 节偏差三条裁决后回写实现。
2. 冻结检查器同步:kind 枚举检查规则(若 R14 覆盖)与 yaml 版本号随评审更新。
3. 在案证据:.context/register.md 2026-10-10 各批次条目(真实微信流量/账本 seq)。
