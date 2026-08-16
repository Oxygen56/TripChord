# v1.0 C-122/C-146 正式 runner 与持久生产链阶段评审

## 结论

C-round4 正式 raw 与 C-146 RETURN 5-P0 的代码返工已完成，结论为“可交不同 Agent 独立复审”，不是 Done-Gate 通过。旧 C-125 结论已撤销，C-124 保持 backlog；本轮交付后只把 C-146 转入 `in_review`，不由实现者启动审查或终验。

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

## C-146 RETURN 5-P0 生产边界

- 删除 `deterministic-blocking`、HUMAN_BLOCK 与空库存成功替身。正式 Layer 6 不替换 production worker builder：正式 runner 发真实 HTTP job，registry 派发独立 worker；worker 先验证父 API runtime identity，再按认证 runtime bundle 重建 Browser bridge、iCom HTTP provider 与 model runtime，并在该进程持有本轮 formal execution capability。
- worker 的 progress、pair checkpoints、source terminal events、barrier 与 model trace 由结构化 envelope 回传；父 registry 按同 job/request/generation 重放，并把 model trace 与公开 response 再次精确绑定。worker cache 按公开 handle 回读精确 snapshot，父端把 pair id、公开 pair run 与缓存内容逐项对齐后原子导入；缺失、未知字段、畸形类型或自洽替换均 fail-closed。
- 正常与非零 leader 退出共享一个进程组合同：只有 `kill_and_confirm is True` 才可发布 SUCCEEDED/FAILED/CANCELLED；False、超时或异常时继续持有 durable identity、admission permit 与唯一确认 owner，按饱和退避和固定窗口调用上限自动重试。
- 冷恢复把认证结果和死亡确认按精确 PGID/marker/probe identity 持久化。marker/进程查询失败时保留 orphan quarantine，后续冷启继续认证，绝不调用终态 resolver 猜标签；没有 worker identity 却残留 marker/probe/auth/death 事实的状态文件在加载时拒绝。
- hard-stop 持续异常、False 和 qcap-full 共用有界重试；wrapper done callback 读取并消费异常，cancel race 通过 `finally` 释放 in-flight/reservation。idempotency 满集合检查被提升到 UUID、runtime、worker command 构造和 eviction 之前，拒绝时构造器零调用、内存映射与 durable bytes 不变。

## 验证口径

交付提交形成后，必须在 clean HEAD 上复跑正式正向/反例、gate 全量、相关 API/scripts 全量、clean-env 与 Ruff；随后受控重启 API，provenance 三哈希须绑定同一 HEAD，并从头运行六层门。精确 SHA、计数、run_id 与各层结果写入 C-146 唯一交付评论，避免再为记录结果制造尾部文档提交。独立复审除原样复跑完整 formal raw/capability 对抗矩阵外，还须复跑：真实 HTTP→worker ready 链、跨进程 observability/cache 绑定、clean/nonzero confirmation False/raise、连续冷启认证失败不漂移、持续 kill exception 固定窗口上界/异常消费、idcap 构造器零调用与 bytes 不变。

## 声明边界

正式 raw 的冻结场景 15/15 与 C-146 工程回归通过都不等于机器六层 `passed=true`。若外部授权仍未满足，未通过层、`evidence_commit` 与 `gate_ref` 必须按实际结果保留失败/空值；当前不请求用户配对或模型费用动作，也不启动 C-125/C-124。
