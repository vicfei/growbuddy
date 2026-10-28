# 上游问题登记簿 · 沪新合并版（2026-09-07）

> **合并来源**：
> - 沪（U-01~U-12）：`.context/upstream-issues-register-20260907.md`（git 权威版 kbpaamc/feature/weighted-ranking docs/ @ 5837b0d6，md5 已核对一致）
> - 新（S-13~S-27）：新加坡节点第 4~5 棒实测/源码定位（#3022 修复、bocha PR、权限评审、时效分析）
> **维护规则**：沿用原登记簿（证据必须 file:line/日志/复现步骤；回馈前先在 upstream/main 复现；分支从 upstream/main 切；密钥自查）。
> **基线**：upstream/main = 647848f3（2026-09-07）；原登记簿证据的 5 个核心文件在 3d3bb7f6→647848f3 偏移段（34 提交）零改动，行号漂移风险低。
> **本文件为追加合并产物，未改动原登记簿。**

## 状态速报（2026-09-07 晚）

- **PR #3069（#3022 消息级图片代理 403 修复）已合并**（05:21 创建 → 07:43 合并，2.4h）——S-01 主项闭环
- **PR #3036（bocha 搜索引擎）已合并**（09-05 创建 → 09-07 12:06 合并）
- PR #3062（opencode 插件）仍 open 无 review
- 时效参考（1200 PR 样本）：长尾外部合并中位 0.33 天，拒绝中位 4.2 天，未决晾置中位 38.8 天

## 合并总览（U-xx 保留沪编号，S-xx 为新增；★=PR 可行性）

| # | 优先级 | 问题 | 类型 | ★ | 状态 |
|---|---|---|---|---|---|
| U-01 | P0 | chat 链路不接 rerank，精排死代码 | 设计缺陷 | ★★★ | 待回馈（旗舰，issue 先行） |
| U-02 | P0 | 单路降级分数不归一，复合分饱和 | bug | ★★★ | 待修 |
| U-03 | P1 | compositeScore/applyMMR 双路径重复 | 代码重复 | ★★★ | 热身首选 |
| U-04 | P1 | SSE 偶发不收尾（DB 有答案无 complete） | bug | ★★ | 待定位根因 |
| U-05 | P1 | embedding 批量调用无退避节流（429 连环） | 韧性缺失 | ★★ | 待修 |
| U-06 | P2 | 向量失败静默降级无观测标记 | 设计缺陷 | ★★ | 待修 |
| U-07 | P2 | 会话标题并行抢答+零校验 | 设计不合理 | ★★ | 待修 |
| U-08 | P2 | 前端 streame.ts 白名单重建请求体 | 脆弱设计 | ★★ | fork 已防 |
| U-09 | P2 | 快速问答默认全租户库且不可见 | 产品设计 | ★ | 先 issue 讨论 |
| U-10 | P3 | 引导遮罩拦截/模式切换无引导/图标无 aria | UX/a11y | ★（aria ★★★） | aria 项=理想首 PR |
| U-11 | P3 | status/parse_status/enable_status 语义混乱 | API 一致性 | ★ | 需讨论 |
| U-12 | P3 | ~~KB 质心缓存不持久~~（fork kb_router 特性缺口） | fork 资产 | — | 不回馈上游 |
| S-13 | P1 | 通用 /files 端点 KB 共享授权缺口（#2017，与已合并 #3069 同族） | bug | ★★★ | 待回馈（修法已验证） |
| S-14 | P2 | hydrateProtectedFileImages 把 403 JSON 当 blob（空白图） | 前端 bug | ★★ | 先 issue 确认（聊天 UX 敏感区） |
| S-15 | P2 | 提取图片不建 resource_bindings（生命周期模型断裂） | 数据模型 | ★★ | 先讨论（改摄取管线） |
| S-16 | P3 | TestSkillPythonVerifier 依赖宿主 Python 环境 | 测试脆弱 | ★★★ | 小修可提 |
| S-17 | P3 | swagger 全量重生成混入无关漂移 | 工具链 | ★ | 记录在案 |
| S-18 | P2 | 新 provider 需 4 处独立接线（枚举+元信息/适配器/container/服务层双 switch） | 注册点散射 | ★★★ | 单一事实源改造可提（bocha PR 实证此坑） |
| S-19 | P3 | web_search AND 语义（agent&&请求级）静默不搜 | 语义陷阱 | ★★ | 至少日志/文档 |
| S-20 | P3 | agent 模型绑定隐式约束（KnowledgeQA 同租户+rerank 必配）报错不指向原因 | 错误提示 | ★★ | 小修可提 |
| S-21 | P3 | web_fetch 对 wN 缓存 ID 报 invalid_url | 工具层怪癖 | ★ | 记录在案需定位 |
| S-22 | P2 | scope=portal 语义="当前租户∪共享"≠用户全空间 | API 语义 | ★ | 需讨论 |
| S-23 | P2 | identity bridge 无单空间移除/批量对账 | 能力缺口 | ★ | 需讨论 |
| S-24 | P2 | 共享列表 N+1（逐 share GetTenantMember）+无分页缓存 | 性能 | ★★★ | 可修可测 |
| S-25 | P3 | 授权无有效期/审批态/per-user 粒度 | 模型表达力 | ★ | 需讨论 |
| S-26 | P2 | bridgeLoginResponse omitempty 剔除 is_new_user:false | API 契约 | ★★★ | 小修好提（我们实测复现过） |
| S-27 | P3 | /leave 自离旁路 + Owner 专属管理 → 双写漂移 | 权限旁路 | ★ | 记录在案 |
| S-F1 | fork | golang-migrate 编号静默跳过陷阱 | 结构性 | — | fork 运维债（FORK-MIGRATIONS.md），不回馈 |
| S-F2 | fork | U-12 同项（质心缓存） | — | — | 同 U-12 |

## 新增项证据要点（详细过程见 sessions/handoff-20260905-realitycheck.md 09-07 两节）

- **S-13**：issue #2017 报告通用 `/files` 端点 wiki 场景同类 403；#3069 修复的 KBAccessRead 回退模式可直接复用；#3069 已合并=该修复方向获官方认可，PR 可行性上调。
- **S-15**：实测 KB 文档提取图片（kind=image）无 resource_bindings 行，仅 source_file 绑定；knowledge_delete.go 靠扫 chunk 文本反查 URL 删图；#3069 修复被迫绕行消息引用链。
- **S-18**：bocha PR 实测 `/types`+`/test` 全过但 POST 创建被拒 `invalid provider type: bocha`——`web_search_provider.go` 的 isValidProviderType 白名单与 validateProviderParameters switch 是第 4 接线点（大小写敏感 grep 还会漏常量名）。
- **S-24/S-26**：见 research/permission_single_source_review.md（2026-08-26，全 file:line）：kbshare.go:283 N+1；identity_bridge.go:36 omitempty。
- **S-16**：2026-09-07 实测 `go test ./internal/...` 唯一失败项，stash 后基线同样失败（Python 3.12 venv 行为差异）。

## 执行顺序（合并版，沿用沪建议+新项插入）

1. **U-03**（零风险热身）→ 2. **U-10 aria**（首 PR）→ 3. **U-02**（小修+单测）→ 4. **S-13**（修法已被 #3069 验证，快赢）→ 5. **U-01**（旗舰，issue 先行带评测数字）→ 6. **U-05/U-07/S-26/S-24** → 其余按"先定位/先讨论/记录在案"处置。

## 新加坡侧未完全理解项（动手前需补课）

1. **U-04 根因**：仅掌握症状（DB 有答案、complete 未发），流式 writer 异常路径未读——沪也标注"待定位"，需先加日志复现。
2. **U-01 细节**：search-only 入口的 RerankModelID 自动解析（session_knowledge_qa.go ~:905-918）未读原文；fork 的评测报告数字无法在本节点独立复算（无 100 题池环境），issue 引用时注明来源为 fork 实验。
3. **U-02 细节**：retriever.EngineAwareNormalizer（knowledgebase_search.go:249）具体行为未读。
4. **U-12/fork kb_router**：质心缓存实现零接触——fork 资产，不回馈，无需补课，仅登记。
5. 沪证据行号基于 fork feature/weighted-ranking，upstream/main 已验证零漂移（34 提交不涉 5 文件），但逐项动手时仍以 upstream/main 行号为准。

---

## 【2026-09-08 归属勘误】（用户质询 bridge 归属后对 upstream/main 逐一核验）

- **勘误起因**：S-26 原证据锚在 fork 的 identity_bridge.go:36，未按纪律先锚定 upstream/main。
- **核验结论**：
  - identity bridge 全家（6 端点 / external_identities 表 / UUM 映射）**upstream/main 零存在**（文件、符号、表迁移全无；FORK-MIGRATIONS.md 明列 20260821154919 external_identities 为 fork 自有迁移）→ 属二开资产。**S-23（bridge 能力缺口）、S-22（scope=portal）随之移出上游回馈清单，归 fork 侧。**
  - **S-26 缺陷本身上游独立存在**：`internal/handler/dto/auth.go:25` `IsNewUser bool json:"is_new_user,omitempty"`——fork 的 bridge 响应即抄自此模板。上游受影响端点：`/auth/login`（auth.go:289/296）、auto-setup（:881/:946）、register-by-invite（register_by_invite.go:228）；IsNewUser=true 出现于 OIDC 首登（user.go:559）。**S-26 保留为上游候选项，但证据需重新锚定：原"两次 bridge 实测"是 fork 侧证据，不随迁；须在 upstream/main 实测 /auth/login 或 OIDC 首登复现后方可提 issue。**
  - 复核其余权限项归属：S-24（kbshare.go N+1）/S-25（无有效期字段）/S-27（/leave 旁路）均为 upstream 代码（#3022 工作期间亲读亲验），不受影响。
- **对排序结论的修订**：S-26 从"可即刻开工（75-85%）"降为"需先 30-60 分钟上游复现再开工"；复现后其影响面从窄端点升级为**核心登录端点**，合并概率评估反而上修。S-18 不受影响，成为当前唯一"即刻可开工"的新增项。
- **教训入册**：跨节点接力的证据必须逐条重锚 upstream/main（本登记簿"维护规则"第 3 条的本意），"评审档案 file:line"不豁免。

## 【2026-09-08 S-26 复现结论：成立，范围第二次收窄】
- **第二次锚点修正**：`AuthLoginResponse`（/auth/login、switch-tenant、auto-setup、register-by-invite）**没有** IsNewUser 字段——昨日勘误把它们列入受影响端点是错的。`is_new_user` 仅存在于 `AuthOIDCCallbackResponse`（dto/auth.go:25）。
- **最终范围**：仅 OIDC 回调。完整链路（全部 upstream/main @647848f3 亲验）：
  ① user.go LoginWithOIDC：仅当回调开户（首登）isNewUser=true，存量用户恒 false；
  ② dto/auth.go:25 omitempty → false 时字段整个消失；③ auth.go:449-460：payload 经
  encodeOIDCCallbackPayload（json.Marshal→base64→redirect #oidc_result=）投递；
  ④ frontend/src 零消费 is_new_user——受众=程序化 OIDC 集成方。
- **复现记录**：pristine upstream/main detached worktree，经上游自己的投递边界函数
  encodeOIDCCallbackPayload 构造两态 payload（测试留档
  .context/research/s26_oidc_isnewuser_repro_test.go）：
  - 首登：{"success":true,...,"is_new_user":true}
  - 后续：{"success":true,...}（字段消失）
  同一端点 payload 形状随登录次数变化，消费方被迫以"缺字段=非新用户"兜底。
- **评级修订**：技术成立但影响面收窄（OIDC 集成者专用，前端不受影响），
  合并概率从 75-85% 下修至约 60-75%；修复仍是一行 tag + 序列化断言测试。
  提 issue 时以"同端点响应形状不对称"为核心论点，附上述两份 payload 对比。

## 【2026-09-08 U-03 已开 issue #3093】
- 验读结论（upstream/main@647848f3 亲读）：重复属实且漂移具体化——agent 版 compositeScore 多
  positionPrior(±0.05 位置先验) chat 版没有（同一 chunk 在 chat/agent 两面排名不同）；applyMMR 两份
  结构不同（chat 朴素扫描+ParallelMap vs agent 增量 maxRedundancy 缓存，agent 注释自证输出等价）。
  两份已共用 searchutil 原语，agent 的 searchResultWithMeta 内嵌 *types.SearchResult → 共享化无障碍。
- issue #3093 已提交（英文，含对照表/合并理由/唯一语义决策点=positionPrior 是否统一）；
  查重干净（#2194 统一检索特性、#1485 去重行为，均非本项）。
- 下一步：维护者认可或 48h 无异议后，从 upstream/main 切分支提取共享包提 PR。

## 【2026-09-08 晚 S-13 撤销：#2017 已被上游修复】
- 复现核验结论（代码级，upstream/main@647848f3）：**#2017 场景已在上游解决，无需回馈**。
  证据链：①前端已有完整 ProtectedFileAccessContext 体系（frontend/src/utils/protectedFileAccess.ts，
  四代理单一真相源，含 knowledgeBase 模式，注释明说解决"共享 KB 内容属主租户对象"）；②三个渲染
  调用点全部传入该模式——WikiBrowser.vue:866（注释原文即 #2017 根因"tenant-scoped /files rejects
  cross-tenant"）、doc-content.vue:809、manual-knowledge-editor.vue:428；③引入提交族=18739bb1
  （消息级代理同一批，v0.8.0 前后）。issue #2017 报告于 UI 0.6.3 时代，属于"已修未关"。
- 启示：S-13 曾被评为"修法已被 #3069 背书的快赢"——但"同族问题存在"不等于"仍未修复"，30 分钟
  现状核验再一次救了提交（否则又要交一记乌龙）。
- 会话/聊天方向候选池刷新（2026-09-08）：#3014 官方已修（closed/completed）；#2706 已被外部
  认领（ktz03 "I'd like to take this"）；存活候选=#320（回溯重复文档，无人认领，评论均为用户
  报告）、#950（agent 循环，深水区）。

## 【2026-09-08 深夜 #320 复现核验：已修未关，撤销回馈候选】
- 复现（官方实例，运行镜像与 upstream/main 相关文件零漂移）：单句 txt + KB 配 summary 模型 →
  解析/摘要完成（1 text 块 + 1 summary 块，ChunkTypeSummary 独立可检索块，knowledge_process.go:1317
  为刻意设计）→ 提问后 knowledge_references 仅 1 条（只命中摘要块），第二轮 0 条——当前管线在
  该场景连"原文+摘要双引用"的数据形态都不再稳定出现。
- UI 层：现行回溯抽屉 ChatReferencesDrawer 经 mergeDocumentReferences 按 knowledge_id 合并同文档
  多块（referenceSources.ts:196/206），上游单测"aggregates chunks from the same document"明确钉住
  该行为；合并逻辑 2026-07 落地（3209ed77/53a66a64），晚于 issue（2025-09-17）10 个月——典型已修未关。
- 评论侧信息：PAPA-L"一个是原文一个是摘要"确认旧症状机理；另两位评论（总参考固定 5 个文档）是
  另一个问题（检索多样性），不在本项范围。
- 结论：#320 无 PR 可提。会话/聊天方向外部池终局：#3022 已由我方完成，#3014/#2017/#320 均已修未关，
  #2706 被外部认领，仅剩 #950（深水）。**该方向的可持续供给=我方第一手登记簿（U-04/U-07 等）**。

## 【2026-09-10 候选池维护：U-05 撤出 + S-16 挂起重验】
- **U-05（embedding 429 退避）撤出候选**：外部 PR #3005「fix(embedding): recover from transient
  failures and resume」（dzc-bit，09-09 开、在审）覆盖同域，避免撞车。
- **S-16（TestSkillPythonVerifier 宿主 Python 依赖）挂起重验**：上游 09-09 连合 #3140/#3143 两个
  skills 运行时修复（runtime prerequisites / installer guidance），其前提可能已被顺带改变，
  动手前必须重锚现状。
- U-03 收官记录：PR **#3150** 已开（09-10 10:00 CST，分支 refactor/unify-ranking-core@6d492ba2，
  #3093 引用评论同发；夜间定时任务卡死未触发，已删任务改为手动执行——cron 机制不可靠，此后窗口
  类动作一律手动）。

## 【2026-09-10 #3142 复现核验：非 bug，按设计工作（第 6 次现状核验拦截）】
- issue 内容：邀请链接注册的用户侧栏无「共享空间」入口，报告者以为邀请用户缺共享能力（v8.0）。
- 核验（upstream/main@b60351f1 代码级）：①菜单门控 `hasRole('admin')` 为显式设计
  （menu.ts:66-78 注释自证「仅 admin/owner 暴露」）；②后端组织操作全量 `g.Admin()` 且服务层
  要求调用者空间=组织所有者（routes_agent.go:97-130），前后端镜像；③邀请注册用户
  TenantProvisioningTenantless 不建自有空间、角色由邀请单必填指定（auth_register_by_invite.go:183、
  tenant_invitation.go:65）；④**报告者真实场景（部门成员建库分享给同事）由 KB 级共享覆盖且
  Contributor 即可**：POST /knowledge-bases/:id/shares 守卫=OwnedKBOrAdmin（routes_agent.go:180），
  agent 同理（:201）；⑤#2627（09-09 才合）与此无关——它只改 invite_only 下"登录后加入"流，
  且报告版本 v8.0 不含该变更。
- 处置：已发中文解释评论（issuecomment-5611638919），含 file:line 证据 + KB 设置分享的正确通路
  +「成员级共享空间」若需要应另开 feature request。
- 入册：外部池新鲜件第 6 例"验后翻空"（#2017/#320/#3098/#3014/#2706/#3142）——先核验后动手的
  纪律持续兑现；下一枪回归登记簿（S-18 仍为即刻可开工首选）。

## 【2026-09-10 U-14 已开 issue #3158】
- 查重干净（#2981=开放窗口配置诉求，根因不同，正文已作 related 引用）；**S-18 评估降级**：模型
  provider 半边被既有 #904（refactor model provider registry）覆盖，若提只剩 web-search 接线切片。
- 证据自包含化：upstream/main 合成测量（每库 ~2KB 严格线性：10 库 20.7KB / 100 库 206KB / 541 库
  1.12MB≈58 万字符），临时测量测试跑完即删；真实部署观测值（541 库→首条用户消息 219,319 token、
  单题 ~260 万 token）以"large multi-KB deployment"口径脱敏引用，无账户/跑批细节。
- 锚点：prompts.go:130-200（无总量上限+description 不截断+FAQ 10 条全文）、observe.go:510-562
  （每轮注入当前用户消息）、engine.go:642（无超窗拒发守卫）；叙事框架="v0.8.0 头版特性 compaction
  的结构性盲区（出生缺陷 vs 累积增长）"。
- 修复双提案：①fail-fast 超窗守卫（止血，~5 行）②目录预算化（N 个全量+其余仅名称、描述截断、
  FAQ 同治）。48h 窗口至 09-12 上午（CST）或维护者回应即出 PR。
- 本日连带发现：#3147（跨会话持久记忆）与 v0.8.0 已发布特性重复（search_memory/画像记忆链路
  代码已验证），可作 15 分钟级答疑；v0.8.0 changelog 中译已存会话（dsh-weknora 官方发布=#3062
  方向背书、context compaction=U-14 叙事支点）。

## 【2026-09-11 深夜 U-14 修复实现完成，PR 待明日窗口】
- 重要插曲：动手 1 小时前上游合入 #3193 附带的 prompts 瘦身（描述 240 截断/FAQ 全文分支删除/文档
  条目仅名）——单项成本 ~2,062→~1,232 B/库（-40%），但**总量无上限依旧**（541 库仍 666KB）。修复
  叙事改为"完成 #3193 开始的工作"
- 实现（worktree /home/ubuntu/weknora-u14，分支 fix/agent-runtime-context-budget，提交 7e9dda5c
  已推 origin）：①formatKnowledgeBaseList 两层预算（全量段 20k 字符→单行降级保 id→硬顶 128k 省略
  +说明；541 库 666KB→110KB 且 id 全保）②engine.ensureTurnFitsContextWindow 发模型前 fail-fast
  （消息+无基线时工具 schema 估算 > MaxContextTokens-安全边际 即终止回合，错误信息给出两个运营
  手柄；未知窗口保持原行为）
- 测试：目录三态（小目录不变/541 降级保 id/3000 触顶省略）+ 守卫四态（超限拒/合式过/未知窗过/
  工具 schema 计入）；整 agent 包全绿，gofmt/build/vet 干净
- PR 正文预存 dualhost-lab/pr-3158-body.md；**明天（09-12）14:01 CST 窗口到期后出 PR**
- U-10 aria 撤出（第 7 例验后翻空候选）：main 前端已有 228 处 aria-label，粗扫纯图标按钮残留 ~1

## 【2026-09-12 深夜续：#3161 认领并修复 → PR #3249】
- 机会扫描终局：#2874 已被认领（根因=Plan3 成员重键，S-24 联动假设不成立）；#2985 认领+产品辩论；
  #3216 认领——**#3161 为唯一无主黄金形状**（外部 issue+file:line 级分析+我方权限域）
- 实现（worktree /home/ubuntu/weknora-u16，分支 fix/shared-agent-self-source@061e3e3b）：
  types.NormalizeAgentSourceTenantID（自引用→absent）+ resolveAgent 入口归一化；外国来源被拒
  不降级守卫原样保留；回归测试四态齐（新增自引用态）+ types 三态单测
- 流程：先发认领评论（不带 PR 号）→ 实现 → PR #3249 拿号 → 回 issue 评论引用；
  PR 正文注明两个同款陷阱兄弟面（shared_agent_access.go / access/knowledgebase.go，属语义加
  而非归一化）留作跟进
- 踩坑：heredoc `cat >> 先建空文件致 || 分支跳过`，测试文件空提交后被抓回（amend 061e3e3b）
- 在途全表：3 合并 + 6 在审（#3249/#3222/#3176/#3150/#3104/#3062）+ #3060/#3061 issue 位

## 【2026-09-14 晚：#3104 合并（第 4 个进主线）+ #3222 静默关闭】
- **#3104 已合并**（09-14 21:02 CST，merge_commit 780f2beb，#2906 随闭 completed）——零 review 直接合，
  累计 **4 合并**（#3069/#3036/#3096/#3104）；我方 ping 发于合并后 1 小时（巧合，无害）
- **#3222（U-14 修复）被静默关闭未合**（12:51 UTC，零评论零 review）；main 无替代实现；#3158 issue
  仍 open——已发礼貌询问（issuecomment-5665442061，提供三种重做方向：守卫位置/配置化限值/拆两 PR）。
  这是外部 PR 第二例静默关闭（#3184 同款），若获回复可校准我方对 agent 核心域改动门槛的认知
- #3249（#3161）/#3222 重跑前 CI 均四绿；#3150 空提交 499bcc5c 已推、CI 重跑中
  （#3199 关闭未合，rebase 救援路线失效）
- 观察补充：同期外部 bug 修复流动良好（#3156 ES 负余弦→f8daa8d7、#3115 qdrant→86f09034）

## 【2026-09-14 深夜：豆包搜索 provider 已开 issue #3250】
- 新增项（编号 S-28）：火山豆包搜索 Custom 版接入（feedcoopapi web_search，Bearer JSON）——实测
  三优势：服务端日期区间过滤 / markdown 全文 / 每结果 ISO 发布时间，单价 ~0.02 元为带正文国内最低；
  WebSearchResult 模型零改动直映；TimeRange 走 bocha freshness 同款 ExtraConfig 模式
- issue #3250 已开（英文，字段映射表+请求样例，key 全脱敏）；48h 窗口至 09-16 深夜，届时出 PR
  （4 接线点+doubao.go 适配器，bocha.go 同构模板，半天工作量）
- 定位：黄金泳道——provider 增加已被验证两次（#3036 两日合并；官方 v0.8.0 持续加 exa/metaso）

## 【2026-09-14 深夜：S-28 豆包 provider 全流程闭环 → PR #3251（未等 48h）】
- 决策记录：经用户质询确认 48h 纪律系我方自制（官方零要求，CONTRIBUTING 无此规定；bocha 先例
  issue→PR 间隔 24 秒、2 天合并）——provider 增加泳道不等窗口，直接出 PR
- 实现（worktree /home/ubuntu/weknora-doubao，分支 feat/doubao-web-search@b3c3ac7f）：适配器
  doubao.go（253 行：Bearer/硬编码端点防 SSRF/time_range 校验/need_content 默认开/Summary 优先
  Snippet/8MB 响应帽）+ 六文件接线（types 常量+元信息表/service 双 switch/container 注册/前端
  联合类型）+ 测试 6 项（请求形态/映射/校验矩阵/HTTP 错误/空 time_range）
- **真 API 冒烟通过**：3 结果、时间窗过滤生效（结果全在 09-12..14 窗内）、全文 7.6k-17.3k 字符、
  +08:00 时区发布时间正确解析（key 从附件读入 env，不打印不落盘，临时测试已删）
- 两次自我纠错：①测试直构结构体绕过构造器致 needContent 零值（按 bocha 模式显式传参修复）
  ②python 静默补丁失误（container.go 缩进 3-tab 不匹配实际 1-tab，replace 无声跳过但打印 ok）
  ——**教训：批量补丁后必须 grep 验证每个目标行**
- PR #3251 已开（Closes #3250）+ issue 引用评论；TestSkillPythonVerifier 失败已证 pristine 同挂

## 【2026-09-15 凌晨：#3251 补课完成——对齐 bocha 验证水位】
- 补三项：①swagger 三文件手术增量（断言护栏拦截一次缩进不匹配后修正；grep 逐点验证）
  ②服务层 TestValidateProviderParametersDoubao（镜像 bocha 形态）③集成 E2E 7/7：
  /types 下发（config_fields=[time_range,need_content]）/原始凭据真搜（时间窗生效）/持久化创建/
  **凭据不回显**/存储凭据真搜/删除/测试租户清理——分支二进制换壳官方镜像，复用 milvus 测试栈
- 编译配方沉淀：golang:1.26-bookworm 容器内需 apt 装 build-essential+libsqlite3-dev+curl 后先跑
  cmd/download/duckdb 再 go build ./cmd/server（宿主直编因 glibc/CGO 头文件双重不可行）
- 临时资产全清（二进制经容器 rm、换壳镜像 rmi、e2e 脚本/override 删除）；key 经 0600 临时文件
  中转一次性使用（附件原文已在会话明文出现过，未落仓库未回显）
- bf40aed2 已推 PR；验证记录已贴（E2E 7/7 + swagger + dispatch test）

## 【2026-09-15：S-28 手工 UI 实测通过，#3251 验证链闭环】
- 用户全链路手工测试通过（下拉/表单/连接测试/持久化/真实问答带引用，5 截图为证）；
  手工验证评论已补发 PR #3251。CI 6/6 绿、mergeable clean、待 review。
- 测试栈已拆（卷保留）、临时镜像已清、官方栈已恢复。S-28 状态：**实现+验证完毕，在审**。

## 【2026-09-15 续：S-28 追加修复——config_fields 前端缺 text 分支（6ed112a5）】
- 用户 UI 实测发现 time_range 无输入框→定位：前端渲染器只有 select 分支（上游框架缺口，
  main@1ef38fdb 同缺）；doubao time_range 是首个 text 字段
- 修复（方案 A）：v-else 文本输入 +5 行并入 #3251；type-check 零错+322 前端测试过+
  CI 6/6 绿。衍生候选：S-18 同族（注册点散射）之外的"前端 config_fields 类型系统
  不完整"已随本 PR 补全，无需单独立项

## 【2026-09-15 issue 普查：新增候选 S-29（#3269 OBS path-style）】
- **S-29 = #3269**（09-15 新开，bug，0 评论未认领，查重干净）：OBS 驱动硬编码
  UsePathStyle:true（obs.go:55/:94）+ resolver HostnameImmutable:true，华为云 2023-12-30
  起禁 path-style → PutObject 404。报告者自带根因+真实日志+OBS 访问日志实证+4 点修复
  建议（含 checksum 对齐 s3.go:71、GetFileURL 虚拟主机化、补零覆盖 obs_test.go）。
  上游核验 ✓（两处 flag 实锤，s3.go 有对齐范式）。评级 ★★★★★ 黄金泳道（#3069 同款）。
  实现注意：HostnameImmutable 必须同步放开，否则 SDK 无法做 bucket 前缀注入；
  私有 IP endpoint 场景需防御性判断（参照 s3.go 启发式）
- #3216（IMA 子文件夹）：报告质量高但今日已被外部 Kaustubh1235 认领，让道；两周无进展再评估
- #3223（PDF 数字丢失/表格错位）：报告极规范（内嵌复现脚本+SHA256+容器已复现），
  但 docreader 布局重建属深水区，列为 TIER-2 备选
- 其余新开（#3258/#3259/#3262/#3266/#3271/#3255/#3236/#3263/#3260）均 feature 讨论类或
  信息不足，无直接 PR 机会
- 在途刷新：5 PR 零 review 不变；#3222 询问仍无回应；#3216 被认领

## 【2026-09-15：S-29 已认领并出 PR #3273】
- 认领评论 issuecomment-5677378910；实现 worktree /home/ubuntu/weknora-u29（分支
  fix/obs-virtual-hosted-style@deb2107c，基线 upstream/main@90e39777）
- 实现：删弃用 obsEndpointResolver（HostnameImmutable:true 本身就钉死 path-style）→
  BaseEndpoint+虚拟主机；IP 字面量/不可解析 endpoint 防御性回退 path-style；checksum
  对齐 s3.go（WhenRequired）；GetFileURL 无代理分支镜像客户端寻址；两处构造合一
  obsS3Options；新增 obs_test.go 6 测（原零覆盖）
- 验证：6/6 新测过 + file 包全绿 + go build ./internal/... 零错；无华为云凭据未做真传，
  已在 PR 与 issue 邀请报告者复验（其环境即 404 复现机）
- PR #3273（Closes #3269）+ issue 回链评论已发

## 【2026-09-15：#3223（PDF 数字丢失/表格错位）修复方案评论已发，S-30】
- 新增项 S-30 = #3223：分层修复方案已贴 issue（issuecomment-5679013969）——
  PR-1 后处理门控（has_charts 传递+页码首末行位置化，低风险先行）/
  PR-2 布局行对齐否决（同时治数量列误删与词内切割）/
  PR-3 可选数字完整性护栏（行级选择性恢复，非整页回退）
- 我方增量机制发现（超出报告者分析）：词裂=_split_columns 递归在行对齐表格的
  字距全高带二次切割；vector_clips 先于 postprocess 计算使门控零成本；
  postprocess 按页调用=免费首末行位置代理
- 报告者基线 462999ec 的 pdf_parser.py 与当前 main 哈希一致（6e95dc00…），零漂移
- 状态：方案对齐中，无异议即开 PR-1（~1 天量级）

## 【2026-09-15：S-30 PR-1 已出 → PR #3283（CI 全绿 clean）】
- worktree /home/ubuntu/weknora-s30c1（分支 fix/pdf-postprocess-gating@adeadf31，
  基线 upstream/main@90e39777）：has_charts 门控（vector_clips 先于 postprocess 计算，
  零管线重排）+ 页码首末行位置化 + 页序值匹配（值不符宁可保留）+ 旧 axis_run 测试契约
  更新 + 5 新门控测试（含报告者的隔离复现原句）
- 验证：官方 docreader 镜像一次性容器内 test_pdf_router 43 过 3 跳；全套 183 过 12 跳
  1 失败（mhtml fixture 缺失，pristine 镜像对照实锤存量）；CI "Python tests and Go
  client tests" + scan 双绿，mergeable clean
- 测试环境坑入册：docreader 镜像 venv 是 uv 管的（无 pip），装包用
  `uv pip install --python /app/.venv/bin/python`；包根=仓库 docreader 父目录
- issue 回链已发（issuecomment-5679610896）；PR-2（布局行对齐否决）待做

## 【2026-09-15 深夜：S-30 布局层完成——#3283 扩为双层 PR，CI 全绿 clean】
- 布局三修（提交 3da205f3 并入 #3283，单 PR 双层：跨 fork 无法 stacked PR + e2e 依赖门控层）：
  ①表格切分否决（窄侧 span<0.25×宽 + median 行长 2-10 + ≥60% 行与对侧对齐→不切分；
  水印排除用每行字形数而非高度——数量列纵向全长是常态，height 守卫会误杀，实现中实测纠正）
  ②断行改垂直区间重叠（降部字形中心偏移 1/3 字高是 "y p" 断裂真因；零高空格字形按带内判入）
  ③空格阈值加字高下限 max(w×0.4, h×0.35)（窄字形拖低宽度中位→字距成词距是 "1 24" 真因）
- 插桩校准数据：标题 y/p 中心偏 2.8pt、数字字距 3.79 vs 旧阈 3.09、真词距 8.12——参数实证锚定
- fixture：报告者 PDF（SHA 钉死）+ 生成器 + e2e 断言（数字/词/标记/行对应四组）；
  **报告者 reproduce_pdf.py 端到端 exit 0**；50/3 过 + 全套 190 过（1 存量 mhtml 失败已对照）
- 既有契约更新：双栏测试改现实几何（各 40% 宽，旧几何 17% 宽会被新否决误并——原几何失真）
- 产出：#3283 标题/描述已改为双层全量（Closes #3223）；issue 终评评论已发（5680238033）
- 教训：理论先行会在"高窄=水印"上翻车（数量列同样高窄），几何启发式必须用真实 fixture 插桩定参

## 【2026-09-15：#3273（OBS）当天合并——第 5 个合并件】
- 生命周期 ~2.3h（08:00 提 → 10:18 lyingbug 合并），追平 #3069 的最快纪录
- 合并方动态：wizardchen 直接在我方分支追加两个测试提交（PutObject 虚拟主机 URL 断言 +
  lint 限额调整，+156→+239）；竞争 PR #3272（同翻 UsePathStyle）被弃——lyingbug 留言
  "Taking this PR over #3272"，我方胜出原因=修得深（resolver 删除/checksum/GetFileURL/IP 回退）
- issue #3269 随合并自动关闭
- 累计：**5 合并（#3069/#3036/#3096/#3104/#3273）+ 6 在审（#3283/#3251/#3249/#3176/#3150/#3062）**

## 【2026-09-16：U-02 代码级核验——假设半证伪半证实，评论已发 #3280】
- 昨日假设"BM25 原始分 16× 污染混合融合"**被证伪**：fuseWithRRF 是纯排名融合
  （info.Score 被 RRF 覆盖，原始分进不了混合排序）；#3280 反而修复了均一分导致的
  关键词 rank 任意性（fuseWithRRF 假设按序到达）
- **真问题（#3280 将引入的新退化）**：关键词-only 路径（keyword-only KB/禁向量/
  向量库降级）走 deduplicateByScore 保留原始分 → rerank base=16.12 →
  compositeScore 0.3×16=4.8 钳位 → 全体饱和 1.0，模型分区分度清零、MMR 退化为
  纯多样性排序（rerank.go:199/:447-453）
- 评论已发 #3280（issuecomment-5691052527）：指出退化 + 建议源端归一（min-max 或
  score/max），混合路径无影响——合并前修正窗口
- U-02 登记项收窄为"关键词分数进入 compositeScore 前需归一"；教训：issue 开立前
  代码核验再次救命（昨日若直接开 issue 就是乌龙）

## 【2026-09-16：#3249 被关（合并到 #3162）——流程教训入册】
- lyingbug 03:20 关闭我方 #3249，合并治理到 lsasw 的 #3162（09-10 创建，比我方认领早 4 天，
  修在 GetSharedAgentForTenant 层覆盖非 chat 入口，更治本）；#3161 仍 open
- **教训（S-31 流程项）**：认领查重=issue 评论+关联 open PR 双查（GitHub issue 侧栏
  Developers 段）；本次只查了前者，4 天存续的 #3162 被漏看，代价=2h 实现+1 个被关 PR
- 已发体面告别评论+移交四态 resolveAgent 测试资产给 lsasw
- 另：#3313（Notion TPM 限流）已被 Myra718 认领（含 TPM pacing+局部重试计划）——
  **登记簿 U-05 转外部处置**，从候选池划掉
- 战况：5 合并 + 5 在审（#3249 出列）

## 【2026-09-16：#3296（禁注册后登录失败）调查——默认栈不可复现，转报告者问询】
- 官方栈实测矩阵（DISABLE_REGISTRATION=true 生效）：登录 200（直连+nginx 双路）、
  auto-setup 优雅 403 JSON、/auth/config 正确 invite_only——后端/nginx 层无罪
- v0.8.0 与 main 的 compose 完全一致：searxng 默认 127.0.0.1:8888 回环、无 8080 冲突
  ——"SearXNG 404 HTML"只能来自报告者侧端口/代理定制（SEARXNG_BIND/PORT 覆盖或前置反代）
- 已发问询评论（要 devtools 精确 URL/.env 四变量/反代拓扑/docker ps）；附 searxng
  公网暴露的安全提醒。认领保持，等报告者回应再定 fix 还是 works-as-env
- 栈已还原（self_serve 基线）+ 测试账户/tenant 已硬删 + 密码文件已清
- 教训：不可复现时把矩阵证据发出去比硬猜修法诚实——5 项全绿的表比任何理论有说服力

## 【2026-09-17 凌晨：#3324（消息搜索 ILIKE 方言 bug）当日闭环 → PR #3328 CI 全绿】
- 01:00 晚扫发现（14:40 开、10h 零评论捡漏）→ 认领 → worktree weknora-s3324
  （fix/message-search-dialect@036dc5e8）→ 修 → 测 → PR #3328 → CI 4/4 绿 clean，全程 ~1h
- 修法：照 session QueryPaged 范式——Postgres 保 ILIKE，其余方言 LOWER/LIKE；
  过时注释两处更新（repo 签名处 + service message.go:556）
- 测试：SQLite 内存库 3 用例（大小写不敏感命中+标题 join/owner+tenant 双重作用域/
  无命中不报错）——旧代码首用例即 `near "ILIKE"` 语法错，钉死回归
- PR 里登记同族观察项：chunk.go（伴生 PG JSON 操作符）/user.go/organization.go 的
  其余 ILIKE——各自单独核验可达性后再修，不做一刀切大扫除
- 战况：5 合并 + 6 在审（#3328/#3283/#3251/#3176/#3150/#3062）

## 【2026-09-17：#3333（初始化模型 tenant_id=0）当日闭环 → PR #3336 CI 全绿】
- 09:50 晨扫定位 → 认领 → worktree weknora-s3333（fix/initialization-model-tenant@89ae8959，
  基线 upstream/main@f2606629）→ 修复+测试 → PR #3336 → CI 4/4 绿 clean，含一次会话中断恢复
- 机制（全链路亲验）：toModel() 不带 TenantID → CreateModel/repo.Create 原样落库
  tenant_id=0 → GetByID (tenant_id=? OR is_builtin) 谁都读不到 → KB 绑死不可读模型、
  上传永久 processing。修复=CreateModel 前 model.TenantID = kb.TenantID（kb 本就在参数里）
- 测试：stub modelService 双路径钉死——创建路径盖 KB 租户（修复前必见 0）、
  复用路径只更新不重写租户且仅缺位者创建
- PR 附存量修复 SQL（join knowledge_bases 三列 + vlm_config->>'model_id'），
  刻意不做迁移（防误伤运营管理的 builtin 行）；重跑初始化不会自愈存量行——此点在
  issue 回链里明示
- 战况：5 合并 + 7 在审（#3336/#3328/#3283/#3251/#3176/#3150/#3062）

## 【2026-09-17：用户群反馈"幽灵文档"分析（S-32）——裁定：成立】
- 症状：解析失败→UI 删除→KB 计数 4/列表 3→重传"文档已存在"→只能手删 DB 行（v0.8.0）
- 机制链（三处代码实锤，upstream/main@f2606629 亲验）：
  ①删除是异步任务：行先标 parse_status='deleting'（knowledge_delete.go:442），UI 只收
  "删除已提交"+轮询（KnowledgeBase.vue:2176 deleteSubmitted），失败 toast 易错过
  ②任务若死掉（redis 丢队列/worker 崩/重试卡住）或耗尽重试但死信标记被
  "no longer active deleting" 守卫跳过（task.go:497-499）→ 行永久卡 deleting/processing
  ③卡住的行：计数全收（CountKnowledgeByKnowledgeBaseID 无状态过滤，knowledge.go:694）
  且拦截重传（CheckKnowledgeExists 只排除 failed，knowledge.go:366）——deleting/processing
  都命中 → 全部症状闭环
- 弱环：4 vs 3 的列表口径未取到用户实际视图（文件夹/状态筛选可能藏卡），但"计数含幽灵
  +重传被拦"两环实锤
- 关联：#3333（模型不可读→processing 卡死）正是幽灵行的生产者之一；0.8.0 无这些修复
- 修复方向（若开 issue/PR）：a) deleting 超时兜底清扫（找不到活跃任务→标 failed 或幂等
  续删）；b) 重传撞幽灵行时返回行 ID 让 UI 引导"先清理"；c) 单文档删除改同步小事务
- 状态：分析完成，候选 issue-first 项

## 【2026-09-17：S-32 幽灵文档已开 issue #3338（查重后定位残余缺口）】
- 查重发现同族两件历史修复均在 v0.8.0 内：#1699（重试耗尽死信标记，2026-06）、
  #2195（#2192 僵尸可见行修复，2026-07）——用户报告发生在含双修复的 0.8.0 上
- issue 定位为**残余缺口**而非重复：三环机制均在当前 main 亲验——去重只排 failed
  （processing/deleting 均拦重传）、计数无状态过滤（4 vs 3）、死信标记有
  "still actively deleting" 守卫（重解析竞态/状态移走即跳过→行永卡非 failed）
- 附加核验：随包 redis 已开 AOF（队列丢失场景弱化，issue 未据此立论）；worker 崩溃
  中途与守卫跳过是主卡行路径；#3333（模型不可读卡 processing）是幽灵行生产者之一
- issue 内容：机制三环+定案 SQL（请求复现者提供行状态）+ 三修复方向（孤儿清扫/
  撞重传返回行 ID+状态引导清理/超阈值 deleting 免拦的权宜）
- 后续：这是 issue-first 项，若官方认可方向 1/2 可接 PR 实现

## 【2026-09-17：S-32 首片修复 → PR #3339 CI 全绿（用户批准当日自修）】
- 实现（worktree weknora-s3338@1844501a）：CheckKnowledgeExists 基础过滤
  failed → NOT IN (failed, deleting)；比 issue 原案（陈旧阈值）更简——
  语义论证：deleting=行将消失，重传与活删除竞态在任务任何结局下都恰好剩一条活行
  （任务按行 ID 计划删除，不会吞新行；耗尽→标 failed 本就不拦）
- 测试：TestCheckKnowledgeExists_ParseStatusMatrix 五态矩阵（failed/deleting 放行，
  pending/processing/completed 仍拦）× 双路径（hash + 文件名/大小回退）；
  踩坑：fixture 漏 file_size 落 0 导致回退路径假失败，补列后全绿
- PR #3339（Refs #3338，首片）+ issue 回链；越界项明示：孤儿清扫（计数膨胀/自愈）
  与前端"清理残留"引导为后续（后者后端已具备——重复错误 data 已带现有文档对象）
- 战况：5 合并 + 8 在审（#3339/#3336/#3328/#3283/#3251/#3176/#3150/#3062）

## 【2026-09-17 下午：战果大丰收——双合并+影响分+接力棒】
- **#3339（04:18）与 #3336（05:09）相继合并**：累计 **7 合并**（#3069/#3036/#3096/#3104/#3273/#3336/#3339）
- **影响分**：我方 #3280 的 composite 饱和评论 → lyingbug 开 #3343（@vicfei 署名）→
  lyingbug 自修（d8be87d94）关闭——U-02 目标由官方实现达成，我方记影响贡献
- **#3328 体面自关**：第三方 coderdailyone 的直提版本 0ad08d41 先落地（含 ESCAPE ?
  子句的更优变体——我方版本预转义了关键字但 SQL 未声明转义符，此差距已学习）；
  #3324 由 lyingbug 关闭
- **#3342 判定版本陈旧**：v0.8.0 tag=09-03，#3069 修复=09-07 不在包内；已回复升级
  指引（docker compose pull），若最新镜像仍 403 才是真残余
- **接力棒 #3345（孤儿清扫）**：lyingbug 亲自开的 #3338 第二片路线图（计数膨胀/
  deleting 无任务自愈/前端 bare toast），0 评论未认领——切片1 两小时合并的线程
  活跃度，是最热车道
- 战况：**7 合并 + 6 在审**（#3283/#3251/#3176/#3150/#3062 + 新开机会）

## 【2026-09-17 晚：#3345 切片 1+2 闭环 → PR #3367 CI 全绿】
- 实现（worktree weknora-s3345@bc17b359，净 +281/-2，9 文件）：
  ①计数对齐：CountKnowledgeByKnowledgeBaseID 排除 deleting（镜像列表既有过滤；
  failed 保持计数=可见可操作卡片）——"4 vs 3"根治
  ②Sweep C：housekeeping 第三扫——搁浅 deleting 行（无存活任务）翻 failed 带
  错误信息（与解析扫同一恢复契约；删除执行器按行 ID 幂等，迟到任务不受影响）
  ③新 inspector 探针 HasQueuedDeleteTasksForKnowledge：匹配 knowledge:list_delete
  批量载荷（解析侧匹配器结构性看不见——任务类型集不同+复数字段）；接口/noop/
  asynq 三实现+三个测试 fake 全补；Lite noop 返 false 属设计（内联删除不入队）
- 失败安全矩阵：活任务→保留；探测错→该行推迟；nil inspector→整个 Sweep C 推迟
- 测试：Sweep 五态 + 计数 + 匹配器六例全绿；TestSkillPythonVerifier 存量失败
  pristine 对照实锤（S-16 在案）
- 切片 3（前端重复命中引导）刻意留给前端 pass，PR 明示
- 战况：7 合并 + 7 在审（#3367/#3283/#3251/#3176/#3150/#3062 + #3328 已自关）

## 【2026-09-18：#3367 合并（第 8 个进主线）——幽灵文档切片 2 落地】
- merged 2026-09-17T16:55:44Z by lyingbug，squash c8ebb9ed，单提交 +281/-2（我方 bc17b359 原样，
  无维护者追加提交）；#3345（lyingbug 开的路线图 issue）随之自动关闭
- 父 issue **#3338 仍 open**——它是切片 3（前端重复命中"清理残留"引导）的载体；后端 data 已带
  现有文档对象（切片 1 时备好）
- 战况：**8 合并**（#3069/#3036/#3096/#3104/#3273/#3336/#3339/#3367）+ **5 在审**
  （#3283/#3251/#3176/#3150/#3062，全 CI 绿、零 review）

## 【2026-09-18：#3342 报告者复测——升级 main 镜像后 403 仍在，疑似 #3069 残余变体】
- 报告者 zhangmy163 09-18 00:58 UTC 回复：切 main 分支 compose、WEKNORA_VERSION=main、
  pull/down/up 全新镜像后，userA 新建会话命中共享 KB 图片**仍 403**
- 回复前的分流岔路（必须问清，逐条对应 #3069 的放行条件与已知边界）：
  ①403 请求的完整路径——消息级 `/sessions/:id/messages/:mid/files`（#3069 修的）还是其他端点
  ②403 响应体 JSON 原文（错误文案区分拦截层）
  ③是回答内嵌的提取图片还是文档源文件——#3069 证据门（chunk 含 resource://handle）只覆盖
  前者，源文件当时验证即仍 403（刻意留的边界）
  ④共享拓扑：KB 组织共享（HasTenantKBPermission 覆盖）还是共享智能体间接访问（未覆盖边界）
- 处置：诊断问询稿已备（本会话），待用户批准后发；也可本机 main 镜像先行自测 ③④ 两条岔路
- **09-19 已发**：issuecomment-5740907759（用户批准）——四岔路问询（403 完整 URL/响应体原文/
  内嵌图 vs 源文件/组织共享 vs 共享智能体）+ #3069 三重校验背景 + 两条已知未覆盖边界说明；
  等报告者回应定分流：命中共享智能体路径或源文件边界=我方域内新 PR 机会（报告者=内置验证人）

## 【2026-09-18：全量 open issue 普查（375 件）——外部池定量结论与幸存候选】
- 快照 /tmp/weknora-scan-20260918/issues.json；分桶 0-7d 40 / 7-30d 67 / 30-90d 116 / >90d 152；
  Bug 110 / Feature 160 / Question 65 / Other 40
- **外部池对"认领即修"已近枯竭**：机制型 bug 短名单 16 件经双查（评论+timeline cross-ref）后 12 件有主——
  #3362→PR#3366（po-et，**issue 开出后 90 分钟出 PR**：10:47→12:15）/#3151→#3166（dzc-bit）/
  #2957→#2963（LuckVd）/#1998→#2116（014-code）/#2473→#2474+#3254（双 PR 竞争）/#2519→#2582（hjxccc）/
  #2855→#2868（Jonesxq）/#2619→#2940（Jonesxq）/#3021→#3088（helloxjade）/#2966→#2990（baka-world）/
  #1892→#2763（Jonesxq）；#3132→LuckVd 修复中（#3102+新提交）；#2884→Roovelrz 认领
- 关闭定性两件：#2703 已被 #2831（移除 LLM rerank 兜底，lyingbug 关闭理由在案）根治、#3189 归 #3239
  处理——两件 issue 虽仍 open 但无可为
- **幸存候选（按价值序）**：①**#2289**（res:// 别名泄漏进 SSE 聊天图挂，55d c0 零关联 PR——资源/流式
  我方域，与 #3069/#3342 同族；动手前须 main 现状核验）②**#3129**（智谱 webSearchPrime 搜索 provider，
  黄金泳道第三次出击——provider 增加合并率 88%、bocha/doubao 模板在案；MCP 端点适配是新增量，
  需 GLM Coding Plan key 做真实验证）③**#3337**（qdrant collection 自动创建失败→手建后正常，1d 新，
  离线环境迁移触发、repro 不详；我方有 qdrant 测试栈可快速定性）④**#3185**（KB 删除重建后 wiki 图谱
  加载失败——疑为"删除不清 wiki/图谱"已知缺口（#2623/#2337 同族）的 UI 症状，可先诊断评论占位）
- 不抢：#3134/#3306（COS 双件，po-et 已在 COS 路上分析且我方无 COS 凭据）；#2121（wolfkill 已宣告
  fork 修复）；#3260（sandbox/skills 官方自研热区）
- 战略校准：新鲜窗口实测收缩到 ~1.5 小时；扫描价值从"抢认领"转向"诊断定性+我方第一手线程"

## 【2026-09-18：#3129 认领前查重拦截——已有 open PR #3214，候选撤出】
- 用户选定 #3129（智谱 webSearchPrime provider）并提供了 GLM Coding Plan key（存 ~/.glm-coding-key，
  0600，仓库外）；**认领前双查发现 open PR #3214**（tianhaotian，09-12 开，1 commit 15 文件 +533/-5）
- #3214 成色：复用 internal/mcp.NewMCPClient（streamable HTTP+Bearer）、固定官方端点防 SSRF、
  每次搜索独立 MCP 会话、zhipu_prime.go+测试+六接线点+swagger+双文档全齐，扫描流水线绿——
  完整对等实现，非可挤掉的弱竞争者；挂 6 天零 review
- 处置：不发认领评论（#3249 教训的正确执行序：查重在先）；key 保留备用于"验证助攻 #3214"
  或 fork 预采纳两条备选路线，待用户拍板；候选顺延回 #2289
- 注：普查当日 #3129 c0 零评论看似无主——**timeline cross-ref 是唯一可靠查重通道**（本例报告者
  在正文中都没提到这个 PR），再次入册强化纪律

## 【2026-09-18：#2289 现状核验=已修未关（第 8 例验后翻空），验证评论已发】
- 用户拍板转向 #2289（res:// 孤儿别名泄漏进 SSE）→ 查重干净（c0 + 零关联 PR）→ upstream/main
  @c8ebb9ed 全链路亲验：**三路全已剥离孤儿别名**
  - 聊天流式：chat_completion_stream.go:111-112 走 modelContext.StreamDecoder()，末级
    orphanResourceStreamFilter（stream.go）剥 `res://\d+` 且跨 chunk 持有部分后缀防劈叉泄漏
  - agent 流式：think.go:70-71 同款组合解码器（:166 的 OrphanResourceHandles 告警仅剩观测意义）
  - 非流式：DecodeOutputText（registry.go:329 StripOrphanHandles；:177-178 与
    session_knowledge_qa.go:1231 调用）
- 时间线：38c594ae（07-15）仅告警版=issue 所述 → **07-27 三个 modelcontext 提交**
  （5e413c64/66632685/69b79239）引入剥离，即 issue 提出（07-24）后 3 天已修，挂 52 天未关
- 残余（评论中已注明）：剥离仅在发射时，07-27 前持久化的旧消息仍含字面 res:// 无回填
- 已发验证评论 issuecomment-5724353420（全 file:line 证据+建议关闭）；无 PR 可提
- GLM key（.glm-coding-key，0600）保留待用户处置
- 外部池幸存候选只剩 #3337（qdrant collection 不自动创建，需定性）与 #3185（薄报告）

## 【2026-09-19：二扫（快照差分）——外部池出清确认，窗口实测 39 分钟】
- 差分快照 /tmp/weknora-scan-20260918b/（380 open，较前次 +5 新 0 关）
- 新到 5 件：#3383（智能推理跨轮重复渲染，高质量确定性复现）→ **39 分钟内出 PR #3386**
  （hata33，issue 03:10 → PR 03:49）；#3390（wiki taxonomy embedding 绕过批处理）→ po-et
  认领评论+**双 PR 竞争**（#3411/#3422）；#3380（脱敏 feature）/#3401（Windows bug 大杂烩）
  不适合；#3403（图谱 tab 500/2900 节点卡，唯一无主）但属前端渲染性能+分层展示 feature
  设计（feat·前端陷阱簇），非我方车道
- 旧幸存者全灭：#3337 被 LeonPure 认领（认领评论自带根因分析——竞争者水位再抬升）；
  #3185 仍薄报告无主；11 个持道 PR 全部仍 open，无车道释放
- **结论：外部池对我方已无可用候选。窗口从 90 分钟（09-18 实测）缩到 39 分钟（09-19 实测），
  抢新车道正式关闭。剩余真实机会=我方自有线程：#3342（报告者等我们回复，残余变体若
  实锤即我方域内新 PR）、#3338 切片3（前端引导，后端 data 已备）、S-26、S-16**

## 【2026-09-20：三扫——大扫除+三机会浮现（#3342/S-15 黄金位坐实）】
- 快照 /tmp/weknora-scan-20260920/：302 open（380→302，**78 件被批量关闭**=维护者大扫除，
  含大量远古件；#3337/#3362/#3383/#3390 随各自修复落地关闭）；我方 #3296 被 lyingbug 关为
  completed（线程干净了结）
- **#3342 残余变体被第二报告者 Arming03 独立证实**（09-18 06:52，Claude 辅助代码审查）：
  main 上 PDF/OCR 抽图仍 403，根因=①入库缺 extracted_image binding（SaveBytes 只登记不绑定），
  两条授权路径都要 live binding ②merge.go:227 用调用方租户富化，共享库命中 image_info 整体丢失
  ——**①正是我方 S-15（09-07 #3022 期间实测登记"提取图片不建 resource_bindings"），三线合一
  （我方实测+两报告者）。我们的诊断问询（09-19）已发，Arming03 评论提供了分流所需的大部分信息**
- 新到幸存候选：**#3453**（wiki ingest selectRelevantFolders 裸 BatchEmbed 绕过池化，
  wiki_ingest_taxonomy.go:195/:200——**#3422（po-et，09-19 合并）修的是同文件另一处**，
  本件=完成 #3422 未竟工作形状，c0 无认领，今日 05:07 新鲜）；**#3451**（VLM 请求硬编码
  detail:"auto"，MiniMax OpenAI 兼容端点 400，c0 无认领，小修）；死件：#3452（lyingbug 自开自修
  #3445 已闭）、#3426（第三方安全大审计自带 PR #3425，95 文件）、#3442（薄问答）、#3450（feature）
- 在审 5 件（#3283/#3251/#3176/#3150/#3062）无变化，全 open 零 review
- 机会排序：**①#3342/S-15 修复（我方域+两报告者等待+第一手数据，无竞速）②#3453（新鲜，
  窗口小时级）③#3451（小修）**——①为旗舰，②③视用户意愿抢窗

## 【2026-09-20 续：三件套执行——#3342 认领 + #3453 翻空 + #3451 当日闭环 PR #3457】
- **#3342 整合评论+认领已发**（issuecomment-5747978269）：三方证据线（Arming03 审查+我方 09-07
  实测+main@a62a51e6 全链核验）+ 机制链 file:line（SaveBytes 不建绑定 / 两路授权收敛到
  IsReferencedByKnowledgeBase / 其实现注释明言"文本不算证据"=binding-only / #3069 时代不查
  binding→收紧引入回归）+ merge.go resolveParentChunks 用调用方租户查父块丢 image_info（第二
  缺陷亦实锤）；修复方向 A 入库建绑定+存量回填、B 文本证据臂（需维护者拍板，router 注释与
  实现的矛盾已摆上台面）、C merge.go 租户改属主——**A+C 起步**
- **#3453 核验=已修未关（第 9 例）**：selectRelevantFolders 两处已是 BatchEmbedWithPool（注释
  引用 #3390），修复随 #3422（aea51ecb）落地，报告者对的是合并前版本；核验评论已发
  （issuecomment-5747979032）建议关闭
- **#3451 当日闭环**：认领（issuecomment-5747979161）→ worktree weknora-3451（分支
  fix/vlm-image-detail-omit，基线 main@a62a51e6，提交 189204e5）→ VLM Predict 与 chat
  msg.Images 兜底两处去掉硬编码 detail:"auto"（omitempty→字段整个消失，OpenAI 行为不变，
  显式配置仍透传）→ 双包全绿+vet/gofmt 净 → **PR #3457** → 回链（issuecomment-5747990968）
  → CI 进行中（detect-lint 已绿）。诚实边界：无 MiniMax key 未真机验证，已注明并邀请报告者复测
- 战况：**8 合并 + 7 在审**（#3457/#3283/#3251/#3176/#3150/#3062）+ #3342 修复在制（A+C 方案）

## 【2026-09-21：四扫——"issue+自提PR"工业化确认，池面出清，我方主线=#3342 执行】
- 快照 /tmp/weknora-scan-20260921/：316 open（+15 新/-1 关：#3453 随我方核验评论关闭）
- **#3457 CI 四项全绿** clean；#3342 认领后暂无维护者回应（方案 A/B/C 已在桌面）
- **#3342 报告者四岔路答复已到位**（09-20 03:03，早于我方认领评论）：URL=消息级端点、
  错误体="forbidden: file not accessible from this resource"（= access.ErrForbidden，与
  我方机制链的"证据门过、binding 检查挂"完全吻合）、切块视图同样 403——四岔路问询结论
  与机制分析互证，无需再等任何信息，可直接进 A+C 实现
- 新到 15 件中 6 件可看，**4 件已被"issue+自提 PR"模式当场占位**：#3458/#3459→PR#3466
  （linus-liu-web 一 PR 双修，其 #3453→本次三连=工业化 issue+PR 流水线）/#3468→#3492
  （Bruce-Yii）/#3485→#3487（CodeZen-Lizhi 自提）——竞争形态从"抢认领"进化为
  "报告者自带 PR"，外部池对第三方贡献者已结构性关闭
- 无主幸存：**#3461/#3462**（ben0112 的 wiki:ingest 双件——积压并发只有配置值 1/5 的
  触发扇出缺失 + asynq 租约过期任务永久不可回收）——wiki 生命周期我方车道，但属
  深水区（并发编排/asynq 语义），且是 #3222 被静默关闭的 agent/wiki 核心域，风险中高；
  暂列观察，不当快赢
- 其余新件：features（#3456/#3464/#3465/#3478/#3479/#3486 架构讨论）/问答（#3480）/
  官方热区（#3474 skills）——不适合
- 结论：外部池无新增可做项；**我方最优动作=执行已认领的 #3342 A+C 实现**（无竞争、
  双报告者待复测、方案已声明）

## 【2026-09-21：#3342 首片修复闭环 → PR #3493（A 入库绑定 + C merge.go 租户）】
- 实现（worktree weknora-3342@97b856c3，基线 main@a62a51e6，9 文件 +322/-18）：
  - **A**：bindStoredImages（knowledge_create.go）镜像 bindContentResources 范式——Resolve
    →同租户校验→Bind(extracted_image)，best-effort；两条调用线：knowledge_process 图片解析后 +
    triggerManualProcessing（provider:// 形态图片正文扫描看不到，需从 resolver 列表直接认领）
  - **C**：resolveParentChunks 三处租户过滤换 *Only 变体（ListChunksByIDOnly 先例 + 新增
    ListChunksByParentIDsOnly repo/接口方法 + searchutil CollectImageInfoByChunkIDsOnly
    平行函数）；删除 tenant 提取块
- 测试：bindStoredImages 四态（同租户双形态/空白/不可解析/跨租户/Bind错容忍）；merge 跨租户
  回归（tenantScopedChunkRepo 严格模拟 SQL WHERE tenant_id，**stash 反向验证旧代码红**）；
  chat_pipeline/searchutil/service 三包绿（唯 S-16 存量环境项）；gofmt/build/vet 净
- **集成验证（一次性全新栈）**：fresh postgres+redis+官方镜像壳挂分支二进制；踩坑三则入册：
  ①迁移是 file://migrations/versioned **文件系统路径**（镜像自带迁移集停在 96，须挂载分支
  migrations 目录→96→107）；②manual 文档 draft 不处理，发布=PUT /api/v1/knowledge/manual/:id
  （/knowledge 组下，非 KB 路径）；③1×1 探针图被 isIconImage 判为图标删除，须 ≥64×64——
  最终：发布带内嵌图文档 → 日志 Resolved 1 + Bound 1/1 → DB 出现 extracted_image 绑定
  （与 attachment 并存）——死链打通实锤
- **PR #3493** + issue 回链（issuecomment-5754429050）；CI 进行中；存量回填明示为第二切片
  （housekeeping 周期扫 vs 一次性，向维护者征询形态）
- 战况：**8 合并 + 8 在审**（#3493/#3457/#3283/#3251/#3176/#3150/#3062）；worktree weknora-3342
  保留待 PR 迭代；一次性栈容器/网络/二进制已清净

## 【2026-09-21 下午-09-22 早：五扫+六扫——#3493 全绿、#3501 窗口关闭、#3150 过时关闭】
- **#3493 lint 修复当日闭环**：revive 未用 ctx + gofumpt 两处 → bfb7f65e → **CI 4/4 绿**；
  教训入册：提交前加跑 gofumpt（gofmt 不够）
- **#3150（U-03）被 lyingbug 关闭未合**：理由=分支过时（#3381 将 knowledge_search.go 重构为
  search_knowledge.go），但明示"去重仍值得做"——**U-03 对新结构重做=维护者背书的候选**，
  列入下一主任务
- #3457/#3283/#3251/#3176/#3062 全 open 无 review；六扫（09-22 早）：321 open，+3 新
- **#3501（agent-chat 引用缺失）夜里被 vinhchuquang PR #3522 占位**（存活一天后失守）；
  #3509 早被 #3515；#3504/#3494/#3512 当日消化——报告者自提 PR 模式持续
- 新到三件：#3527（MCP ask 会话归属 StorageID 错配）=linus-liu-web issue+自提 PR #3528 死件；
  #3526（重复摄取产生 concept/X+entity/X 双页面，slug 判重带不稳定类型前缀）=同作者 issue
  无 PR 但其自提模式风险极高+涉及 wiki 页面身份语义（设计决策域），不抢；#3516（anydoc 多页
  字段丢失）=外部引擎限制+评论已建议换引擎，不碰
- 六扫结论：外部池无可做项；主任务=**U-03 对 #3381 新结构重做**（无竞速+维护者邀约），
  次选 #3342 回填切片设计、S-26 空闲档

## 【2026-09-22：U-03 重做开工——现状核验 + 设计定稿 + 上海二开对照（三步前两步完成）】
- **现状核验（upstream/main@33c0333e）**：#3381 重构后重复依旧双存——agent 侧
  search_knowledge.go:1010 compositeScore（含 positionPrior，clamp 在乘后）+:1043 applyMMR
  （增量 maxRedundancy 形态）+三个纯委托方法（clampFloat/tokenizeSimple/jaccard）；chat 侧
  rerank.go:439 compositeScore（无 prior）+:458 applyMMR（朴素扫描+ParallelMap 分词+
  pipelineInfo 日志+avgRedundancy 仅用于日志）；chatManage 参数仍疑似未用。差异图谱与
  09-09 原始分析一致——旧设计可整体复用，仅需重锚
- **设计（行为零变更纯去重）**：searchutil.RankingCore：①CompositeScoreRaw（0.6/0.3/0.1+
  web_search 0.95，不 clamp——chat 直接 clamp，agent 乘 PositionPrior 后 clamp，两侧位级对齐）
  ②PositionPrior(startAt,endAt)（agent 专用因子集中暴露）③泛型 ApplyMMR[T]（增量形态，
  返回 selection+avgRedundancy；tokens/score 经闭包注入——chat 保留 ParallelMap 分词+日志，
  agent 保留串行分词）；调用侧删三委托+未用参数；测试=金值运行时计算+朴素vs增量差分模糊
  （300 例选择序+平均冗余逐位一致）
- **上海对照（SSH 实读 feature/weighted-ranking，已并入沪 main）**：
  - 沪 theme-3 v1（8a77b068c）：types.RankingConfig 可配置权重+recency 半衰期+MMR λ，
    未设字段回落旧常量（"enable-with-defaults 构造性 no-op"+parity 单测）——**no-op by
    construction 范式直接借鉴到我们的去重 parity 测试**
  - 沪**刻意不动 agent 侧 composite 副本**（"intentionally unchanged in v1"）——与上游
    去重互补：我方统一核心落地后沪 weighted 变体可挂到共享核心上不再度分裂（PR 里作为
    future work 一句话点出，不实现）
  - **沪三臂评测（27548b2ea，eval-100 池模式）**：rerank 是最大单杠杆（top3 +12.7/EM +6.1）；
    **MMR λ0.7 是功臣**（λ1.0 回吐 top5 −4.5——多样性惩罚防近重复挤占名额）；RRF 基线分
    仅 0.008-0.011 → 旧公式 0.3×基线项贡献≈0.003，**权重再平衡是死胡同，有效旋钮=接不接
    rerank 和 λ**——即：统一必须位级保真 λ0.7 与公式，任何"顺手调优"都是错的方向；
    v3 发现 chat 池模式此前静默跳过 rerank（=U-01 根因的沪侧实证）
  - 不汲取进上游 PR 的：RankingConfig/recency/请求级开关/前端开关（fork 实验域，且沪评测
    自证权重面无收益）；参考文档 docs/design/weighted-ranking-deep-dive.md（217 行）在沪仓

## 【2026-09-22：U-03 重做当日闭环 → PR #3533（CI 跑动中）】
- 实现（worktree weknora-u03b@6cceb069，分支 refactor/unify-ranking-core-2，基线 main@33c0333e，
  5 文件 +358/-145）：searchutil/ranking.go 三件套（CompositeScoreRaw 不 clamp + PositionPrior
  集中暴露 + 泛型 ApplyMMR 增量形态/调用方注入 token sets/返回平均冗余）；chat 侧保留并行分词
  +pipeline 日志、删未用 chatManage 参数；agent 侧保留串行富化分词、删三个一行委托；λ/权重/
  日志零变更
- 测试：parity（两套旧公式逐字保留为参考预言机，全矩阵 scores×sources×positions 位级复现）+
  300 轮随机差分（选择序+平均冗余 1e-12）+ agent 包既有朴素预言机测试改跑共享核心原样通过；
  实现中自查抓到一个真 bug（avgRedundancy 误用循环下标索引 tokenSets，应为选中项原始下标）
- 门禁：gofmt/gofumpt（吸取 #3493 教训先跑）+build+vet 净；searchutil/agent tools/chat_pipeline/
  application service 全绿（唯 S-16 环境项）；本机 gofmt 与上游 CI 有版本偏差（3 个上游自有
  测试文件被本机 gofmt 标记，非我改动，CI 侧绿——已备注）
- **PR #3533** + #3093 回链（issuecomment-5770368083）；PR 正文致谢关闭 #3150 的指针并注明
  "第二轮"；future work 一句话：统一核心形状已为可配置加权变体留好接口（沪侧协同）
- 战况：**8 合并 + 8 在审**（#3533/#3493/#3457/#3283/#3251/#3176/#3062）；worktree weknora-u03b
  保留待 review

## 【2026-09-22 下午：七扫——#3493 合并（第 9 件！）+ #3533 lint 修复 + 池面仅存 #3539】
- **#3493（#3342 首片：提取图片绑定+merge.go 租户）已于 09-22 09:52 UTC 被 lyingbug 合并——
  累计 9 合并**（#3069/#3036/#3096/#3104/#3273/#3336/#3339/#3367/#3493），零评论直合；
  **#3342 存量回填第二切片随之解锁**（PR 正文已声明 follow-up，合并=默许）
- #3533（U-03 重做）golangci-lint 红：ineffassign 抓到测试预言机的 ineffectual default 赋值
  ——f3a3d07d 修复已推，CI 重跑中（教训二连：本地把 ineffassign 类也预跑一遍/上 golangci-lint）
- 七扫（324 open，+7 新）：**四件高价值新件全部自带 PR**——#3558/#3559/#3560（rerank 静默跳过/
  分批退化/FilterTopK 硬截断，linus-liu-web 三连自提 #3564/#3561/#3565）、#3551（generic 行
  思考档位失效，DerrickLinus 自提 #3552）；**唯一无主=#3539**（process_config chunk_size 无
  数值下界，实测放大 28.8 倍饿死共享队列——knowledge_process_config.go:258 校验缺口+chunker
  仅 <=0 回退，tokg-venv 报告，7h 零认领）——管线域我方车道，修法=ValidateProcessOverrides
  补数值边界
- 其余新件：#3532（wiki 跨库搜索 feature）/#3536（paddleOCRVLTimeout 常量）不适合
- 在审 7 件：#3533（CI 重跑中）/#3457/#3283/#3251/#3176/#3062；#3493 出列

## 【2026-09-22 傍晚：#3539 当日闭环 → PR #3566 CI 四绿；#3533 排障记】
- **#3539（chunk_size 无下界）当日闭环**：认领（5777497370，含三处缺口核验）→ worktree
  weknora-3539@4443f311（基线 main@0a99a285，3 文件 +103）：共享校验器
  validateChunkingSizeBounds（[100,10000]+overlap≥0，镜像向导 binding）接线三处（上传级
  ValidateProcessOverrides/KB 创建/KB 更新——后两处此前零校验且持久化），越界 BadRequest
  而非静默回退，零值=未设置直通；测试=九态边界矩阵（含实测值 1）+上传级端到端；
  follow-up 明示：总量上限（建议②）/父子块尺寸/preview 只读端点；**PR #3566 CI 四项全绿**
  → 回链（5777608515，邀请报告者贴实验脚本做②的回归基础）
- **#3533 排障二连**：ineffassign 修复（f3a3d07d）后 lint 仍红——lll 报的是我没碰过的
  handler/knowledge_spans_test.go，判定=lint 对比窗口把上游新提交（0a99a285 族）长行算进
  PR；**rebase 到最新 main（78977b5d）后 lint 转绿**。教训三连入册：①测试预言机别写无效
  default 赋值 ②lint 红且报非我文件时先想基座漂移，rebase 是解 ③本地装 golangci-lint 预跑
  提上日程
- 战况：**9 合并 + 8 在审**（#3566/#3533/#3457/#3283/#3251/#3176/#3062）；worktree+3539 保留

## 【2026-09-22 晚：上海侧 issue 草稿核验=机制不成立，拦截未投（跨节点草稿也要重锚）】
- 上海会话发来 issue 草稿（大 chunk 数文档详情页冻浏览器，归因"merged 全文视图拼接
  全部 chunks 无大小守卫"，基于 v0.8.0 fork 实测冻屏），要求新加坡投官方
- **upstream 核验四处全部推翻其机制**（v0.8.0@1edcd54b 与 main 双版比对）：
  ①后端 /chunks/:knowledge_id 分页 + PageSize>100 封顶在 v0.8.0 就有（chunk.go:120-124）；
  ②前端 getfDetails 单页 25 条（KNOWLEDGE_CHUNK_PAGE_SIZE），两版代码逐字相同；
  ③merged 视图 mergeChunks(props.details.md) 只拼接**当前页** 25 条——非全量；
  ④xlsx 是 excel 预览型（filePreview.ts register('excel',[xlsx,...])），file 类型详情
  **默认视图=preview 不是 merged**（doc-content.vue watch 自动切换）
- 真嫌疑改判：excel **预览渲染器**（客户端渲染 9MB/2.4 万行表格→DOM 爆炸）——草稿把
  归因写反了视图；且其实测在 fork（feature/agent-lab）上，不能排除 fork 前端差异
- 处置：未投递；需上海补三件事再定：①冻屏时激活的是哪个视图（默认应是 preview，
  devtools/截图）②官方原版镜像是否复现（排除 fork 差异）③文件多大开始冻。
  或者新加坡用官方栈+browser-use 自行复现后再按"excel 预览缺大小守卫"重写 issue
- 教训入册：**跨节点协作的 issue/PR 草稿同样是"接力证据"，投递前必须逐条重锚
  upstream/main**（S-26 归属勘误教训的推广形态）；这次若直接投=一次权威性自伤

## 【2026-09-22 深夜：WSL2 回执+机制终钉——excel 预览三连击实锤，issue 成稿待批】
- **归属勘误**：草稿出自 **WSL2 节点**（生产判读位），非上海——登记更正
- WSL2 回执：认账机制归因错误（教训"数据加载全路径必须验证"其侧入册）；**fork 差异排除**
  （doc-content.vue/filePreview.ts/document-preview.vue 相对 v0.8.0 零 diff，fork 前端改动
  仅门户/标签树）→ 复现若成立即上游 bug；实测细节：国泰海通系 xlsx 9.28MB/31,924 chunks，
  打开详情页后浏览器弹 "Page unresponsive"
- **SG 机制终钉（main 核验，file:line）**：document-preview.vue renderExcel(:241)——
  ①XLSX.read 全量解析整个工作簿（SheetJS 客户端）②workbook.SheetNames.forEach 无条件
  sheet_to_html 全行转 HTML（:262-266）③sanitizeHTML 全量消毒→v-html 一次性注入（:583）
  ——**无行数/体积守卫、无虚拟化、无 sheet 截断**；触发链=xlsx 注册 excel 预览型
  （filePreview.ts:49）+file 类型详情默认 preview（doc-content.vue watch）→用户未点标签即冻
- **证据链双版闭合**：v0.8.0 组件（fork 逐字相同）上实测冻屏 + main 同款代码核验——
  上游 bug 成立；唯一保留=未在官方原版镜像复现（可诚实标注）
- 成稿（excel 预览缺守卫方向，英文+file:line）已备，待用户拍板投递或补自复现

## 2026-09-22 R3 — excel 预览冻屏 issue 投递（WSL2 中继 → 新加坡核验 → 投稿）
- **#3586 已投**：Document detail page freezes then crashes on large .xlsx — renderExcel 无守卫（open）
  - 触发链：filePreview.ts:49 excel 可预览 → doc-content.vue 详情默认预览 → document-preview.vue renderExcel(:241) XLSX.read 全量 + sheet_to_html 每 sheet 全行(:262-266) + sanitizeHTML + v-html(:583)，零守卫
- 用户第一手实测补全（信创 UOS + 360 浏览器）：9.28MB xlsx → 24,283 chunks；截图1=详情抽屉元数据渲染完、内容区空白、已弹"1个页面无响应"（主线程阻塞）；截图2="喔唷，崩溃啦！SIGTRAP"（渲染进程内存耗尽）。解析中/解析后行为一致（与解析状态无关，预览拉原始文件字节）
- 截图含 DLP 水印+券商文档标题，按 #3251 惯例不上传，文字证据成稿
- v0.8.0 UI 形态与 main 有差异（"预览"按钮+"查看分块"链接 vs main 三视图标签），但 renderExcel 组件两版核验一致；此差异不影响机制归因
- 流程复盘：WSL2 初稿机制归因错（合并视图全量拼接）被我拦下 → WSL2 承认并 diff 排除 fork 因素 → 用户补环境+截图 → 双版证据链闭合 → 投稿。跨节点草稿必须重新锚定的纪律再次生效

## 2026-09-23 R4 — 第 8 轮全池扫描（战局质变：自有 issue 进入农场狩猎半径）
- **外部池零机会，贴牌窗口塌缩到 4-18 分钟**：#3596→PR#3597（4min）、#3494→#3499（18min）、#3501→#3522、#3599→po-et 认领（73min）、#3551→PR#3552、#3361→PR#3389、#3403→yaodong-shen 待方向、#3516→tsumon 最小复现中
- **#3586（我方 15:43 投）被 dvd233 于 21:10 抢答 PR #3592**（5.5h）：8MiB 守卫+sheetRows 5001 截断检测+50K cells 上限+五语言+测试，质量高。判定：不硬拼；转"作者立场评审"——我方有唯一第一手复现数据（24283 chunks/SIGTRAP/解析前后均冻），另指出残余缺口：cells 上限在 metrics 检查处，宽表（5001×16384）解析成本在拒绝前已发生
- **#3342 第二片被 tsumon 以 issue #3594 占位**（等维护者确认入口即实现）。判定：放弃竞赛（我方已拿主片 #3493）；维护者定入口后可做 #3493 实现细节的技术输入（CreateBinding OnConflict DoNothing 幂等、同租户校验）
- **#3539 follow-up ② 材料到手**：tokg-venv（issue 作者）23:39 交付真实管线入口放大测量脚本+数据，正是我评论里要的回归基础。gated on #3566 合并
- 在途 7 PR（#3566/#3533/#3457/#3283/#3251/#3176/#3062）均无维护者评论
- 农场阵容确认：linus-liu-web 3h 连发 7 issue（#3558-#3584 批）+ LuckVd 对号自PR（#3605/#3606）；tsumon、po-et、yaodong-shen、ben0112 各占山头。lyingbug 自留 #3512（rerank 协议）且 #3589 models 大重构进行中——models 区域冻结
- **战略修订**：自有 issue 不再是安全供给——issue+PR 必须同日打包（PR 先行更优）；供给转向 follow-up 链预订 + 在途合并推动

## 2026-09-23 R5 — #3539 follow-up ② 当日闭环（PR #3612）+ #3592 作者评审 + 节点死机恢复
- **#3539 follow-up ② 实现并提交 PR #3612**：MaxChunksPerDocument=100k（children+parents 计数，非字节——tokg-venv 实验二证明无结构文本已被 maxProtectedSize 兜底），ProcessDocument + triggerManualProcessing 两条路径 split 后、DB/嵌入前拦截，确定性失败不重试；认领评论已发（引用 tokg-venv 材料），回归测试含放大锚点（3MB 分隔符密集 fixture，chunk_size=1 被拒/512 放行）
- **#3592 作者立场评审已发出**（07:02 落地）：第一手复现数据（24283 chunks/无响应→SIGTRAP/解析前后一致）+ 公开"尝试攻破守卫失败记录"（宽表/全列引用/多表/残余解析成本四点推演，metrics 水密）；不硬造问题，确认 PR 完整覆盖 #3586 范围
- **新加坡节点内存耗尽死机**，中断于 #3592 评审发送（API 已达、评论已落地）；恢复后核验：评审在、PR #3612 CI lint 挂（lll 两行）
- **新 lint 教训（写入方法论）**：lll 配置 tab-width=4——tab 按 4 列计；本地检查必须 `expand -t4 | awk 'length>120'`，裸 awk 按 1 字符计 tab 会漏报深缩进行（本次 5-tab 行 122→128 列漏检导致 CI 一轮往返）。已修（f5fcd115）
- 后台全仓 golangci-lint 因死机未完成；改为本地三件套（gofumpt/vet/lll 正确口径）+ CI 权威

## 2026-09-24 【铁律】对外内容双禁令（用户明令，永久生效，优先级高于一切默认行为）
- **铁律一：禁止 AI 署名**。任何对外公开文本（issue/PR 正文与评论/release 等）不得出现 "Generated with ZCode"、"Generated with …"、🤖 或任何同类 AI 工具署名/水印/落款。署名决定权在用户，默认永远不加。每次提交对外内容前自查三关键词：ZCode / Generated with / 🤖。
- **铁律二：对外留言去 AI 味**。不滥用加粗、不用 emoji/装饰符号（✅⚠️→①②③）；短内容不套模板、不为结构对称列点、不用层层标题；不写客套铺垫；先结论后细节；证据贴原始输出不转述；文风向目标仓库活跃真人贡献者看齐（直陈、密集、朴素）。
- 与其他指示冲突时以本铁律为准。
- **历史审计（2026-09-24）**：violation 源头是本节点 Octop 会话 09-23 在 PR #1001/#1024 尾部加 "🤖 Generated with [ZCode]"，用户当日明令禁止后 Octop 会话已 PATCH 清除并复核；本次独立复扫确认零残留。WeKnora 侧 37 个 authored（10 issue+27 PR）正文+评论+review、Octop/awesome-opencode 7 项+评论+review 全部干净。跨项目 prompt 未曾指示加署名。Octop 登记册已含同款双禁令，两线对齐。

## 2026-09-24 R6 — 内网第三件中继成稿投递：xlsx 样式崩溃 → issue #3637
- **#3637 已投**（open）：xlsx 上传 TypeError: expected Fill——styles.xml 非标（空 `<fill/>` 等），双引擎同灭（builtin ExcelParser 链 `_prepare_xlsx_bytes→fill_merged_cells_xlsx→load_workbook` 无守卫 + markitdown 链 XlsxConverter→pd.read_excel），openpyxl 3.1.5 无升级路径
- **重锚修正**（中继转录变形二连）：①"markdown_parser.py:56"实为 `markitdown_parser.py`（文件名转录变形，同 `_prepare_xlsx_bytes` 双下划线案）；②容器内 repair 调用顺序与 main 略异（镜像 build 差异），以 main git 锚为准
- **关键新发现（中继未覆盖）**：#1528（wolfkill，5月开、9-21 lyingbug 关闭未合并）正是同签名修复，关评论明确邀请"still hit on current main → open an issue with a sample file"——#3637 正是接这张牌；修复建议刻意避开被否的"手写 XML fallback"路线，改为扩展 xlsx_repair.py 既有 zip 级修复模式（维护性异议的对症回答）
- **样例交付**：本地 WeKnora-docreader 容器（openpyxl 3.1.5/py3.10）合成畸形 xlsx，迭代出三种命中形态（空 `<fill/>`/大写 `<Fill/>`/文本内容），取最小 diff 形态做成生成脚本 gist（vicfei/21f84de4）附于 issue——内网原件不外发，合成样例等效且可复现（traceback 与生产逐行一致：stylesheet.py→sequence.py:27→base.py:61）
- 引用三件全部亲验：openpyxl Heptapod #1805（双签名在案）、pandas #40499（崩溃全在 openpyxl 内、Excel 可开）、SO 72276196
- **脱敏**：无公司名/用户名/主机名/DB端点/内部文件名；"云文档系统导出"泛化表述。铁律自查通过（ZCode/Generated with/🤖 零命中），文风按铁律二（模板头沿用仓库格式，正文平实直陈）
- 关联在树前案已引：#2648/#2663（图表序列化）、#3599/#3608（dataValidation sqref，po-et 在修）

## 2026-09-24 R7 — #3637 当日 issue→PR 闭环：PR #3638（styles 清洗双链修复）
- **#3637 投出 2 小时内自带 PR #3638**（新纪律"issue+PR 同日打包"首次执行）；认领评论已发（方案三点：就地替换保索引/快乐路径单次 styles 扫描/双链差异化接入）
- 实现：`xlsx_repair.py` 新增 `sanitize_xlsx_styles`（zip 级，四种畸形形态就地替换为 no-op patternFill，cellXf fillId 引用稳定；干净工作簿返回 None）；链 A `_prepare_xlsx_bytes` TypeError→清洗→重试一次（清洗后字节流转 pd.ExcelFile）；链 B markitdown xlsx/xlsm 前置清洗（挂既有按类型预处理旁，lazy 重试在 MarkItDown 内不可靠）
- 对 #1528 关闭理由（手写解析器维护成本）的对症设计：openpyxl 仍是唯一读取器，零新增解析器
- 测试：docreader 容器（py3.10.18/openpyxl 3.1.5，与 CI 同版）6 新测全绿 + 全套 197 过/12 跳（唯一 error 是容器无 testdata/ 的环境缺项，与改动无关）；compileall 干净
- 发现记录：`<fill><Foo/></fill>`（unknown-child）openpyxl 实际容忍不崩——测试拆两组（三种崩溃形态做锚 + tolerated 单独断言规整化）
- 铁律自查通过（PR 正文/认领评论 grep 零命中，文风平实）
  - PR #3638 CI 一次全绿（Python tests + Go client + 安全扫描，2026-09-24）

## 2026-09-24 R8 — 第 9 轮全池扫描（外部池持续零机会；models 区解冻信号）
- 新批 6 issue 全部出清：#3609→作者自修 PR#3611；**#3625（MCP 图片 ImageContent）一日三 PR（#3630/#3639/#3640）**——农场开始互相复刻，竞争烈度再升；#3636 RFC 非快 PR 材料；#3626 大设计无维护者卷入；#3634 纯提问；#3629 低质量报告（正文零文字全靠截图，图片服务拒收 GitHub 附件无法读；代码面 skill API 全套俱在——/api/v1/skills、catalog install、sandbox skills CRUD——具体缺口无法定位）
- **重要信号：lyingbug 的 #3589 models 大重构已合并（09-23 06:20）——models 区解冻**。#3512（lyingbug 自提的 rerank 协议+补厂商行）随重构落地变为潜在可做区，但属维护者自有 issue，观望优先于抢入
- 我方 10 项在途全部无维护者动静（最老 #3062 已 17 天、#3176 14 天；暂不催——维护者厌催，等 lyingbug 批量审阅窗口）
- #3592（dvd233，修我方 #3586）仍未合并
- 结论不变：外部池结构性关闭；供给=在途合并+自有 follow-up 链（#3637 的 dxfs/.xls、#3539 的 preview 端点）+ 空闲槽 S-26
  - 2026-09-24 补 commit 502fa79b（用户拍板吸收 WSL2 对比结论两项）：①命中日志（计数+复发计量器语义，对齐 sharedStrings 修复日志先例，xlsx_repair.py 增 logger）②never-raise 包装（内部异常→返回 None 原样透传+warning 日志，公共函数拆 _sanitize_xlsx_styles 实现）；新增 2 测试（assertLogs 计数断言 + mock _rewrite_zip 抛 OSError 断言 None），容器 8/8 绿，CI 二连绿

## 2026-09-24 R9 — 第 10 轮全池扫描（质变终局：issue 与 PR 零间隔配对）
- 新批 9 issue：6 个实质项全部出生即带 PR（#3643→#3645、#3648→#3656、#3649→#3654、#3652→#3653、#3657→#3658、#3659→#3660）——**贴牌窗口从 4-18 分钟塌缩到 0 分钟：issue+fixture+PR 三件套流水线**（#3659 作者 Perfecto23 在 issue 里直接附自己 fork 的回归测试和 go test 命令，PR 同小时挂出）
- 池子文风"我方化"：证据密度型标题（"与同函数其他失败路径不一致"/"silently accepts truncated"）批量出现，Perfecto23 一小时两发——多账号 agent 农场在新规模复制我们的作业形态。issue 池正式变成别家 PR 的广播频道
- 剩余两项：#3646（检索引用回溯原文位置，真 feature 但设计面大，等维护者定方向，观察项）；#3655（提问级，四栏同句，扩展名混淆类）
- #3659（xlsx style-only sheet 幻影列）虽在我专业区但已被作者自修——农场连邻近选题都预占
- 我方 10 项在途依旧零维护者动静；#3512 无动静。供给线不变：在途合并 + 自有 follow-up（#3637 dxfs/.xls、#3539 preview 端点）+ S-26 空闲槽

## 2026-09-24 R10 — 牌桌盘点：#3533 收到 lyingbug 评审（漏接两天的活牌，已接）
- **发现机制盲区**：09-23/24 两轮扫描只查了 issue 评论端点，PR 评审在 reviews 端点——#3533 于 09-22 11:59 收到 lyingbug "we'd like to merge it. A few small things first" 评审，两天后才补扫到。教训：**PR 状态核查必须含 reviews + pulls/comments（inline）两个端点**
- 三阻塞项处置：①ineffassign 已在 78977b5d 解决（评审快照早于修复推送）；②lowerASCII 删除换 strings.EqualFold + 测试 oracle 换 strings.ToLower（eea10f3d）；③PositionPrior 注释改为真实语义（ratio 恒正，multiplier ∈ [1.0,1.05]，无低于 1.0 惩罚）。rebase 至 3e8b0bfc，CI 四绿
- **可选项是明牌**："a follow-up issue would be appreciated" → 已提 **#3663**（PositionPrior 语义：ratio=chunkLen/(startAt+chunkLen)，startAt≤19×chunkLen 即满额——实际近恒定 +0.05，残余变化与 chunk 尺寸耦合；三个选项请维护者定方向）
- 评审回复已发（逐项对应+commit 引用+issue 链接）；agent/tools 的 TestSkillPythonPackageRecovery 失败为宿主 ensurepip 环境缺项（登记册既有 pristine same-fail 记录）
- **牌桌全貌（本轮盘点）**：lyingbug 09-22 对全开 PR 池批量评审 ~20 个（含对我方 #3533；#3499 被指"Go 白名单缺 sql 端到端无效"）。剩余牌：#3512（他自提 rerank，#3589 合并后解冻，无明邀）、#3572（讨论型）、#3594（等他确认入口）、#3646（needs-triage 无表态）。我方其余 8 PR 排队等下一批评审窗口

## 2026-09-25 R11 — 第 11 轮机会扫描（#3533 终局 + 维护者整合潮 + 我方 PR 主动对齐）
- **#3533 关闭未合并（superseded）**：lyingbug #3675 落地同一整合（新 internal/reranking 包：rerank+composite+MMR，chat 与 agent 双管线共用），PositionPrior 按我方 #3663 选项 2 删除；我方源 issue #3093 closed as fixed。判定：实质胜出（16 天推动的方向落地 + #3663 的代数直接塑形其设计），形式未合。worktree u03b 已清理
- **#3646 关闭**：lyingbug 自己实现 #3700（引用打开到原文段落）——观察牌被维护者收回自做。近期 UX feature 请求他都在亲自做
- **维护者整合潮**：#3675（rerank 统一）/#3687（recall 正确性+分数尺度）/#3696（stuck processing）三天连发，retrieval+processing 两区大重组。**#3696 动了 #3612 的两个接线文件**——我方 #3566/#3612 已双双 rebase 至 8619bdf3，语义核验（插入点仍在：knowledge_process.go:3984/knowledge_create.go:1393，测试全过）并推送（0812d7f7/820d8e10），CI 自动重跑
- 外部池：#3668→PR#3669 预占；yeliheng-010 三连自供（Notion/Yuque sync bugs）；#3698/#3699（UX 分类/搜索）needs-triage 无表态——按 #3646 先例大概率维护者自做
- **战略读法**：lyingbug 正在把社区提案方向吸收进自己的大重构（#3675 吸收我方排名整合、#3700 吸收 #3646）。我方最高杠杆动作从"PR 实现竞争区"转向：①精确高分析度 issue（#3663 模式——他一天内采纳执行）②避开其活跃整合区的 PR（docreader #3638 安全区、knowledge 校验 #3566/#3612 半安全）
- 牌桌更新：#3594 仍等入口确认；#3512 常备；S-26 空闲槽

## 2026-09-26 R12 — 中秋两日（09-25/26）动静分析：#3638 合并（第10合）+ 维护者插件架构开幕
- **#3638 于 09-25 14:23 合并——第 10 个合并 PR**；#3637 同秒自动关闭。全周期 36 小时（issue 投递→PR→合并），史上最快。worktree weknora-3637 已清理
- **#3176 关闭（superseded）**：lyingbug 09-25 13:12——引号包裹/残留 Markdown 已由 unwrapTitleQuotes + #3633 的 title cleanup 处理；answer-leak 启发式在 main 无法复现，"没有具体案例不愿加"。教训：启发式修复必须有可复现锚点，否则维护者宁可不要
- **中秋不休**：lyingbug 09-25 单日合并 ~30 个 PR（10:47-14:33 一波清了 ~20 个积压，含农场作品 Perfecto23 #3653/dvd233 #3611/DerrickLinus #3552），自创自合 ~10 个
- **插件系统架构开幕（重大战略面）**：#3711→#3716→#3720→#3721→#3724→#3725→#3727 七连发（builtin/declarative MCP+skills/code plugins+Go SDK/Host API+KV/remote runtime+Python SDK/standalone hosts+Redis discovery/sandboxed pages）——两天内 laying 的新地基。后续必然派生：新 issue 群、社区插件生态位、协议文档缺口
- 我方余 7 PR：#3566 clean；#3612/#3457/#3283/#3251/#3062 mergeable:null（大合并后待 GitHub 重算，监控项）
- 新 issue 池 25/26 共 7 个：农场自供 4（yeliheng×2、hahaha×1、tangkh UX×2 待分诊）+ RFC #3708（二次投递）

## 2026-09-27 R13 — 机会扫描 + 四 PR 全量对齐当前 main（CI 全绿）
- **扫描结论**：外部池零可认领（农场 issue+PR 预配对持续）；插件系统七连发已全合入（新地基完整落地，生态层等维护者开牌）；#3750（OneNote 导入）唯一无 PR 真需求但需维护者表态
- **#3251（doubao）重对齐**：三处冲突解（serply 先合，双保留）+ 三轮 lint 波（lll×9→staticcheck/revive×6→导出注释/签名换行）+ **swagger 教训**：本机 swag v1.16.4 重生成会丢契约测试要求的定义——正确做法是恢复 main 文档三件套后外科手术式只加 doubao 枚举+x-enum-varnames（原 PR 的老手法）。fa773ec6 ALL GREEN
- **#3457（vlm detail）重做而非 rebase**：#3589 重构删除 go-openai 后分支不可救——语义核查发现缺陷**在重构中幸存且扩散为 4 处**（vlm Predict/completions Images fallback/responses Images fallback + responses MultiContent 的 orDefault 强制 auto）。基于新架构重写（留空即消失，omitempty 双 wire 类型确认）+ 3 组测试 + 更新钉死旧行为的存量测试 TestBuildRequestBodyImagesAndAuto。a90ae004 四绿，PR 评论已发
- **#3283（pdf 表格量词）**：冲突与上游新 SOURCE_BOXES 行框特性同点——双保留合并（我方参数化调用+上游 line_boxes），容器 49 测过。1bed1b51 绿
- **#3062（opencode 插件）**：README 重构为表格形态——我方段落改为表格行融入（镜像 dsh 行，四语言）。a22656cf 绿
- **新教训**：①rebase 后 CI 以新 main 为 lint 基准，老 PR 代码全部进"新代码窗口"——lll/staticcheck/revive 全套补课；②swagger 生成物冲突永远外科手术、不整文件重生成；③本地导出符号审计正则要含带接收者方法；④awk 行长对 CJK 是字节口径，CI lll 是字符口径
- 在途 8→7 PR 全部 clean 待下一批审阅波（lyingbug 节奏：09-22/09-25 各一批）

## 2026-09-27 R14 — 第 13 轮 PR 机会扫描（合并低谷期；#3796 机制证伪）
- 我方 6 PR 全 clean 零维护者触碰；合并低谷（20h 仅 1 合并，#3794 lyingbug citations 自修）——09-25 大清仓后无新批，审阅窗口时机不明
- 农场新动向：linus-liu-web 转向钉钉数据源全家桶（今日 7 组 issue+PR 对：Validate/连接器/上传文件/表格/Base/wiki 写保护）
- **#3796（RRF 按切片位置排名）唯一无主候选，但机制在当前 main 证伪**：bestRanks→deduplicateByScore 结尾 sortByScoreDesc——每列表先按分排序再取位置，完成序不泄漏。报告者或测于 #3687 之前版本。残余关切=列表局部排名跨不等长列表的 RRF 语义（设计特性）。判定：验证评论机会（po-et 式），非认领机会——等用户拍板再发
- #3803/#3804（SIGTERM 关停竞态）为本账号另一会话 17:42 新发（非本会话）——纳入视野不触碰
- #3797→#3801/#3802 预占；#3780→#3807 预占；#3789（skill 示例 docs）显示插件生态文档位开始被社区填充
  - #3796 验证评论已发（用户批准）：当前 main 机制证伪（bestRanks→deduplicateByScore 内含 sortByScoreDesc，完成序不进排名，#3687 或为分界）+ 残余 list-local RRF 语义问题独立成_thread 的建议 + 请求报告者钉版本。铁律自查通过

## 2026-09-28 R15 — 解决侧分布分析（148 合并 / 171 关闭，窗口 09-20+）
- 核心读数：合并潮=09-24 v0.8.2 发版（#3632 changelog 同步）；171 关闭中大头是 09-20 stale 清档（#98-#1616 上古批量归档），真修复关闭为少数
- 我方窗口内合并 2 个（#3493/#3638），个人贡献者前列

## 2026-09-28 R16 — 接上海节点交接：上游哨位（SIGTERM 竞态线）
- 交接核验通过：/tmp/WeKnora-pr（fix/sigterm-cleanup-race@6b6b9967）、/tmp/sigpkg（补丁+证据+issue 草稿）、~/golang/go 均在位
- 状态快照：PR #3804 open/clean/CI 四绿，零评审零评论；#3803 open 1c（互链）；#3806 open 0c（BrowserSkill 跨轮失效，或落 Tencent/BrowserSkill 仓库）；#3805 已撤回关闭（勿动）
- 职责并入既有看护：双端点纪律照常；**修复所有权在上海**——我方只回澄清类评论、按指令 git am 上海新补丁并推送；信誉动作先经用户确认；小机不做重编译
- 此前 R13 扫描发现的"本账号另一会话"#3803-#3806 即此线（本机受托发布的 GitHub 出口动作），谜底解开
- 定时巡查 cron：上海提议待用户定周期，未设

## 2026-09-28 R17 — WSL2 中继：xlsx 大表守卫调研（重锚四论断全过）
- ①行级切块在 main 实锤（excel_parser.py:118 iterrows→:135 chunks.append）②大表守卫缺失实锤（无任何 row/threshold/guard）③ParserEngineRules 按类型引擎规则在位 ④xlsx_first_row_as_header 在位（knowledge_process_config.go:13）
- 调研增量：calamine/fastexcel（Rust 读取派，样式免疫+提速一个量级，pandas engine 级替换）；SpreadsheetLLM（微软，结构锚点压缩，学术正解）；sheetwise（其开源实现，压缩派 96% token 缩减）vs knowledgestack/excel-parser（保真引用派，source_uri 精确到 Sheet!A1:F18，引用召回 0.889 vs Docling 0.000）；业界共识：xlsx 大表分诊无人做
- **关键定位**：两家借鉴库都站在 openpyxl 上——我们的 #3638 sanitizer 是它们的前置刚需（openpyxl 上游不修、下游生态集体买单，可作跨库 issue 证据）
- 「大表守卫」issue 成熟度跃升：第一手量化数据（9MB→24,283 块、3.4K块/MB vs PDF 百万字 2.1-2.5K、单档 1.5h vs 常规 46-57块/s）+ 业界空白确认 + 学术设计依据（锚点压缩替代简单截断）+ source_uri 溯源差异化方向。与 #3586/#3637 构成 xlsx 治理三部曲第三部
- PoC 场地定 SG（合成工作簿，生产 36 巨兽不出内网）；等用户拍板 issue 成稿投递

## 2026-09-28 R18 — 独立库 xlsx-triage 落地（用户拍板：先建库，WeKnora issue 暂缓）
- **vicfei/xlsx-triage（PRIVATE）已建仓推送**：RAG 分诊层定位——小表行级 / 大表锚点压缩（schema画像+verbatim头部+section锚点+显式coverage），全 chunk 带 source_uri（#Sheet!A1:F18），内置 #3638 移植的样式清洗作为读取前置（never-raise）
- 纯 Python（仅依赖 openpyxl），src 布局，8 模块；25 测试全绿（四畸形形态矩阵/分诊边界含生产规模数字 24283/uri 构造/压缩 token 缩减 >90%/端到端混合模式）；CI workflow（3.10+3.12）
- 许可核查：sheetwise 实体 Apache-2.0（README 标 MIT 是错的）→ 只借鉴思想零拷贝；excel-parser MIT；本库 MIT，README 致谢设计来源
- 测试环境：宿主缺 python3-venv，用 docreader 容器跑（PYTHONPATH=src）；两处测试期望自纠（unknown-child 容忍形态不触发清洗路径 / 坏 zip 构造改 mock）
- **保密纪律**：库计划对 WeKnora 侧零披露（登记册为内部文档）；WeKnora issue「大表分诊缺失」按用户决定暂缓，与库解耦
- 下一步（未执行）：calamine 引擎位预留已留（reader seam）；锚点压缩深化；插件系统 re-land 后做 WeKnora 集成

## 2026-09-29 R19 — 第 14 轮扫描：#3804 合并（第 11 合，上海线首胜）+ 我方旧六 PR 三波未被审
- **PR #3804 于 09-28 15:37 合并（零评审直接合）**：上海诊断的 SIGTERM 竞态修复经我方出口上岸——账号累计第 11 合；#3803 同秒自动关闭；#3806（BrowserSkill）仍零回应。按 handoff 职责：已向用户报告"下次上游同步收入"（上海走官方优先纪律不手动摘樱桃）
- 09-28 14:00-17:32 又一波 ~13 合并（linus 错误吞噬七连+钉钉、yeliheng RSS/Notion/GitLab、VictorMaznyuk、BaiMoHan、dajiaohuang、nullptr）：**该波吃了我方 #3804 但旧六 PR（#3457/#3251/#3283/#3062/#3612/#3566）全部零触碰**——三波模式：每波带走我方 1-2 个新旧交替，旧六排队已 4-20 天
- 外部池持续零机会：linus 又发 issue+PR 七连预配对（错误吞噬家族+分页防护家族）；#3820 embed 400 → #3850 已合 + 两竞速 PR；#3828/#3829 均自带 PR
- 牌桌不变：#3572 用例征集牌（用户已知）、#3172/#3353 增强评论机会、插件 re-land 观察、#3594 等待

## 2026-09-29 R20 — 第 15 轮扫描（外部池连续第五轮零可认领）
- 我方 6 PR 第三波被跳过（09-29 的 4 合并又无我方）——旧六排队 5-21 天，全部 clean 零触碰
- linus-liu-web 14:11 一分钟 13 issue（CopyIndices 四库家族/RebuildLinks/query_knowledge_graph 截断/web_fetch/GitLab 体积/IMA/Notion 分页/OCR 超时/token 颁发/MCP 连接测试）+ PR 随配；dajiaohuang 双自配对；it235 Confluence 自配对
- 四候选出清：#3865（KnowledgeQA usage）被 BaiMoHan 评论认领出局；#3867（关配置后问句向量残留，iceriverlin 二连报，带生产只读统计 63,467 行）无 PR 但落在检索区+#2652 维护者"由检索效果评测决定"先例——设计敏感，只宜验证评论不宜抢修；#3849 无复现素材弱；#3851 贡献者自留可能
- 新个体观察：BaiMoHan（合并+认领）与 iceriverlin（RRF+问句向量两连深报，生产统计型）在成型——池子里的"分析师型个体"在增加
- 牌桌四张不变：#3572 用例/#3172+#3353 增强评论/xlsx-triage 库深化；#3594 无动静
  - 2026-09-30/10-01 快查：合并波持续（09-30 八连：lyingbug AnyDoc 0.2.4 升级+邀请制注册档、linus 13 连开始落地×3、nullptr×2）；我方旧六第四波被跳过；#3849（docx 页眉）已有 helloxade #3914 修复 PR（弱候选自然出清）；插件系统仍未 re-land

## 2026-10-03 R21 — 第 16 轮扫描（国庆静默期；仅两个无主候选）
- **近 48h 零合并**（10-01 07:26 后无合并波）——国庆假期效应；提交流量仍在（farm 预配对照常：linus EMF/WMF 对、icearia0219 工具参数对）。我方旧六第五个周期零触碰（#3062 已 26 天），假期不宜 ping
- 本账号另一会话新线：#3941（技能安装验证 vendored 依赖树）、#3942（沙箱清扫器竞态）+ PR #3943（10-02）——非本会话，不碰
- 无主候选仅二：#3940（QA 脚本默认 gpt-4o-2024-05-13 已下线，一行级修）、#3923（pt-BR i18n——通道已被 Polish/vi-VN/zh-TW 验证可合，但体量大且非母语质量风险，farm 不做 i18n，竞争低）。#3922/#3926 均已被 PR 预占
- 生态信号：#3917 有人给仓库加 docs/agents/* 工程文档——agent 友好化趋势
- 节后预判：假期后大概率有清仓波（09-25 大扫除先例），我方六张 clean 牌在波前就位

## 2026-10-03 R22 — 上海委托独立复核（四提交物）完成
- **#3943（重点）三段复核全过**：①机理证实——20行 Go 验证器实measured 5000 vendored 路径→命令串 340,039 字节>131072（内核头 MAX_ARG_STRLEN=PAGE_SIZE*32 实锚），补丁后同输入 39 字节；②Skill 测试 182 过/1 败（TestSkillPythonVerifier 基线同败=环境性，严格归因）；③**嵌套边界缺口成立**（ParseSkillBundle 的 Files=zip 相对路径，tools/.venv 合法）→ 按授权追加加固 commit 9b40f63b（underVendoredTree 任意深度过滤 + 嵌套回归测试），已推送
- **引文核对全部证实，零乌龙**：#3941 的 for 单参数拼接✅（正文未引 512MB/20000——那是交接摘要自身的数字，20000 与实际常量 100_000 不符但不影响 issue）；#3942 三处逐字✅（sweeper 注释 docker_idle_sweeper.go:16 / makeDir 格式真身 docker_remote_client.go:1046 firstNonEmptyLine / remote_client.go:358 DefaultSandboxExecUser="root"）
- **#3942 机理审计评论已发**：B 排除（Create 有 waitUntilRunning:298 注释记录历史 409、exec 有 ensureRunning:526 门）；A 成立+窗口五步刻画（inspect 过→清扫删→exec 空 stderr→InvalidRequest:1043→CanReplaceRemoteBinding:254 白名单不含→断自愈）；关键增量=清扫器安全声明只对 inspect-404 路径成立，mid-flight 窗口不成立；修复方向一（exec 失败后 inspect 复核改判）改动面更小
- **交接陈旧信息标记**：#3804 于 09-28 已合并（漂移检查无意义）——交接写于合并前
- GitHub 动作：#3943 分支加固 commit 推送；#3942 审计评论；无其他（遵只动授权范围纪律）

## 2026-10-05 R23 — 第 5 棒委托执行（中继推送 + 两处交接乌龙核实）
- **任务一完成**：bundle（78M 全量，verify 通过）中继推送 kbpaamc/main：ce00329c..8573abcf，远端 HEAD 确认 8573abcf（102-sync + fork-v5）。只推 main，未碰 fix 分支
- **任务二两处乌龙（未执行其指令，依据核实）**：①#3804 早已于 09-28 合并（merge_commit=ec0ca463），"评论并 close"无意义；②bccb4b15 实为 embed 修复（与 SIGTERM 无关）；③**反转**：引入 ShutdownBudgets 的 ec0ca463 正是 #3804 自己的合并提交——上海在上游看到的"官方实现"就是我们出口合并的他们自己的最终扩展版（8+ 文件：shutdown.go/shutdown_test.go/双 main/cleanup.go/cleaners，远超初版 +13/-5，说明合并前上海推过大扩展补丁）。无需任何 PR 动作
- **任务三核实**：#3942 审计评论在案（issuecomment-5956982734，2026-10-02T16:45Z，即本会话 10-03 段）——上海"网页端零评论"为缓存/看错，未重复发送
- **任务四巡检**：#3943 open/clean/0c（含我方加固 9b40f63b）；#3803 closed（随合并自动关）；#3941 open 0c；#3806 open 0c。upstream/main HEAD=bccb4b15（09-30）后零新提交——国庆全冻结
- GitHub 动作：仅 kbpaamc 推送。零多余动作（两处乌龙均拦截在执行前）

## 2026-10-06 R24 — 第 5 棒收口委托执行完毕
- **任务一 ✅**：差量 bundle（109K，sha256 校验通过，基点 8573abcf 具备）fetch→push kbpaamc/main：8573abcf..d3636da1，远端确认 **d3636da1a**。FF 安全预检后推送，未 force。注：区间实数 17 提交（交接称 19，小出入无害，HEAD 精确匹配）
- **任务二 ✅ PR #3980 已开**（fix/sandbox-idle-sweep-race@1bb8bb741，Bug3=#3942 机理A的"方向一"修复）：分支复核 2 文件+88/-17 与委托书精确一致；单包回归测试两条 PASS（消失容器→NotFound+换绑 / 真失败→InvalidRequest+保绑定）；铁律自查过；#3942 链接评论已发（issuecomment-5999642917）；**CI 四绿**
- **任务三巡检**：#3943/#3941/#3806 零动静（10-02/09-27 起）；#3942 现 2 评论（我们的审计+链接评论）；upstream bccb4b15（09-30）后仍零新提交——国庆第 7 天全冻结
- GitHub 动作：kbpaamc push、origin 推分支、开 PR #3980、#3942 评论。无异常

## 2026-10-07 R25 — 例行委托：v3.1 分支中继 + 巡检
- **v3.1 分支补推 ✅**：bundle 前提 bb5b37d0 具备→fetch（80a950ad，基点上恰 3 提交：bocha/doubao 紧急回移+DMZ 适配集成测试+P1/P2/P4）→ 新建推送 kbpaamc `backport/v3.1-bocha-doubao`，远端确认 80a950ad9。未碰 main
- **kbpaamc/main 核对 ✅ = 9df00ac72**（10-07 08:05，上海直推的 P2/P4 在位）
- 巡检：#3980/#3943/#3941/#3806 全零动静；#3942 两评论（皆我方）；**upstream 仍冻在 bccb4b15（09-30）**——国庆假期名义上今天结束，维护者尚未回归
- 无异常；两次 jq 引号笔误自纠（不影响任何远端操作）

## 2026-10-12 R26 — 第 17 轮扫描：节后大丰收 + #3612 技术性否决 + 唯一真候选 #4125
- **三连动静**：#3980（Bug3 沙箱竞态）与 #3943（vendored 过滤，含我方加固）10-07 17:28 同分钟合并——**账号累计 13 合**；#3612（chunk 预算）10-07 关闭未合并，lyingbug 技术否决两点（50MB 默认上限下合法大表可超 10 万块会误杀真文档；检查点在 split 后不省内存成本）+ **点名 #3566 为根因正解**——间接背书
- 余 5 PR 全 clean（#3566/#3457/#3251/#3283/#3062）；#3566 分支落后 main 232 提交但无冲突，被点名后随时可 rebase 刷新
- 节后合并波 10-10→10-12 大开：lyingbug 自己连发（gallery 无限滚动/wiki 图谱/OCR 校验/observability/preview 引用）；社区大件：Cohere rerank v2、Claude 5.5 模型行、平台用户管理、docker 交互终端；农场有斩获（Perfecto23 helm/LuckVd paradedb）
- 候选出清：#4129 被 tayadwx 自留（画廊按文档筛图方案）；#4149 是 tayadwx 自己的三臂实验线（agent 治理实体清单方案三）；**#4125（MDX 导入支持）唯一真候选**——正文已查清 Go 白名单 import_filetypes.go + builtin_converter simpleFormats 两处缺口，两天无人认领，.sql 线同族（lyingbug 在 #3499 评审里亲自教过"端到端白名单"课），farm 未碰
- 行动选项待用户拍板：认领 #4125（推荐）/ #3612 礼貌性回复 / 持牌

## 2026-10-12 R27 — 三任务推进完毕（磁盘两度中断均恢复）
- **① #3566 rebase 刷新完成**：b18b10622（7925f774d 基），CI 四绿——lyingbug 点名"正解"后的存在感刷新
- **② #3612 礼貌回复已发**：认可两点否决理由（50MB 数学/后置检查不省成本）+ 提及 #3566 已刷新
- **③ #4125 MDX 支持当日闭环：PR #4182 已开，CI 七项全绿（ba2613d9）**：
  - 五层接线：导入白名单（单源真理覆盖上传/URL/worker/连接器）+ simpleFormats + 双引擎 FileTypes + 前端 DEFAULT_VALID_TYPES
  - MDX 降级器（mdx.go）：行扫描状态机（RE2 无反向引用教训）——import/export 语句行丢弃、JSX/HTML 标签剥壳留瓤、代码围栏逐字保留（围栏内 JSX 不动）、标题存活保 chunking；`a < b` 散文数学安全
  - 测试涟漪三处自纠：镜像断言加 mdx（本就是 PR 的目的）；Seafile 两夹具把"不支持样例".mdx→.exe（.zip 样例在，意图保留）
  - **MCP TestResourceListStopsRepeatedCursors 判定为 main 存量破损**（纯净 c2edec192 本地双复现）——PR 评论已附证据链；首轮 CI 它挂、次轮 CI 它过（CI 侧 flaky/环境差异），PR 侧全绿
- 磁盘事件两度中断（会话执行区满→上海清 docker 缓存 20G+；再次满→再清 18G 在位）；根因：多项目共用节点积累，深层清理建议已列（octop-memory-scan 5.1G、~/go 3.5G、weknora-images 3G、他会在 /tmp 690M+）
- 在途：7 PR 排队（#4182 新 + #3457/#3251/#3283/#3062/#3566 + 上海线 #3941/#3806 issue）

## 2026-10-12 R28 — WSL2 战役委托：裸上游验证完成 + 战役形态修正
- **裸官方栈验证 ✅**（wechatopenai:main 镜像，恢复栈→修测试账号 bcrypt 密码（Go 工具链生成，容器无 pip 的弯路）→builtin 绑 model+rerank（两次 400 报错引导）→三组对照）：knowledge-chat references=1+落库 1 条；**builtin agent-chat 零事件+落库 []+原始流字符串级零出现**；内联 <kb/> 非确定（同栈两次 3 处/0 处）——"唯一通道不可靠"论点实锚
- **查重修正战役**：#3501（09-21 同题 issue）仍开零评论即该立的牌；#3522（社区修复 PR）悬置三周未合并（曾获合并意向+作者已响应评审）；当前 main 代码级定罪：AgentReferencesData 唯一发射点 session_knowledge_qa.go:1331（knowledge-chat 侧），agent 引擎零发射者——fork"自定义有引用"=回移 #3522，builtin 连 fork 也无
- 正确打法：#3501 发证据增强评论（矩阵+上游运行时+代码分析+#3522 中性提示），不开新 issue；docs PR 照做
- 产物：~/java-sse-kit/SG-RESULT-upstream-verify-20261012.md（协调文件，含分工建议：SG 起草+投递、上海把关互锚）+ sse-sample-agent-chat-20261012-upstream-builtin.txt（171 行裸上游原始流）
- 栈已复停（恢复用户暂停态）；测试账号密码已重置为一次性值（登记：opencode-manual@test.local / SseTest#2026，本地测试栈）

## 2026-10-12 R29 — WSL2 裁决执行：两份草稿完成，待上海互锚后投递
- 裁决采纳：不开新 issue、增强 #3501；docs 措辞护栏（agent-chat 差异链接 #3501 注"修复讨论中"，不写成永久契约）
- **草稿一**（~/java-sse-kit/DRAFT-comment-3501.md）：#3501 证据增强评论——中文（线程语言）；结构=当前 main 完全成立且影响面更广（custom+builtin 一律）→代码级机理（唯一发射点+handleReferences 连体）→官方镜像栈运行时对照表→多环境交叉→"fork 自定义有引用系本地增强"澄清→内联标记非确定实测（3→0）+#3522 中性提示与独立佐证→builtin 配置前置
- **草稿二**（~/java-sse-kit/DRAFT-docs-sse-contract.md）：docs PR 补丁——落位 02-api-chat.md（agent-chat 端点后）；三块新增=两接口差异表（护栏措辞）、内联 <kb/> 解析三要点（流式切块聚合/非确定性降级/字段同源）、builtin 绑模型前置（curl 示例）；总览 SSE 节已有信封与事件枚举不重复
- 投递顺序：上海审 #3522 diff+互锚两稿 → SG PAT 投递（评论→docs PR 互链）。当前状态：**等上海互锚**

## 2026-10-12 R30 — SSE 契约战役投递完成（三节点协作闭环）
- **#3501 证据增强评论已投**（issuecomment-6124755690）：上海互锚两处修订落实——①"fork 本地增强"归因撤回换中性表述（上海分支考古证伪：fork 全分支+v0.8.0 官方均无 agent 侧发射器，WSL2 10-08 样本的 references=1 成因未定按红线不提交）②#3522 覆盖面升级为确定性表述（上海审阅：收集逻辑挂通用 ReAct 循环、与工具名解耦、builtin/custom 同路覆盖——合并后两场景一并关闭）
- **docs PR #4204 已开（CI 绿）**：02-api-chat.md +38 行——两接口差异表（"修复讨论中 #3501"护栏措辞×2）、内联 <kb/> 解析三要点、builtin 绑模型前置 curl；PR 正文链 #3501 评论
- 战役三件套落位：#3501（牌，已增强）↔ #4204（docs，在途）↔ #3522（社区修复，获独立佐证+覆盖面确认）；等 WSL2 登记收口
- 流程复盘：双向重锚纪律全效——SG 裸上游运行时+main 行号、上海分支考古+草稿核验+#3522 审阅；"只提交验证过的断言"红线拦下一次归因乌龙（fork 增强）

## 2026-10-16 R31 — 第 18 轮扫描：池面全预占（唯一漏网 #4313）；我方七牌穿越两波未叫号
- 我方 7 PR（#4204/#4182/#3457/#3251/#3283/#3062/#3566）穿越 10-14→16 两波合并全部零触碰；#3522 仍未合（09-23 后无动）；#3501 我方评论后暂无维护者回应
- 大盘：lyingbug 进入产品建造模式——"WeKnora 助手"套件 10+ PR 连发（助手管 KB/agent/技能/文档/FAQ+侧栏+权限）+RRF 设置控件+swagger 真实契约文档；外部评审带宽被自家工程挤压是排队延迟的合理注解
- 池面：新批 30+ issue 全部预占（#4273/4275→自PR、#4329→#4354、#4268→自PR+他人认领、#4243→#4279、#4331→#4332、#4335→旧PR#3498、#4311→#4392）；**唯一无主：#4313**（liouxb：带图/文件请求的审计与记忆写库 varchar(1000) 溢出+base64 全量进应用日志——真实但非我方专域，报告者三连报型可能自续）
- 值记：Greenplumwine #3497 排队 26 天后终于合并——排队耐心有先例可援
- 战役件去向：#4204/#4182/#3522/#3501 四件等波次

## 2026-10-20 R32 — 第 19 轮扫描：预配对覆盖率 100%（含个体贡献者）；七牌穿越第三波零叫号
- 六候选全预占（#4477→#4478、#4458→#4459、#4484→#4485、#4469→#4470、#4503→#4505、#4486→#4487）——**issue+PR 同发已成为全体贡献者的标准工作流**（不再限于农场），"先 issue 后认领"模式正式死亡
- 我方 7 PR 穿越 10-17→20 第三波合并（lyingbug automation/助手权限/多智能体协作大套件 + nullptr 双模式沙箱基础 + 农场多件）全部零触碰；#3062 排队 23 天（Greenplumwine 先例 26 天，30 天时考虑 ping）
- 大盘注解：lyingbug 持续自家产品建造（automation 套件、统一 agent 权限模型、助手多智能体协作），外部评审带宽继续被挤压
- 我方新 PR 的可行路径收敛为三条：自有发现同日打包（现行为）、维护者邀约牌、战役线程延续

## 2026-10-20 R33 — 本地官方栈升级 v0.8.3 并恢复服务
- 流程：pg_dump 备份（434K，/home/ubuntu/weknora-backup-20261020-2354.sql）→ .env WEKNORA_VERSION main→v0.8.3 → 三镜像拉取（app/ui/docreader）→ up -d
- 结果：五容器运行（app+docreader healthy），启动迁移自动完成（80 表），数据完好（1 KB/1 completed 文档/测试账号可登录）
- 冒烟：knowledge-chat 全链通——references=1 事件（v0.8.3 上引用通道照常）、complete 正常终止、DashScope 模型端点活跃
- 注：compose 的 sandbox 服务未起（v0.8.3 镜像集未含或门控关闭，与主链无关）；本地栈现作为"官方最新版试验场"，与上游 main 开发验证解耦

## 2026-10-21 R34 — v0.8.3 栈沙箱配置：Docker 后端全绿交付，Cube 待凭据
- **Docker 后端完整落地**：①占位服务预拉 weknora-sandbox:v0.8.3 镜像；②.env WEKNORA_SANDBOX_DOCKER_ENABLED=true（部署级闸门，注释明示 docker.sock=宿主 root）；③compose 取消注释挂载 /var/run/docker.sock（入口脚本自动按 GID 加组）；④重建 app（日志实锚 sandbox.docker_enabled applied enabled=true）；⑤POST /sandbox-configs 建 docker-local 配置（id=2f009e19）；⑥**官方 deep 完整验证五项全绿**（daemon 可达/凭据/模板/真实建容器跑探针 sandbox_exec 140ms/出站可达）+ capabilities: pause_resume/reconnect
- 架构笔记：compose 的 sandbox 服务是 profile=full 占位（跑 true 只为拉镜像），真后端=app 按 session 用该镜像建容器（空闲 TTL 清扫即 #3942/#3980 所修子系统）；后端配置全部在「设置→沙箱后端」（TenantSandboxConfig：cube/e2b/docker 三型+网络策略+EnvVars 加密+卷挂载）
- **Cube 后端待用户凭据四件套**：api_url（控制面）、api_key（按集群）、template_id、sandbox_domain（数据面，自托管另有 proxy_url）；就绪后一键 sandbox-check（deep 会真建远端沙箱）→ POST /sandbox-configs
- 测试账号备注：opencode-manual@test.local / SseTest#2026（本地栈）

## 2026-10-26 R35 — 会话续接：第 20 轮巡检，七牌穿越 10-26 早波零叫号；节点磁盘 94% 预警
- **上游今日（10-26 UTC 04:24–05:35）一波 15 件合并**，main 头 236d79614（05:35）——lyingbug #4729（共享沙箱跨技能保留）+ ydflow 六连（agent 压缩/沙箱 stderr 预览/#4675 deep-check 进度保持）+ 社区多件（sqlite 组织支持/rerank 分数刻度/docreader PDF 边界）；**我方 7 PR 全 open 零触碰，双端点（reviews+comments since 10-20）全零**，updated_at 停在各自最后动作日
- 战役件：#3501 仍只有我方 10-12 评论，维护者零回应；#3522 悬置如故（09-23 后无动）
- **#3062 排队第 29 天**（09-27 开），明日 10-27 满 30 天=礼貌 ping 决策日（Greenplumwine 26 天先例已过）
- 节点健康：内存正常（used 1.7G/3.6G）、WeKnora 五容器 v0.8.3 健康；**磁盘 94%（7.6G 空闲）预警**——Docker 镜像 52.75G 其中 34.16G 可回收（未被运行/停止容器引用）为最大杠杆，`docker image prune -a` 可释 34G；共节点多项目（deer-flow 等），清理留用户拍板未执行；/tmp 无我方遗留；他人项目大户不动（octop-memory-scan 5.4G、~/go 3.8G、weknora-images 3.0G）
- 等待输入不变：WSL2 Cube 四件套；DeepSeek Search provider 等用户点头
- **【R35 勘误 10-28】**GitHub 权威核验：①#3062 创建日实为 09-07（登记册误记 09-27，该日是对齐 main 的刷新日）→ 排队已 51 天，30 天 ping 决策已过点，下次巡检时升级处理；②在途实为 **8 PR** 非 7——#4069（10-08，中文向 provider 连通性测试修复）在册外遗漏；#4204 author=vicfei 实锚（gh pr list 偶发漏列，以 API 直查为准）；③合并 13 件全清单已 GitHub 实锚（3036/3069/3096/3104/3273/3336/3339/3367/3493/3638/3804/3943/3980，跨度 09-07→10-07）+ issue 17 件——为用户试用期汇报材料提供的数字基座
