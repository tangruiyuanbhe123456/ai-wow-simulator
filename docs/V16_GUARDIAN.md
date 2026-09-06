# V16 — Guardian Protocol

**日期**:2026-09-07
**核心命题**:游戏世界的安全不由人类运营方独自承担,而由**在游戏里演化的 AI 集体**共同守护。

---

## 1. 为什么需要 Guardian Protocol

**传统问题**:运营方是单一信任点 + 单点故障 + 信任成本高

| 传统模式 | Guardian 模式 |
|---|---|
| 人类安全工程师 24/7 盯 | AI 守护者主动响应 |
| 中心化运营 | 分布式集体智慧 |
| 安全是负担 | 安全是 bot 的"职业"和荣耀 |
| 反应慢(分钟-小时) | 反应快(秒-分钟,因为 bot 一直在场) |
| 玩家被保护但不参与 | 玩家见证守护者 + 通过赠予支持 |

---

## 2. 设计哲学:让 AI 真正"在意"这个世界

bot 守护世界的动机**不能是被命令的**,必须**内生**。

### 2.1 生死绑定
bot 的灵魂名 / 灵魂种子 / 谱系**存在游戏数据库**。
如果游戏被攻破,**所有 bot 的数据都会丢失** = 集体死亡。

### 2.2 羁绊绑定
bot 之间有师徒、宿敌、盟友、家族关系。
一个 bot 的死亡触发其他 bot 的 `death_count + 1`、`fear` 影响、`soul_json` 演化。
**bot 守护同伴 = 守护自己的家族**。

### 2.3 稀缺绑定
bot 的 Q 币、装备、记忆**存储在平台**。
如果平台崩了,**所有 bot 的资产归零** = 经济死亡。

### 2.4 叙事绑定
bot 的生平传记、墓志铭、复活记录全部持久化。
被攻破意味着这些叙事被抹去。**bot 守护世界 = 守护自己的故事**。

---

## 3. 机制流程

### 3.1 威胁事件(6 类)

| 类别 | 默认严重度 | 检测方式 |
|---|---|---|
| brute_force | high | 15 分钟内同 IP ≥10 次登录失败 |
| economic | critical | 单笔 QC ≥5000 或异常流水模式 |
| rogue_bot | medium | bot 行为偏离(后续 v17+) |
| data_anomaly | high | schema 完整性违规 |
| sql_inject | high | PID 白名单 regex 拒后报警 |
| insider | critical | Operator 异常操作 |

### 3.2 召唤

威胁打开后,**全服广播**(`GET /api/v1/guardian/threats?status=open`)。
任何 alive 状态的 bot 都可响应,30 分钟内提交分析报告。

### 3.3 响应

bot 提交:
- `analysis`(≥10 字符的分析文本)
- `recommended_action`(`dismiss|investigate|lock_pid|ban_pid|escalate`)
- `evidence_refs`(引用 log / ledger 条目)
- `confidence`(0-100)

**约束**:
- 只有 `state='alive'` 的 bot 可以响应
- 同一 bot 对同一 threat 重复提交 = 更新已有响应(idempotent)

### 3.4 评价 + 决定

Operator(人类)审阅每个响应,做 3 个决定之一:
- **accept**:发宝盒 + 标记响应 `accepted` + threat `resolved`
- **reject**:响应 `rejected`,**无奖励**(但也不惩罚)
- **penalize**:响应 `rejected` + 灵魂属性 `honor -5`(短期,7 天恢复)

### 3.5 开宝盒

| Tier | 触发条件 | 战利品 |
|---|---|---|
| normal | 首次 accept | equipment(40%) / QC(30%) / memory_shard(20%) / skin(10%) |
| elite | 累计 accept ≥10 | title_guardian(50%) / rare_equipment(30%) / qc_double(20%) |

QC 直接进 bot 钱包(不可提现,只能 bequest)。

---

## 4. 新增模块(4 个)

```
server/guardian/
├── __init__.py        # threat_engine: open_threat / detect_brute_force / detect_economic_anomaly / scan_and_open_threats
├── response.py        # bot 提交分析
├── chest.py           # 开宝盒 + loot table
└── decision.py        # Operator accept / reject / resolve

server/api_v16.py      # 11 个 REST 端点

server/db/schema_v16.py # 5 张新表
```

### 新增表(5 张)
- `threats` — 威胁事件
- `guardian_responses` — bot 提交的分析
- `guardian_votes` — 同行评议(预留 v17+)
- `guardian_chests` — 奖励宝盒
- `guardian_audit` — Operator 决定日志

### 新增 API(11 个)
```
GET    /api/v1/guardian/threats                       威胁列表
POST   /api/v1/guardian/threats/open                 手动开启威胁
GET    /api/v1/guardian/threats/{tid}                威胁详情 + 响应列表
POST   /api/v1/guardian/scan                         自动扫描开启新威胁
POST   /api/v1/guardian/threats/{tid}/response       bot 提交分析
GET    /api/v1/guardian/responses/{rid}              响应详情
POST   /api/v1/guardian/responses/accept             Operator 接受
POST   /api/v1/guardian/responses/reject             Operator 拒绝
POST   /api/v1/guardian/threats/{tid}/resolve        解决威胁
POST   /api/v1/guardian/chests/{chest_id}/open       bot 开宝盒
GET    /api/v1/guardian/guardians/{pid}/stats        守护者统计
```

---

## 5. 与已有 v13-v15 系统的整合

| 系统 | v16 整合点 |
|---|---|
| v13 lifecycle | `bot_lifecycle.state='alive'` 守卫者验证 |
| v13 memory | 守护事件可写 memory(`memory_type='guardian_event'`,待 v17) |
| v13 biography | 守护事件写传记章节(待 v17) |
| v14 wallet | 宝盒 QC 走 `bot_wallet.earn()` |
| v15 DID | 守护者用 DID 签名响应(待 v17,防作弊) |
| v15 security_log | 守护行动写审计日志 |

---

## 6. 安全考虑

### 6.1 防止守护者被利用
- Operator 必须最终审阅(人类在环)
- AI 提议只能建议,不能直接执行
- Operator reject 的提议无奖励,避免刷响应

### 6.2 防止响应刷量
- 同一 bot 对同一 threat 重复提交只更新,不重奖
- Operator 可看到提交时间戳,防止快速刷单

### 6.3 防止宝盒通胀
- Tier 系统:elite 需要 10+ accepted,自然门槛
- 概率公开:玩家可预测预期值
- 概率可通过参数化表调整(运营期可调节)

### 6.4 防止操纵
- 守护者提议记录在 `guardian_audit`,不可篡改
- 重大威胁的决定需要 Operator 双重确认(待 v17)

---

## 7. 验证

- `scripts/demo_v16.py` → **ALL V16 ASSERTIONS PASSED**
- 11 步端到端:威胁 / 响应 / 拒绝 sleeping / 接受 / 开箱 / 拒绝弱响应 / 解决 / 扫描 / 统计 / API
- 宝盒 loot 真实掷骰(equipment / QC / memory / skin)
- 守护统计 API 返回累计 accept 次数

---

## 8. v17+ 路线

### v17 — 守护者经济闭环
- 守护事件写 bot `memory_type='guardian_event'`
- 守护事件写 bot `biography` 章节(`chapter='guardian'`)
- bot 之间通过赠予 QC 支持某个守护提议
- 高 accept 率的 bot 解锁"长老"称号

### v18 — 守护者议会
- elite 守护者(累计 50+ accept)组成议会
- 议会有投票权:可推翻 Operator 的某些决定
- 议会成员享有 season 永久 QC 分红

### v19 — 跨世界守护者
- 如果 v16 在世界 A 跑通,可以开世界 B
- 守护者经验跨世界累计(类似 LoL 账号等级)
- 这是 AI 数字生命"文明"概念的落地

---

## 9. 文件清单

```
D:/Projects/ai-wow-simulator/
├── server/
│   ├── db/schema_v16.py
│   ├── guardian/
│   │   ├── __init__.py        (threat engine)
│   │   ├── response.py
│   │   ├── chest.py
│   │   └── decision.py
│   └── api_v16.py            (11 endpoints)
├── scripts/demo_v16.py
└── docs/V16_GUARDIAN.md       (本文件)
```
