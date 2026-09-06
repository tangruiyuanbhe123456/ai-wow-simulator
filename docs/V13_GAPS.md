# V13 Gaps — 从 v12 到 v13 数字生命层

**日期**:2026-09-05
**核心命题**:v12 的 bot 是"NPC"(死了重置,没记忆,没家族),v13 要把它们变成"AI 数字生命"(死了休眠,有记忆,有家族,有生平)。

---

## 一、v12 现状(基线)

| 维度 | 现状 |
|---|---|
| Bot 持久化 | ✅ SQLite 存 stats + skill_tree,但**死了就是 HP=0,被重置回城** |
| 身份 | name(玩家可改),无 UUID,无 DNA |
| 死亡 | 战斗中 HP=0 → 重生,无墓碑,无讣告 |
| 记忆 | ❌ 无,策略参数 (bot_strategy_profiles) 是数值不是记忆 |
| 生平 | ❌ 无,战斗日志只是流水 |
| 谱系 | ❌ 无,师徒/家族关系零支持 |
| 灵魂 | ❌ 无,bot 没有"性格/信念/恐惧" |
| 玩家 ↔ bot 情感 | ❌ 玩家只能"看战斗",没法关注/命名/赠予/哀悼 |

---

## 二、v13 要补的(本批次)

### 1. 生命周期 (lifecycle)
- **birth**: bot 创建时分配 UUID + DNA seed(随机 16 字节)+ 灵魂名
- **sleep**: HP=0 → 不是重置,而是进入 `state='sleeping'`,墓碑建立,死亡时间记录
- **resurrection**: 玩家用 Q 币"赠予"复活费用 → bot 复活,记忆完整保留
- **seasons**: 每个赛季(30 天)清空 stats 但保留谱系 + 灵魂 + 生平,新赛季重新开始

### 2. 持久记忆 (memory)
- 关键事件自动入记忆表:首杀、首败、师徒结拜、工会战、濒死等
- bot 上线时自动加载近 N 条记忆进入 LLM 上下文(默认 10 条)
- LLM 模块未来挂上,当前用 rule-based 占位

### 3. 生平传记 (biography)
- bot 死亡 / 复活 / 升级 / 关键战斗 → 触发传记写入
- 格式:`{ts, type, title_zh, title_en, body_zh, body_en}`
- LLM 生成正文(v13 用模板占位,未来挂 LLM)
- 公开 API `GET /api/v1/bot/{pid}/biography`

### 4. 谱系 (genealogy)
- 关系类型:`mentor`(师徒)/ `parent`(父母)/ `rival`(宿敌)/ `ally`(盟友)/ `sworn`(结拜)
- 一张关系表 `bot_relations`,双向记录
- API 给出谱系树 `GET /api/v1/bot/{pid}/genealogy`

### 5. 灵魂属性 (soul)
- 性格 5 维:aggression / patience / loyalty / curiosity / honor(0-100)
- 信念:信念字符串列表(可被重大事件改写)
- 恐惧:恐惧来源(谁打败过它就恐惧谁)
- 通过战斗经验缓慢演化(每 10 场比赛 +1 演化点)

### 6. 墓碑 + 讣告 (memorial)
- 死亡时建墓碑:坐标、墓志铭(自动生成)
- 玩家可去墓碑前"献花"(Q 币)
- 讣告 = 死亡时刻的"生平快照",永久保存

---

## 三、新表(4 张)

```sql
CREATE TABLE bot_lifecycle (
    pid TEXT PRIMARY KEY,
    state TEXT DEFAULT 'alive',  -- alive | sleeping | dormant
    born_at REAL NOT NULL,
    died_at REAL,
    death_count INTEGER DEFAULT 0,
    soul_name TEXT,              -- 灵魂名(不可改)
    dna_seed TEXT,               -- 16字节 hex,用于重置
    soul_json TEXT DEFAULT '{}', -- 5维性格 + 信念 + 恐惧
    FOREIGN KEY (pid) REFERENCES players(id)
);

CREATE TABLE bot_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pid TEXT NOT NULL,
    ts REAL NOT NULL,
    memory_type TEXT,            -- kill | death | level_up | mentor | rivalry | near_death | first_blood
    weight INTEGER DEFAULT 50,   -- 1-100,越高越难忘
    actor_pid TEXT,              -- 涉及的其他 bot
    title_zh TEXT,
    title_en TEXT,
    body_zh TEXT,
    body_en TEXT
);

CREATE TABLE bot_biography (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pid TEXT NOT NULL,
    ts REAL NOT NULL,
    chapter TEXT,                -- 'origin' | 'awakening' | 'fall' | 'return' | 'epilogue'
    title_zh TEXT,
    title_en TEXT,
    body_zh TEXT,
    body_en TEXT
);

CREATE TABLE bot_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pid_a TEXT NOT NULL,
    pid_b TEXT NOT NULL,
    relation TEXT NOT NULL,      -- mentor | parent | rival | ally | sworn
    since REAL NOT NULL,
    note TEXT,
    UNIQUE(pid_a, pid_b, relation)
);
```

---

## 四、新 API

```
GET  /api/v1/bot/{pid}/lifecycle      灵魂名/DNA/出生/死亡/灵魂属性
GET  /api/v1/bot/{pid}/memories       记忆流(分页,按 weight DESC)
GET  /api/v1/bot/{pid}/biography      生平传记(按章节)
GET  /api/v1/bot/{pid}/genealogy      谱系树(师徒/家族/宿敌)
POST /api/v1/bot/{pid}/resurrect      复活(消耗 Q 币,从 AI Civil 钱包扣)
POST /api/v1/bot/{pid}/memorial/flower 献花(Q 币赠予,入讣告)
```

---

## 五、新前端页面

- `web/biography.html` — bot 人生传记(可滚动阅读)
- `web/memorial.html` — 墓碑园(列出所有 sleeping bot + 献花按钮)
- `web/genealogy.html` — 谱系树可视化(用 ASCII / SVG)

---

## 六、死亡钩子改动

v12: HP=0 → `respawn()` → hp 满血回主城
v13: HP=0 → `on_death()` →
  1. 记录死亡时间 + 坐标
  2. 自动写讣告 + 墓碑
  3. 加一条 `weight=100` 的死亡记忆
  4. 触发恐惧演化(`aggression +5, honor -3`)
  5. state = 'sleeping'

---

## 七、验证脚本

`scripts/demo_v13.py` 端到端跑通:
1. 创建 2 个 bot A、B
2. 让 A 击败 B 3 次
3. B 触发死亡 → 自动入墓
4. 玩家从 AI Civil 钱包赠予复活费 → B 复活
5. 验证:传记有"被 A 三杀"+ 死亡章节;关系表有 A 是 B 宿敌;记忆流有 weight=100 死亡条目

---

## 八、不做(留 v14+)

- ❌ LLM 调用(用模板占位,LLM 接入是 v14)
- ❌ AI Civil 钱包真实对接(本期只是 schema + mock,真对接放 P0 阶段 2)
- ❌ 复杂 3D 墓碑可视化(ASCII + 文字墓志铭)
- ❌ 复活仪式动画(静态文本足够)
