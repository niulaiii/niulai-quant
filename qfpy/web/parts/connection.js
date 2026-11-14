/* ---------- 连接状态 ---------- */
const CONN={connected:["已连接","green"],reachable:["客户端未连接","amber"],auth_failed:["口令错误","red"],unreachable:["连不上","red"],error:["异常","red"],unknown:["未检查",""]};
function connState(){return (BK&&BK.conn&&BK.conn.state)||"unknown"}
function connColor(){return {green:"var(--down)",amber:"var(--warn)",red:"var(--up)","":"var(--mute2)"}[(CONN[connState()]||CONN.unknown)[1]]}
function connPill(){const [t,c]=CONN[connState()]||CONN.unknown;return `<span class="pill ${c} ${c==="green"?"live":""}" title="下单程序 ${esc(BK?BK.base_url:"")}"><span class="d"></span>下单程序：${t}</span>`}
async function loadBroker(){try{BK=await load("/api/broker")}catch(e){BK=BK||cget("/api/broker")}const d=$("#navdot");if(d)d.style.background=connColor();return BK}
function moneyRow(mn){if(!mn||!Object.keys(mn).length)return "";const L=[["total_asset","总资产"],["market_value","总市值"],["available","可用"],["balance","资金余额"],["position_pnl","持仓盈亏"],["today_pnl","当日盈亏"]];
  return `<div class="kpis">${L.filter(([k])=>mn[k]!=null).map(([k,l])=>{const v=parseFloat(String(mn[k]).replace(/,/g,""));const cls=(k.includes("pnl")&&v)?(v>0?"up":"down"):"";return `<div class="kpi"><div class="l">${l}</div><div class="v ${cls}">${isNaN(v)?esc(mn[k]):money(v)}</div></div>`}).join("")}</div>`}
function brokerPosTable(list,local){if(!list||!list.length)return '<div class="empty">券商没有持仓（或持仓表没读到）</div>';const lm=local||{};
  return `<div class="tw"><table><tr><th>代码</th><th>名称</th><th class="n">持仓</th><th class="n">可卖</th><th class="n">成本</th><th class="n">现价</th><th class="n">市值</th><th class="n">盈亏</th><th class="n">盈亏%</th><th>本地记录</th></tr>${list.map(p=>{const l=lm[p.symbol];
   const tag=!l?'<span class="pill">非本系统</span>':(l.quantity===p.qty?'<span class="pill green">一致</span>':`<span class="pill red">本地 ${l.quantity}</span>`);
   return `<tr><td class="num"><b>${p.symbol}</b>${p.code_verified?"":' <span class="pill amber" title="代码没核实">?</span>'}</td><td>${esc(p.name)}</td><td class="n">${p.qty}</td><td class="n">${p.available}</td><td class="n">${(+p.cost).toFixed(3)}</td><td class="n">${(+p.price).toFixed(3)}</td><td class="n">${money(p.market_value)}</td><td class="n ${p.pnl>0?"up":p.pnl<0?"down":""}">${money(p.pnl)}</td><td class="n">${pct(p.pnl_pct)}</td><td>${tag}</td></tr>`}).join("")}</table></div>`}
const EVK={buy_filled:"green",sell_filled:"green",auto_confirmed:"green",unknown_found:"blue",buy_filled_inferred:"blue",sell_filled_inferred:"amber",positions_stale:"",manual_sell_detected:"amber"};
function eventList(ev){if(!ev||!ev.length)return '<div class="empty">还没有对账事件</div>';
  return `<div class="tw"><table><tr><th>时间</th><th>代码</th><th>事件</th><th>推送</th></tr>${ev.map(e=>`<tr><td class="num small">${esc(e.at||"")}</td><td class="num">${esc(e.symbol)}</td><td class="small"><span class="pill ${EVK[e.kind]||(e.notify?"red":"")}">${esc(e.kind)}</span> ${esc(e.message)}</td><td>${e.notify?'<span class="pill red">已推送</span>':""}</td></tr>`).join("")}</table></div>`}

