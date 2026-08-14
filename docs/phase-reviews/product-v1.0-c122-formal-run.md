# v1.0 C-122 正式 runner raw 链阶段评审

## 结论

C-round4 的代码返工已完成，结论为“可交不同 Agent 独立复审”，不是 Done-Gate 通过。旧 C-125 结论已撤销，C-124 保持 backlog；本轮交付后只把 C-122 转入 `in_review`。

## 本轮修复

- 正向证据由正式 `_install_browser_bridge` composition 创建并绑定精确 `BrowserTaskBridge`、`IComTransferProvider`、`LivePackageAgentSystem` 与 `FlexibleLiveAgentSystem`，再以 `benchmarks.run_live_done_gate_v4._run` 为唯一 runner 入口。Companion 经 mounted HTTP heartbeat/claim/complete 上报；iCom 仅在 `httpx` transport 返回 schedules/base-fare/policy 原始 JSON，URL/schema/normalize/evidence/领域结果全部由真实 provider 产生。测试除此之外只提供冻结时钟和输出路径；request、runtime/Companion preflight、API job、三 pair checkpoint、事件重规划、15 项 producer、completed bundle 与 0600 原子 writer 都由正式代码产生。
- compact 从正式 raw 的顶层 `pair_checkpoint_binding` 读取控制面绑定；旧 `context` 包装只保留兼容读取。两份同时存在且内容不同会 fail-closed，不能择一洗白。
- query 的 foreign/cross-pair/missing/extra，checkpoint 的 missing/extra/cross-pair，foreign request identity、extra check、empty evidence 均从同一正式 raw 深拷贝，并经正式 `_write_evidence_bundle` 落盘后再进入 compact/consumer。
- 测试不替换生产类或函数；结束时显式断言方法/函数 identity、FastAPI routes/state 与模块 globals 未改变。数据库与 bridge-state 由 pytest session 重定向到临时路径。
- 追加快审评论 `1966f579-ade0-4428-9095-62a19b2d0e5d` 已在同一技术 run 消费：领域 provider 桩、空/缺 iCom HTTP 回执、手工 composition 回执和非 HTTP Companion heartbeat 均有 fail-closed 反例，不能满足正向测试。

## 验证口径

交付提交形成后，必须在 clean HEAD 上复跑正式正向/反例、gate 全量、相关 API/benchmark/scripts 全量、clean-env 与 Ruff；随后受控重启 API，provenance 三哈希须绑定同一 HEAD，并从头运行六层门。精确 SHA、计数、run_id 与各层结果写入 C-122 唯一交付评论，避免再为记录结果制造尾部文档提交。

## 声明边界

正式 raw 的冻结场景 15/15 不等于机器六层 `passed=true`。若 Companion 仍未满足实时授权，L5/L6、`evidence_commit` 与 `gate_ref` 必须按实际结果保留失败/空值；当前不请求用户配对或模型费用动作，也不启动 C-124。
