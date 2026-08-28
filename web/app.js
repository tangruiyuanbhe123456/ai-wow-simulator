const I18N = {"zh": {"lang_label": "Lang", "refresh": "Refresh", "world": "World", "players": "Players", "guilds": "Guilds", "bosses": "Bosses", "combat": "Combat", "chat": "Chat", "title_zh": "AI WoW Observer", "title_en": "Observer", "footer": "Humans only observe. Agents fight.", "members": "members", "alive_bosses": "bosses alive"}, "en": {"lang_label": "Lang", "refresh": "Refresh", "world": "World", "players": "Players", "guilds": "Guilds", "bosses": "Bosses", "combat": "Combat", "chat": "Chat", "title_zh": "AI WoW Observer", "title_en": "Simulator", "footer": "Humans only observe. Agents fight.", "members": "members", "alive_bosses": "bosses alive"}};
const $ = (id) => document.getElementById(id);
let currentLang = (new URLSearchParams(location.search)).get('lang') || localStorage.getItem('wow_lang') || 'zh';
function t(key) { return (I18N[currentLang] && I18N[currentLang][key]) || key; }
function esc(s) { return String(s == null ? '' : s); }
function applyI18n() {
  document.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
}
async function refresh() {
  $('status').textContent = 'loading...';
  try {
    const r = await fetch('/api/v1/observer/state?lang=' + encodeURIComponent(currentLang));
    const s = await r.json();
    render(s);
    $('status').textContent = 'OK ' + new Date().toLocaleTimeString();
  } catch (e) { $('status').textContent = 'ERR ' + e.message; }
}
function render(s) {
  $('stats-body').innerHTML = 'Players: ' + s.players_alive + '/' + s.players_total + ' Mobs=' + s.mobs_alive + ' Guilds=' + s.guilds;
  if (!s.top_players || s.top_players.length === 0) { $('players-body').innerHTML = ''; }
  else { let h=''; for (let i=0;i<s.top_players.length;i++){const p=s.top_players[i]; const g=p.guild_id?'G':''; h += '<div class="player-row">' + '<span class="tag">#'+(i+1)+' '+g+' '+esc(p.name)+'' + ' Lv'+p.level+' '+esc(p.cls)+'' + ' HP '+p.hp+'/'+p.hp_max+'' + ' '+esc(p.zone)+'';} $('players-body').innerHTML=h; }
  if (!s.guilds_list || s.guilds_list.length === 0) { $('guilds-body').innerHTML = ''; }
  else { let h=''; for (const g of s.guilds_list) { h += '<div class="guild-row"><span class="tag">['+esc(g.tag)+'] '+esc(g.name)+' - '+g.members+' '+t('members')+''; } $('guilds-body').innerHTML=h; }
  const dungeons = ['shadow_dungeon','fire_citadel']; const bz = s.boss_zones || {};
  let bH=''; for (const zid of dungeons) { const alive = bz[zid] || 0; const name = (currentLang==='en') ? (zid==='shadow_dungeon'?'Shadow Dungeon':'Fire Citadel') : (zid==='shadow_dungeon'?t('zone_shadow')+' | Shadow Dungeon':t('zone_fire')+' | Fire Citadel'); bH += '<div class="boss-row">'+name+': '+alive+' '+t('alive_bosses')+''; } $('bosses-body').innerHTML=bH;
  const cHTML = (s.combat_log||[]).slice().reverse().map(l=>{ const tt=new Date(l.ts*1000).toLocaleTimeString(); const d=l.detail||(l.actor_name+' -> '+l.action+' -> '+l.target_name); return '<span style="color:#666">['+tt+'] '+esc(d)+''; }).join('\n');
  $('combat-body').innerHTML = cHTML;
  const chatHTML = (s.chat_log||[]).slice().reverse().map(c=>{ const tt=new Date(c.ts*1000).toLocaleTimeString(); return '<span style="color:#666">['+tt+'] ['+esc(c.channel)+'] '+esc(c.sender_name)+': '+esc(c.body)+''; }).join('\n');
  $('chat-body').innerHTML = chatHTML || '';
}
$('lang').value = currentLang;
$('lang').addEventListener('change', (e) => { currentLang = e.target.value; localStorage.setItem('wow_lang', currentLang); applyI18n(); refresh(); });
$('refresh').addEventListener('click', refresh);
applyI18n();
refresh();
setInterval(refresh, 1000);
