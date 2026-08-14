# v1.0 C-122 正式 runner raw 链阶段评审

## 结论

C-round4 的代码返工已完成，结论为“可交不同 Agent 独立复审”，不是 Done-Gate 通过。旧 C-125 结论已撤销，C-124 保持 backlog；本轮交付后只把 C-122 转入 `in_review`。

## 本轮修复

- 正向证据由正式 `_install_browser_bridge` composition 创建并绑定精确 `BrowserTaskBridge`、`IComTransferProvider`、`LivePackageAgentSystem` 与 `FlexibleLiveAgentSystem`，再以 `benchmarks.run_live_done_gate_v4._run` 为唯一 runner 入口。Companion 经 mounted HTTP heartbeat/claim/complete 上报；iCom 仅在 `httpx` transport 返回 schedules/base-fare/policy 原始 JSON，URL/schema/normalize/evidence/领域结果全部由真实 provider 产生。测试除此之外只提供冻结时钟和输出路径；request、runtime/Companion preflight、API job、三 pair checkpoint、事件重规划、15 项 producer、completed bundle 与 0600 原子 writer 都由正式代码产生。
- compact 从正式 raw 的顶层 `pair_checkpoint_binding` 读取控制面绑定；旧 `context` 包装只保留兼容读取。两份同时存在且内容不同会 fail-closed，不能择一洗白。
- query 的 foreign/cross-pair/missing/extra，checkpoint 的 missing/extra/cross-pair，foreign request identity、extra check、empty evidence 均从同一正式 raw 深拷贝，并经正式 `_write_evidence_bundle` 落盘后再进入 compact/consumer。
- 测试不替换生产类或函数；结束时显式断言方法/函数 identity、FastAPI routes/state 与模块 globals 未改变。数据库与 bridge-state 由 pytest session 重定向到临时路径。
- 追加快审评论 `1966f579-ade0-4428-9095-62a19b2d0e5d` 已在同一技术 run 消费：领域 provider 桩、空/缺 iCom HTTP 回执、手工 composition 回执和非 HTTP Companion heartbeat 均有 fail-closed 反例，不能满足正向测试。
- 退回后的正式来源绑定改为生产 `FormalLiveSourceAuthority`：`_install_browser_bridge` 创建单一 installation 并绑定实际对象关系；mounted HTTP heartbeat/claim/complete 与真实 `IComTransferProvider` public GET 才能追加哈希链事件。runner 从运行前后 runtime 快照派生 delta，raw、compact 与 final 共同精确验证 installation/composition/source commit、事件形状与次序、claim-complete 对应以及三条 iCom 路径。仅手工拼接正确类名、成员、路径和 HTTP 200 不再构成证据。
- `pair_checkpoint_binding` 改为 presence-sensitive：顶层字段缺席才读取 legacy `context`；顶层一旦出现，`null`、list 或其他非 object 值立即 fail-closed。正式测试同时保留“字段缺席 + 有效 legacy”兼容正例。
- 已消费追加独审评论 `3d5717e4-913b-446a-ae7a-f92008bca8ea`：正式 installation 用 Browser bridge token 派生 installation/commit 专属 HMAC capability，snapshot、mounted HTTP/iCom 事件及 before/after binding 均带 capability MAC；token 不进入 runtime payload、raw、compact 或提交证据。攻击者即使完整填写新 schema、精确复刻事件顺序/路径/状态并重算全部公开 SHA，再用自选 foreign secret 生成自洽 MAC，仍无法取得生产 capability，直接 validator、raw writer 与 final consumer 三层均 fail-closed。
- raw `formal_live_source_binding` 改为严格 presence：missing、`null`、list 或其他非 object 值均在 compact writer 读取 raw 时立即拒绝；正式 runner 生成时已用同一 capability 校验，final 再次验证，不能把失败后移或借 compact 洗白。

## 验证口径

交付提交形成后，必须在 clean HEAD 上复跑正式正向/反例、gate 全量、相关 API/benchmark/scripts 全量、clean-env 与 Ruff；随后受控重启 API，provenance 三哈希须绑定同一 HEAD，并从头运行六层门。精确 SHA、计数、run_id 与各层结果写入 C-122 唯一交付评论，避免再为记录结果制造尾部文档提交。独立复审须原样复跑完整 foreign-capability 自洽 binding、手工自洽回执、foreign installation/composition、缺 claim、compact 路径篡改、raw binding missing/null/list、checkpoint 顶层 null/list 与 absent-legacy 正例。

## 声明边界

正式 raw 的冻结场景 15/15 不等于机器六层 `passed=true`。若 Companion 仍未满足实时授权，L5/L6、`evidence_commit` 与 `gate_ref` 必须按实际结果保留失败/空值；当前不请求用户配对或模型费用动作，也不启动 C-124。
