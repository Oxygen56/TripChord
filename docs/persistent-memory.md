# 可重启恢复的本地记忆

`PersistentMemoryStore` 是现有 `MemoryStore` 的单进程本地持久化适配器。它沿用原有的租户、用户、会话、行程和 Agent 角色可见性检查，并在每次写入或 TTL 清理后原子替换整份 JSON 快照。

```python
from tripchord.agents.persistent_memory import PersistentMemoryStore

store = PersistentMemoryStore(".runtime/memory-state.json")
store.upsert(memory_record)

# 撤销必须带同一用户/租户边界，且删除也会原子写入快照。
store.delete(memory_record.id, memory_access)

# 进程重启后使用同一路径；过期记忆会在恢复阶段被清理。
store = PersistentMemoryStore(".runtime/memory-state.json")
```

快照带 SHA-256 摘要，用于发现截断或意外篡改，但它不是有密钥的防篡改签名。目标文件权限固定为 `0600`。默认遇到损坏文件会拒绝启动（`fail_closed`）；显式选择 `CorruptionPolicy.QUARANTINE` 时，损坏文件会被移入同目录的 `*.corrupt-*` 文件，且其中任何记忆都不会进入运行时。

默认不将 `sensitive=True` 的记忆写入磁盘。该适配器不提供静态加密、多进程锁、分布式一致性或远程备份；若要部署为多副本服务，应替换为具备数据库事务和密钥管理的存储实现。

快照恢复会重新校验记忆 payload 大小、嵌套深度、污染标记与 RAG 契约；旧快照中
的超界或未标注注入文本会使加载失败关闭，不会被宽松导入。
