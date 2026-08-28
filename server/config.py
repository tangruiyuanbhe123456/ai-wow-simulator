"""Server configuration."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
WEB_DIR = PROJECT_ROOT / "web"

DB_PATH = DATA_DIR / "world.db"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Network
HOST = os.environ.get("WOW_HOST", "127.0.0.1")
PORT = int(os.environ.get("WOW_PORT", "8787"))

# World
TICK_MS = int(os.environ.get("WOW_TICK_MS", "500"))  # 500ms tick
MAX_PLAYERS = int(os.environ.get("WOW_MAX_PLAYERS", "50"))
STARTING_LEVEL = 1
MAX_LEVEL = 60

# Combat balance (tuned so L1 solo vs boss = loss, party of 3 = win)
# L1 stats: hp=120, atk=14, mp=30, defn=2.
# Boss Shadow Lord: hp=600, atk=12, defn=5.
# Solo warrior dmg/tick ~ 18 (heroic_strike 1.4×14 - 2.5 defn) → 33 ticks to kill boss.
# Boss atk/tick = 12 + rand(0..5) = 12-17 → warrior dies in 7-10 ticks → LOSE.
# Party of 3 (war+mage+priest) combined output ~ 60/tick (with +20% bonus) → 10 ticks to kill boss.
# Priest heal 17 hp, +20% = 20; boss hits lowest-hp for 12-17 → net 0 or positive → party survives → WIN.
BOSS_BASE_HP = 600
BOSS_BASE_ATK = 12
BOSS_CRIT_MULT = 1.5
PLAYER_BASE_HP_PER_LEVEL = 100
PLAYER_BASE_ATK_PER_LEVEL = 12
PARTY_BONUS_PER_MEMBER = 0.10  # 10% per extra member (3-person party = +20%)

# Loot
LOOT_DROP_RATE_BOSS = 0.8
LOOT_DROP_RATE_MOB = 0.15

# Auth
TOKEN_HEADER = "Authorization"
TOKEN_PREFIX = "Bearer "

# i18n
DEFAULT_LANG = os.environ.get("WOW_LANG", "zh")
SUPPORTED_LANGS = ("zh", "en")

LOG_FILE = LOGS_DIR / "server.log"
