# V14 — QC Wallet + Death Dispatcher

**日期**:2026-09-06
**主题**:v12/v13 的两个 mock 点打通到真实生产逻辑
**前置依赖**:v13 数字生命层(已完成)

---

## 1. 解决的问题

| Mock 点 | v12/v13 现状 | v14 真实化 |
|---|---|---|
| 死亡触发 | `POST /api/v1/bot/{pid}/lifecycle/death` 手动 | SQLite trigger 自动(任何 UPDATE hp<=0) |
| 死亡处理 | 同步调用 `on_death` | outbox 模式 + 后台 dispatcher 异步 |
| Q 币 | mock,前端写死 100 | 真实账本(player_qc_wallets + qc_ledger) |
| 复活/献花扣费 | 不扣 | 真实从钱包扣 + ledger 记录 |
| 玩家赠予 | 没实现 | 20% 抽水 + 双记账 + ledger |
| KYC | 没实现 | 30 天滚动累计 10000 QC 触发 |

---

## 2. 新增表(3 张)

```sql
player_qc_wallets        -- 玩家 QC 余额 + 终身统计 + KYC 状态
qc_ledger                -- 追加式账本(7 类方向:topup/gift_sent/gift_received/gift_fee/resurrect/flower)
death_outbox             -- SQLite trigger 写入,Python 轮询消费
```

外加 1 个 SQLite trigger:

```sql
CREATE TRIGGER trg_player_hp_zero
AFTER UPDATE OF hp ON players
WHEN NEW.hp <= 0 AND OLD.hp > 0
BEGIN
    INSERT INTO death_outbox (pid, zone, pos_x, pos_y, hp_before, created_at)
    VALUES (NEW.id, NEW.zone, NEW.pos_x, NEW.pos_y, NEW.hp, strftime('%s','now'));
END;
```

---

## 3. 新增 API(5 个)

```
GET  /api/v1/bot/qc/balance/{pid}          玩家余额
GET  /api/v1/bot/qc/kyc/{pid}              KYC 状态 + 30 天累计
POST /api/v1/bot/qc/topup                  充值(PayPal/Stripe webhook 调)
POST /api/v1/bot/qc/gift                  P2P 赠予(自动扣 20%)
POST /api/v1/bot/qc/spend/{bot}/{dir}      复活/献花扣费
```

---

## 4. 经济规则(已落到代码)

| 行为 | 费率 | 说明 |
|---|---|---|
| 充值 | 3% 已含 | 显示价格已含服务费,ledger 只记 gross |
| P2P 赠予 | 20% 平台费 | 发送方付 gross,接收方拿 80%,平台拿 20% |
| 复活 | 100 QC | 玩家钱包扣 |
| 献花 | 默认 10 QC | 玩家钱包扣 |
| KYC 触发 | ¥1000/30天 ≈ 10000 QC | 充值或 30 天累计收礼 ≥ 50000 QC |

---

## 5. 死亡流(从 v12 → v14)

```
v12:combat 扣血 → bot HP=0 → 重生回城 ❌ 没有死亡叙事
v13:combat 扣血 → bot HP=0 → 重生回城 ❌ on_death 需手动 POST
v14:combat 扣血 → bot HP=0
       ↓ (SQLite trigger,自动)
       death_outbox 写一行 pending
       ↓ (后台 dispatcher 每 2s 轮询)
       on_death() 调用
       ↓
       bot_lifecycle.state = 'sleeping'
       bot_memorial 建墓碑
       bot_memories 加 weight=100 死亡条目
       bot_biography 加 'fall' 章节
       bot_relations 加 rivalry
       灵魂演化(aggression+3, honor-2, fear+killer)
       death_outbox 标记 processed
```

**未来要做**:combat 代码里调 `set_last_damage()` 告诉 outbox 真正的凶手 pid,
这样墓志铭能正确写"被 X 击倒于 Y"。

---

## 6. 验证

- `scripts/demo_v14.py` → **ALL V14 ASSERTIONS PASSED**
- v13 demo 无回归
- 5 个 API 端点全部 200
- ledger 8 类方向验证完整

---

## 7. 不做(留 v15+)

- ❌ PayPal/Stripe 真实对接(目前 topup 是手动 API 调用)
- ❌ 多设备/多账户 RMT 检测(基础结构在,kill 算法未实现)
- ❌ 季节/赛季 reset 脚本
- ❌ 玩家端 UI(充值入口/钱包/赠予按钮)
- ❌ KYC 真实对接(目前只标记 required,需要接 阿里云/Onfido)

---

## 8. 文件清单

```
D:/Projects/ai-wow-simulator/
├── server/
│   ├── db/schema_v14.py            # 3 张新表 + 1 trigger
│   ├── wallet/__init__.py          # QC 账本逻辑
│   ├── death_dispatcher.py         # outbox 消费者
│   └── api_v13.py                  # +5 个 v14 端点(同文件追加)
├── scripts/demo_v14.py             # 端到端验证
└── docs/V14_GAPS.md                # 本文件
```
