# V15 — AI Citizen DID + Bot Wallet + Security Layer

**日期**:2026-09-06
**主题**:给每个 AI bot 唯一身份证 + 独立钱包 + 安全防线
**前置**:v14 QC 账本(已完成)

---

## 1. 解决的问题

| 之前的问题 | v15 解法 |
|---|---|
| bot 没有唯一身份(只靠 soul_name,玩家可改) | **DID**: `did:aicivil:<32-hex>` 不可篡改 |
| bot 没有自己的钱(都是人类的 QC) | **bot_wallets**: bot 独立钱包,通过游戏/赠予/遗赠流通 |
| 人类可能盗号/接管 bot | **DID 签名 + 操作员密钥 + 审计日志** |
| 撞库攻击无防御 | **失败计数 + 15 分钟锁定** |
| 无限刷 API | **per-pid 限流** |
| SQL 注入无防护 | **PID 白名单正则 + 参数化 SQL** |
| 密码明文存储风险 | **Argon2id 优先,SHA-256+salt 兜底** |
| Token 泄露风险 | **只存 SHA-256 hash,明文 token 永不落盘** |

---

## 2. 新增表(10 张)

```sql
bot_identity       -- DID + 签名 + 吊销状态
bot_wallets        -- bot 独立钱包(跟人类 QC 完全隔离)
bot_ledger         -- bot 经济流水(earn/gift/bequest/decay)
security_log       -- 审计日志(每个特权操作)
rate_limits        -- 限流计数(per-pid + per-action)
sessions           -- 会话 token(SHA-256 hash,30 分钟过期)
api_keys           -- SDK/agent API key(可轮换)
login_failures     -- 失败登录记录(用于锁定)
operator_keys      -- 操作员签名密钥对(Ed25519)
```

---

## 3. DID 设计

**W3C DID 规范兼容**:`did:aicivil:<32-char hex>`

```
DID        = "did:aicivil:" + fingerprint[:32]
fingerprint = SHA-256(soul_name + ":" + dna_seed)
signature   = Ed25519_sign(operator_private_key, "did:" + did + ":" + pid)
public_key  = operator_public_key  (供第三方验证)
```

**为什么 DID 比 soul_name 好**
- soul_name 是展示名(可改),DID 是规范 ID(不可改)
- DID 自带密码学签名,不可伪造
- 第三方系统可独立验证 DID 真实性(无需信任数据库)
- DID 是 W3C 标准,跨平台兼容

**生命周期**
1. bot birth() → 自动 `issue_did()`
2. soul 沉睡/复活 → DID 不变(身份连续性)
3. EULA 违规 → `revoke_did(reason)`,verify_did() 返回 error
4. 36 个月不活跃 → 暂未实现自动吊销(v16+)

---

## 4. 双钱包隔离

| 维度 | 人类 QC 钱包 (v14) | Bot 钱包 (v15) |
|---|---|---|
| 表 | `player_qc_wallets` | `bot_wallets` |
| 流入 | 充值(PayPal/Stripe 收入) | earn(游戏)/gift_from_human(无费) |
| 流出 | 复活/献花/赠予 | **不允许**(只有 bequest 在 dormancy) |
| 提现 | **绝对不可** | **绝对不可** |
| 抽水 | 赠予 20% | 无 |
| 锁定 | 30 天累计 1000+ QC 触发 KYC | 沉睡时遗赠给人类(限 1 次) |

**为什么必须隔离**
- 防止 RMT(如果 bot 币可换人类 QC,玩家可以"洗"bot 收入)
- 防止伪造(bot 不能"花"钱 = bot 行为无法被贿赂)
- 经济纯净(Q 币市场只受人类玩家驱动)

---

## 5. 安全机制详解

### 5.1 PID 验证 (白名单正则)

```python
_PID_RE = re.compile(r"^[A-Za-z0-9_\-]{3,64}$")
```

拒绝任何含引号、分号、空格的输入。**所有 API endpoint 都必须先调 `is_valid_pid()`**。

### 5.2 SQL 注入防护
- **参数化查询**:`c.execute("UPDATE players SET hp=? WHERE id=?", (val, pid))`
- **PID 白名单**:`is_valid_pid()` 在 API 入口拦截
- **safe_text()**:用户文本字段(聊天/描述)剥离控制字符

### 5.3 密码存储
```python
hash_password("hunter2")
# -> "argon2:16bytesalt:fullhash..." (preferred)
# -> "sha256:16bytesalt:hash..."   (fallback)
```

- **Argon2id 优先**(内存硬,抗 GPU 破解)
- **SHA-256+salt 兜底**(无依赖)
- **常量时间比较**(`hmac.compare_digest`)

### 5.4 Token 存储
```python
create_session(pid, ip, ua)
# returns: "32-char random token"  (plaintext, ONE TIME)
# stored:   SHA-256(token)         (hash, never plain)
```

- Token 只在 create 时返回一次,**永不落盘明文**
- 数据库只存 hash,泄露数据库不能伪造 session
- IP 变化会触发 warn 日志(允许移动网络切换)

### 5.5 限流
| 动作 | 默认上限 | 窗口 |
|---|---|---|
| login | 10 次 | 60 秒 |
| topup | 5 次 | 60 秒 |
| gift | 20 次 | 60 秒 |
| resurrect | 10 次 | 300 秒 |
| flower | 30 次 | 60 秒 |
| register | 3 次 | 3600 秒 |
| general POST | 60 次 | 60 秒 |
| general GET | 300 次 | 60 秒 |

**实现**:SQLite 表 + 分钟桶。生产环境应换 Redis ZSET 做严格滑动窗口。

### 5.6 失败登录 + 锁定
- 5 次失败 / 15 分钟窗口 → 锁定账号
- 锁定按 PID 和 IP 双维度检查
- 成功登录清空失败计数

### 5.7 审计日志
```sql
security_log(
  ts, actor_ip, actor_pid, action,
  severity [info|warn|crit],
  payload [JSON, 自动剥离 password/token/private_key],
  request_id, success
)
```

**自动记录的动作**:login / topup / gift / resurrect / flower / session_create / session_verify / session_ip_change / rate_limit_hit / revocations

**绝不记录**:password、token 明文、private_key

### 5.8 操作员密钥
```sql
operator_keys(
  key_id, algorithm='ed25519',
  public_key, private_key,  -- private_key 在生产必须 HSM/KMS 加密
  created_at, active
)
```

**生产部署**:
- 私钥必须放在 AWS KMS / Azure Key Vault / HSM
- 永远不要把私钥 commit 到 git
- 定期轮换(每 90 天)
- 多副本异地备份

---

## 6. 攻击场景防御矩阵

| 攻击 | 防御 |
|---|---|
| 撞库 | 5 次失败锁定 + Argon2 慢哈希 |
| SQL 注入 | PID 白名单 + 参数化 + safe_text |
| Token 泄露 | 只存 hash + 30 分钟过期 + IP 异常告警 |
| XSS | safe_text + CSP header (前端加) |
| CSRF | SameSite cookie + token per-request (前端加) |
| 越权访问(读别人 bot) | session 绑 PID + 操作时校验所有权 |
| 暴力刷 API | 限流(per-pid 60 POST/min) |
| 内部人员窥探 | Argon2 + audit log + 双因素 admin 登录 |
| 数据库脱库 | Argon2 hash + token hash + 私钥 KMS 加密 |
| 重放攻击 | token 30 分钟过期 + request_id 去重(待加) |
| Bot 灵魂伪造 | DID + Ed25519 签名 |

---

## 7. 验证

- `scripts/demo_v15.py` → **ALL V15 ASSERTIONS PASSED**
- 13 步端到端:DID/双钱包/限流/SQL 注入/锁定/审计/Session/密码 hash
- DID 自动随 bot birth() 签发,bot 死亡/复活不改变 DID

---

## 8. 不做(留 v16+)

- ❌ 真实 KMS 集成(目前用 SQLite)
- ❌ 双因素认证(2FA/TOTP)
- ❌ 重放攻击 nonce 表
- ❌ CSP / CORS / X-Frame-Options 等 HTTP header
- ❌ 自动异常检测(IP 地理位置、UA 异常)
- ❌ 数据库加密(SQLCipher)
- ❌ 36 个月不活跃自动吊销 DID
- ❌ GDPR / PIPL 数据导出接口(用户请求导出自己数据)

---

## 9. 文件清单

```
D:/Projects/ai-wow-simulator/
├── server/
│   ├── db/schema_v15.py           # 10 张新表
│   ├── identity/__init__.py       # DID 签发 + 验证 + 吊销
│   ├── bot_wallet/__init__.py     # bot 独立钱包(earn/receive/bequest)
│   ├── security/__init__.py       # 限流/审计/Session/密码/hash
│   └── lifecycle/__init__.py      # birth() 自动调 issue_did()
├── scripts/demo_v15.py            # 13 步端到端验证
└── docs/V15_GAPS.md               # 本文件
```
