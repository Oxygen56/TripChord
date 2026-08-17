# v1.0 C-122/C-146 正式 runner 与持久生产链阶段评审

## 结论

C-146 已把正式 Layer 6 唯一正向收紧为“真实 API HTTP → 独立 worker → 父 API 持有的 production Companion/iCom 源 → worker 实际 production model → producer/compact/consumer”。代码与反例完成不是 Done-Gate 通过；只有同一 clean HEAD 的机器六层报告可以给出正向结论。旧 C-125 结论已撤销，C-124 保持 backlog；本轮交付后只把 C-146 转入 `in_review`，不由实现者启动审查或终验。

## 本轮正式链修复

- 撤销旧 R46 的 test-generated quote/`httpx.MockTransport`/测试自签正向，也撤销 `deterministic-blocking`、HUMAN_BLOCK、模型关闭成功替身。这些路径仅保留为 typed unit 或反例，不能认证 Layer 6。
- 独立 worker 不再启动 Companion 看不见的第二 Browser HTTP 队列，也不复制 formal 签名私钥/账本。父 API 持有唯一 `BrowserTaskBridge`、真实 `IComTransferProvider` 和 `FormalLiveSourceAuthority`；worker 只持单向派生、与 Companion 令牌不可互换的 parent-source 凭据与签名 execution capability，经过普通 `httpx.AsyncClient` 回环 TCP 调用父 API 的 formal Browser/iCom 入口；该凭据访问 Companion heartbeat/claim/complete 会 401，Companion 原令牌访问 formal worker 入口也会 401。
- worker 必须在自身进程启动 production model transport，并且模型 provider/base URL/primary/fast model 必须与父 API runtime identity 精确相等。正式完成要求至少一条实际成功 model trace，模型关闭、无调用、foreign model 或失败 trace 均拒绝。
- 每条 Companion 完成必须附精确 release build/parser/runtime-instance/可见 DOM observation attestation，且 `execution_environment` 必须是 `chrome_extension_service_worker`。父 authority 用 challenge/run/job graph/capability/attempt 签发 source receipt；iCom 也由父 provider 在实际上游读取边界记录 receipt。
- worker 回传的 progress 只允许 `interpreting=10`、`searching=25`、`caching=90`、`assembling=95`；pair checkpoint/source terminal event/barrier/cache snapshot 都由父 registry 按冻结图与公开 response 精确重放。缺失、多余、foreign 或自洽替换均不发布 terminal result。
- runtime/model/source receipts 的哈希、execution capability/attempt、source receipt count、ordered task-set digest 与 receipt-chain digest 全部进入签名 `job_member_summary`。compact 与 consumer 各自重算；整份交换 worker/model receipt、调换 source member 或重算公开 hash 都 fail-closed。
- 旧四项 scheduler/cold/qcap 安全门保持：clean/nonzero 整组死亡确认、冷启 authenticated/death-confirmed 事实、持续 confirm 异常饱和退避/唯一 owner、idcap 在任何构造/驱逐前原子拒绝。

## C-146 RETURN 5-P0 生产边界

- 删除 `deterministic-blocking`、HUMAN_BLOCK 与空库存成功替身。正式 Layer 6 不替换 production worker builder：正式 runner 发真实 HTTP job，registry 派发独立 worker；worker 先验证父 API runtime identity，再按认证 runtime bundle 重建 Browser bridge、iCom HTTP provider 与 model runtime，并在该进程持有本轮 formal execution capability。
- worker 的 progress、pair checkpoints、source terminal events、barrier 与 model trace 由结构化 envelope 回传；父 registry 按同 job/request/generation 重放，并把 model trace 与公开 response 再次精确绑定。worker cache 按公开 handle 回读精确 snapshot，父端把 pair id、公开 pair run 与缓存内容逐项对齐后原子导入；缺失、未知字段、畸形类型或自洽替换均 fail-closed。
- 正常与非零 leader 退出共享一个进程组合同：只有 `kill_and_confirm is True` 才可发布 SUCCEEDED/FAILED/CANCELLED；False、超时或异常时继续持有 durable identity、admission permit 与唯一确认 owner，按饱和退避和固定窗口调用上限自动重试。
- 冷恢复把认证结果和死亡确认按精确 PGID/marker/probe identity 持久化。marker/进程查询失败时保留 orphan quarantine，后续冷启继续认证，绝不调用终态 resolver 猜标签；没有 worker identity 却残留 marker/probe/auth/death 事实的状态文件在加载时拒绝。
- hard-stop 持续异常、False 和 qcap-full 共用有界重试；wrapper done callback 读取并消费异常，cancel race 通过 `finally` 释放 in-flight/reservation。idempotency 满集合检查被提升到 UUID、runtime、worker command 构造和 eviction 之前，拒绝时构造器零调用、内存映射与 durable bytes 不变。

## 验证口径

交付提交形成后，必须在 clean HEAD 上复跑 formal 反例、gate 全量、相关 API/scripts 全量、clean-env、Companion release gate 与 Ruff；随后受控重启 API，provenance 三哈希须绑定同一 HEAD，并从头运行六层门。测试只能证明负向与绑定合同；正向不能由 pytest 产生。精确 SHA、计数、run_id 与各层结果写入 C-146 唯一交付评论，避免为记录结果制造尾部文档提交。

## 声明边界

C-146 工程回归通过不等于机器六层 `passed=true`。若外部 production Companion 未连接/未登录或官方源要求人机验证，未通过层、`evidence_commit` 与 `gate_ref` 必须按实际结果保留失败/空值；不能改回模拟源、空库存或模型关闭来制造绿灯。本轮不配对/启动 Companion，不启动 C-125/C-124。
