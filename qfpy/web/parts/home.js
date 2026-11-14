/* ---------- 首页：大盘行情 + 市场情绪 + 股池走势 ---------- */
const POOLOPEN=new Set();
function spark(mn,w=240,h=58){const pts=(mn&&mn.points)||[];if(pts.length<2)return '<div class="mute small" style="height:58px;display:grid;place-items:center">暂无分时</div>';
  const pre=mn.preclose||pts[0].p,ps=pts.map(x=>x.p),lo=Math.min(pre,...ps),hi=Math.max(pre,...ps),rg=(hi-lo)||1,n=241;
  const X=i=>(i/(n-1)*w).toFixed(1),Y=v=>(h-3-(v-lo)/rg*(h-6)).toFixed(1),last=ps.at(-1),col=last>=pre?"var(--up)":"var(--down)";
  const line=ps.map((v,i)=>`${X(i)},${Y(v)}`).join(" ");
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><line x1="0" x2="${w}" y1="${Y(pre)}" y2="${Y(pre)}" stroke="var(--mute2)" stroke-dasharray="3 3" stroke-width="1"/>
   <polygon points="0,${Y(pre)} ${line} ${X(ps.length-1)},${Y(pre)}" fill="${col}" opacity=".12"/><polyline points="${line}" fill="none" stroke="${col}" stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg>`}
const yi=v=>v==null?"-":(v/1e8>=1e4?(v/1e12).toFixed(2)+" 万亿":(v/1e8).toFixed(0)+" 亿");
function idxTiles(mk){if(!mk)return '<div class="empty">正在后台取大盘行情…</div>';
  return `<div class="idxs">${mk.indices.map(i=>{const c=i.change_pct;return `<div class="idx"><div class="row" style="justify-content:space-between"><b>${esc(i.name)}</b><span class="small mute num">${esc(i.code)}</span></div>
    <div class="p num ${c>0?"up":c<0?"down":""}">${i.price==null?"-":(+i.price).toFixed(2)}</div><div class="small num">${pct(c)} <span class="${i.change>0?"up":i.change<0?"down":""}">${i.change==null?"":(i.change>0?"+":"")+(+i.change).toFixed(2)}</span> <span class="mute">成交 ${yi(i.amount)}</span></div>${spark(i.minute)}</div>`}).join("")}</div>
   ${mk.index_error?`<div class="warn small">指数行情取不到：${esc(mk.index_error)}</div>`:""}`}
function statsCard(mk){const s=mk&&mk.stats;if(!s)return `<div class="empty">${mk&&mk.stats_error?"市场情绪取不到："+esc(mk.stats_error):"正在后台取市场情绪…"}</div>`;
  const up=s.up||0,dn=s.down||0,fl=s.flat||0,tot=Math.max(1,up+dn+fl);
  return `<div class="kpis"><div class="kpi"><div class="l">上涨</div><div class="v up">${up}</div></div><div class="kpi"><div class="l">下跌</div><div class="v down">${dn}</div></div><div class="kpi"><div class="l">平盘</div><div class="v">${fl}</div></div>
    <div class="kpi"><div class="l">涨停</div><div class="v up">${s.limit_up??"-"}</div></div><div class="kpi"><div class="l">跌停</div><div class="v down">${s.limit_down??"-"}</div></div><div class="kpi"><div class="l">两市成交额</div><div class="v">${yi(s.amount)}</div></div></div>
   <div class="breadth" title="涨 ${up} / 平 ${fl} / 跌 ${dn}"><i class="u" style="width:${up/tot*100}%"></i><i class="f" style="width:${fl/tot*100}%"></i><i class="d" style="width:${dn/tot*100}%"></i></div>
   <div class="small mute">数据日 ${esc(s.date||"-")} · 来源 ${esc(mk.stats_source||"-")}${s.suspended?` · 停牌 ${s.suspended}`:""}</div>`}
function poolTable(pd){if(!pd)return '<div class="empty">正在后台汇总股池…</div>';if(!pd.pools.length)return '<div class="empty">没有配置股池。去「设置 → 股池」勾选。</div>';
  return `<div class="pools">${pd.pools.map(poolCard).join("")}</div>${(pd.errors||[]).map(e=>`<div class="warn small">${esc(e)}</div>`).join("")}
   <div class="small mute">当天分时 = 今天成分股的分时涨跌幅（相对各自昨收）等权平均。近 N 日 = 用当前成分股的日K回看，每天等权平均后累计；不是当时的股池，有幸存者偏差。点股池名展开成分股和每日明细。</div>`}
async function togglePool2(k){if(POOLOPEN.has(k))POOLOPEN.delete(k);else POOLOPEN.add(k);const o=cget("/api/overview");const el=$("#homePools");if(el&&o)morph(el,poolTable(o.pools));fillPools()}
function fillPools(){const o=cget("/api/overview"),byk=Object.fromEntries((((o||{}).pools||{}).pools||[]).map(p=>[p.key,p]));
  for(const k of POOLOPEN){const id="#pd_"+k.replace(/[^\w]/g,"_"),p="/api/overview/pool?key="+encodeURIComponent(k);const draw=d=>{const el=$(id);if(!el||!d)return;
   if(d.pending){morph(el,'<div class="empty">后台还在汇总，稍后自动更新</div>');return}morph(el,poolDetail(d,byk[k]))};
   draw(cget(p));load(p).then(draw).catch(()=>{})}}
/* 首页后台自动刷新（第六轮小修）：交易时段按「设置 → 首页」的间隔刷，其余时间 5 分钟一次；页面在后台时暂停，回来马上刷一次。
   只用 morph 更新数字和图，正在看的股池、选中的票、滚动位置都不动；拉取失败保留上一次的数据并用小字标出。 */
let HOMET=null;const HOME={auto:localStorage.getItem("nl_home_auto")!=="0",last:null,fail:null,sess:null};
const hhms=d=>d?d.toTimeString().slice(0,8):"";
async function home(m){clearTimeout(HOMET);const my=SEQ;await loadPNames();const f=await show(["/api/overview"],o=>drawHome(m,o));if(!f)return;HOME.last=new Date();HOME.fail=null;HOME.sess=(f[0]||{}).session||null;drawHomeTick();homePoll(my,0)}
function homeEvery(){const o=cget("/api/overview")||{};const s=o.session||{};return s.trading===false?300:Math.max(5,o.refresh_secs||15)}
function homePoll(my,n){clearTimeout(HOMET);const o=cget("/api/overview");const run=o&&((o.market_job||{}).running||(o.pools_job||{}).running);
  HOMET=setTimeout(()=>homeTick(my,n),run&&n<40?1500:homeEvery()*1000)}
async function homeTick(my,n,force){if(my!==SEQ||TAB!=="home")return;if(!force&&(document.hidden||!HOME.auto)){homePoll(my,n);return}
  try{const o=await load("/api/overview"+(force?"?refresh=1":""));if(my!==SEQ||TAB!=="home")return;drawHome($("#main"),o);HOME.sess=o.session||null;await ptQuiet();HOME.fail=null;HOME.last=new Date()}
  catch(e){HOME.fail=zhErr(e.message)}
  if(my!==SEQ||TAB!=="home")return;drawHomeTick();homePoll(my,n+1)}
/* 股池走势：静默刷新左边列表和右边选中的那只票（不清空、不显示「读取中」） */
async function ptQuiet(){const key=PT.key;if(!key||!$("#ptleft"))return;const r=await load("/api/pool-trend?key="+encodeURIComponent(key));if(key!==PT.key)return;PT.data=r;drawPtLeft();
  const code=PT.code;if(code&&$("#ptright")){const d=await load(`/api/pool-stock/${encodeURIComponent(code)}?pool=${encodeURIComponent(key)}`);if(code===PT.code&&key===PT.key){PT.detail=d;drawPtRight()}}}
function drawHomeTick(){const el=$("#hc_tick");if(!el)return;const s=HOME.sess||{};
  const st=s.label?`<span class="pill ${s.trading?"green":""}" id="hc_sess">${esc(s.label)}</span>`:"";
  morph(el,`${st}<span class="mute num" id="hc_last">最后更新 ${HOME.last?hhms(HOME.last):"—"}</span>${HOME.fail?`<span class="warn-t" id="hc_fail">刷新失败，显示的是 ${HOME.last?hhms(HOME.last):"上一次"} 的数据（${esc(HOME.fail)}）</span>`:""}<label class="inl"><input type="checkbox" id="hc_auto" ${HOME.auto?"checked":""} onchange="homeAuto(this.checked)"> 自动刷新<span class="mute">${s.trading===false?"（非交易时段每 5 分钟一次）":`（每 ${homeEvery()} 秒）`}</span></label>`)}
function homeAuto(on){HOME.auto=on;localStorage.setItem("nl_home_auto",on?"1":"0");drawHomeTick();if(on)homeTick(SEQ,0)}
async function homeRefresh(b){if(b)b.disabled=true;try{await homeTick(SEQ,0,true)}finally{if(b)b.disabled=false}}
document.addEventListener("visibilitychange",()=>{if(!document.hidden&&TAB==="home"&&HOME.auto)homeTick(SEQ,0)});
