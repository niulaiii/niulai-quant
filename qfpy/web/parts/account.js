/* ---------------- 账户（资金 · 持仓 · 委托 · 手动下单） ---------------- */
let ORD={side:"buy",symbol:"",ptype:"limit",price:"",qty:"",q:null,qerr:"",qbusy:false,last:null};
let PF={code:"",name:"",qty:"",cost:"",sell:"",edit:false},FF=null,OFILT="all",ACCT=null;
const OPEN_ST=["submitted","partial","unknown"];
/* 账户页每 15 秒原地刷新一次（只读本系统的接口，不调用下单程序） */
function acctPoll(my){setTimeout(async()=>{if(my!==SEQ||TAB!=="account")return;if(!document.hidden){try{const [a,pv]=await Promise.all([load("/api/account"),load("/api/sell/preview")]);if(my!==SEQ||TAB!=="account")return;drawAccount($("#main"),a,pv)}catch(e){}}acctPoll(my)},15000)}
function redrawAcct(){if(TAB==="account")drawAccount($("#main"),cget("/api/account"),cget("/api/sell/preview"))}
async function reloadAcct(refreshPreview){if(refreshPreview){try{await api("/api/sell/preview?refresh=1",{quiet:true})}catch(e){}}
  try{const [a,pv]=await Promise.all([load("/api/account"),load("/api/sell/preview")]);if(TAB==="account")drawAccount($("#main"),a,pv);pollPreview()}catch(e){}refreshHeader()}
function drawAccount(m,a,pv){if(!m||!a)return;ACCT=a;const auto=!!a.auto_positions;
  m.innerHTML=`${posSrcCard(a)}${auto?brokerCard(a):fundsCard(a)}
   <div class="card" id="poscard"><h3>持仓${auto?"（券商）":""} <span class="r"></span></h3><div id="accPos">${auto?autoPosTable(a,pv):posTable(a,pv,true)}</div>${auto?"":posForm(a)}</div>
   ${orderCard(a)}
   ${ordersCard(a)}
   ${perfCard(a)}`}
/* 自动获取持仓：只在这里切换 */
function posSrcCard(a){const auto=!!a.auto_positions,live=a.mode==="live";
  return `<div class="card srcbar" id="pmcard"><button class="switch ${auto?"on":""}" id="possw" ${live||auto?"":"disabled"} onclick="setPosAuto(${!auto})" aria-label="自动获取持仓"></button>
   <div><b>自动获取持仓 ${auto?'<span class="down">开</span>':'<span class="mute">关</span>'}</b><div class="small mute">${auto?"资金和持仓从下单程序读取（券商数据）。连接地址和口令在「设置 → 下单程序」。":
     live?"关闭：不读取任何券商数据。资金、持仓、委托都在本页手动填写，系统发出的委托自动记录。":"只有实盘能打开。现在用本地账本：资金和持仓可以手动填写，按转入/转出处理，收益率不跳变。"}</div></div></div>`}
/* 手动账本：资金 */
function fundsCard(a){const s=a.summary||{},fu=a.funds||{},paper=a.mode==="paper";if(!FF)FF={available:fu.available??s.cash??"",balance:fu.balance??""};
  const K=(l,v,cls,id)=>`<div class="kpi"${id?` id="${id}"`:""}><div class="l">${l}</div><div class="v num ${cls||""}">${v}</div></div>`,sg=v=>v>0?"up":v<0?"down":"";
  return `<div class="card" id="fundcard"><h3>资金${fu.updated_at?` <span class="r small mute num">修改于 ${esc(fu.updated_at)}</span>`:""}</h3>
   <div class="kpis">${K("可用资金",money(s.cash),"","k_avail")}${K("资金余额",money(fu.balance??s.cash),"","k_bal")}${K("持仓市值",money(s.market_value))}${K("总资产（估）",money(s.equity))}${K("浮动盈亏",money(s.unrealized_pnl),sg(s.unrealized_pnl))}${K("已实现盈亏",money(s.total_realized_pnl),sg(s.total_realized_pnl))}${paper?K("总收益",pct(s.total_return_pct)):""}</div>
   <div class="frow" id="fundform"><label>可用资金 <input id="fd_avail" type="number" step="0.01" value="${esc(FF.available)}" oninput="FF.available=this.value"></label><label>资金余额 <input id="fd_bal" type="number" step="0.01" value="${esc(FF.balance)}" placeholder="可空" oninput="FF.balance=this.value"></label><button class="btn" id="fd_save" onclick="saveFunds(this)">保存资金</button></div>
   <div class="small mute">按同花顺里的数字填。资金余额 = 可用 + 冻结，可以不填。委托有新成交时，可用资金自动跟着变。${paper?"模拟盘按转入/转出处理：初始资金同步调整，收益率不跳变。":""}</div></div>`}
async function saveFunds(b){const av=String(FF.available).trim(),bl=String(FF.balance??"").trim();if(av===""||isNaN(+av)||+av<0){toast("可用资金要填一个不小于 0 的数",true);return}
  b.disabled=true;try{await api("/api/funds/manual",{method:"POST",body:{available:+av,balance:bl===""?null:+bl}});toast("资金已保存");FF=null;await reloadAcct()}finally{b.disabled=false}}
/* 手动账本：持仓增改删 */
function posForm(a){return `<div class="frow" id="posform" style="margin-top:10px"><b class="small">${PF.edit?"修改持仓":"添加持仓"}</b>
   <label>代码 <input id="mn_code" maxlength="6" style="width:84px" value="${esc(PF.code)}" ${PF.edit?"readonly":""} oninput="PF.code=this.value.trim()"></label>
   <label>名称 <input id="mn_name" style="width:96px" value="${esc(PF.name)}" placeholder="可空" oninput="PF.name=this.value"></label>
   <label>数量 <input id="mn_qty" type="number" step="100" style="width:90px" value="${esc(PF.qty)}" oninput="PF.qty=this.value"></label>
   <label>成本 <input id="mn_cost" type="number" step="0.001" style="width:90px" value="${esc(PF.cost)}" oninput="PF.cost=this.value"></label>
   <label title="今天就能卖的部分，其余按 T+1">今天可卖 <input id="mn_sell" type="number" step="100" style="width:90px" value="${esc(PF.sell)}" oninput="PF.sell=this.value"></label>
   <button class="btn primary" id="mn_save" onclick="manSave(this)">${PF.edit?"保存修改":"添加"}</button>${PF.edit||PF.code?`<button class="btn ghost" onclick="PF={code:'',name:'',qty:'',cost:'',sell:'',edit:false};redrawAcct()">清空</button>`:""}</div>
   <div class="small mute">有在途委托或待确认的股票不能手动改。可卖以外的部分按 T+1 处理。</div>`}
function posEdit(c){const p=((ACCT&&ACCT.positions)||[]).find(x=>x.symbol===c);if(!p)return;PF={code:c,name:p.name||"",qty:String(p.quantity),cost:String(p.avg_cost),sell:String(p.sellable??p.quantity),edit:true};redrawAcct();const i=$("#mn_qty");if(i){i.focus();i.scrollIntoView({block:"center"})}}
/* 自动获取持仓打开时：只有这一块涉及下单程序和券商数据 */
function brokerCard(a){const b=a.broker||{},sy=b.sync||{},fr=b.freshness||{};
  return `<div class="card" id="autopos"><h3>下单程序与券商数据 ${fr.fresh?`<span class="r small mute num">数据时间 ${esc(fr.data_time||"")}${fr.broker_cached?" · 券商缓存":""}</span>`:""}</h3>
   <div class="row" style="margin-bottom:10px"><button class="btn" id="bkconnect" ${sy.running?"disabled":""} onclick="bsync('status',this)">连接服务器</button><button class="btn primary" id="bkrefresh" ${sy.running?"disabled":""} onclick="bsync('fresh',this)">刷新</button>${sy.running?'<span class="small mute">正在后台读取，完成后自动更新</span>':""}</div>
   ${sy.sync_error&&!sy.running?`<div class="warn bad small">上次读取失败：${esc(sy.sync_error)}</div>`:""}
   ${fr.fresh&&b.money?moneyRow(b.money):`<div class="empty">${fr.data_time?`上次数据（${esc(fr.data_time)}）已过期，不再显示。`:"还没有读到数据。"}点「刷新」读取最新的资金和持仓。</div>`}
   ${b.stale_positions?'<div class="warn small">这次没读到可信的持仓表，本地持仓没有按它删减。</div>':""}</div>`}
function autoPosTable(a,pv){const b=a.broker||{},fr=b.freshness||{},list=fr.fresh?(b.positions||[]):null;if(!list)return '<div class="empty">拿到最新数据前不显示持仓。</div>';if(!list.length)return '<div class="empty">券商没有持仓</div>';
  const lm=Object.fromEntries((a.positions||[]).map(p=>[p.symbol,p])),ev=Object.fromEntries(((pv&&pv.results)||[]).map(r=>[r.symbol,r]));
  return `<div class="tw"><table class="pos"><tr><th>股票</th><th class="n">持仓 / 可卖</th><th class="n">成本 / 现价</th><th class="n">盈亏</th><th>卖出因子（红=触发，黄=今天到点）</th><th>保留</th></tr>${list.map(p=>{const l=lm[p.symbol],e=ev[p.symbol]||(l&&l.exit_preview)||null;
   return `<tr id="bp_${p.symbol}"><td><b>${p.symbol}</b> ${esc(p.name)}${p.code_verified?"":' <span class="pill amber" title="代码没核实">?</span>'}${l?"":' <span class="pill" title="不是本系统买入的，不自动卖">非本系统</span>'}</td><td class="n">${p.qty}<div class="small mute">可卖 ${p.available}</div></td><td class="n">${(+p.cost).toFixed(3)}<div class="small mute">现 ${(+p.price).toFixed(3)}</div></td>
    <td class="n ${p.pnl>0?"up":p.pnl<0?"down":""}">${money(p.pnl)}<div class="small">${pct(p.pnl_pct)}</div></td><td class="sellcol">${e?sellChips(e):'<span class="small mute">-</span>'}</td><td>${l?holdCell(l):""}</td></tr>`}).join("")}</table></div>`}
/* 手动下单 */
function orderCard(a){const q=ORD.q,buy=ORD.side==="buy",mkt=ORD.ptype==="market",px=mkt?(q&&q.price):+ORD.price,amt=px&&+ORD.qty?px*ORD.qty:null;
  const seg=(k,v,t,cls)=>`<button class="${ORD[k]===v?"on "+(cls||""):""}" onclick="ordSet('${k}','${v}')">${t}</button>`;
  return `<div class="card" id="ordcard"><h3>手动下单</h3><div class="ordgrid"><div class="ordform">
    <div class="seg" id="ordside">${seg("side","buy","买入","buy")}${seg("side","sell","卖出","sell")}</div>
    <label>代码 <input id="ord_code" maxlength="6" inputmode="numeric" placeholder="6 位代码" value="${esc(ORD.symbol)}" oninput="ordCode(this.value)"></label>
    <div class="seg" id="ordptype">${seg("ptype","limit","限价")}${seg("ptype","market","市价")}</div>
    <label>价格 <input id="ord_price" type="number" step="0.01" value="${mkt?"":esc(ORD.price)}" ${mkt?'disabled placeholder="按现价 ±1% 转限价"':""} oninput="ORD.price=this.value;ordCalc()"></label>
    ${q&&!mkt?`<div class="row small"><button class="btn small ghost" onclick="ordPx(${q.price})">现价</button>${q.limit_up?`<button class="btn small ghost" onclick="ordPx(${q.limit_up})">涨停</button>`:""}${q.limit_down?`<button class="btn small ghost" onclick="ordPx(${q.limit_down})">跌停</button>`:""}</div>`:""}
    <label>数量 <input id="ord_qty" type="number" step="100" min="0" value="${esc(ORD.qty)}" placeholder="股" oninput="ORD.qty=this.value;ordCalc()"></label>
    ${q&&!buy&&q.sellable?`<div class="row small"><button class="btn small ghost" onclick="ORD.qty='${q.sellable}';redrawOrd()">全部可卖 ${q.sellable}</button></div>`:""}
    <div class="small mute num" id="ordcalc">${amt?`金额约 ${money(amt)} 元`:" "}</div>
    <button class="btn ${buy?"danger":"primary"}" id="ordsubmit" onclick="ordReview()">${buy?"买入":"卖出"}…</button></div>
   <div class="ordquote" id="ordquote">${quoteBox()}</div></div>
   <div id="ordres">${ORD.last?ordResult(ORD.last):""}</div>
   <div class="small mute" style="margin-top:8px">手动单照样过风控（买入看单笔上限、每日金额；卖出看可卖数量、T+1），不受自动交易开关限制。实盘发给下单程序，模拟盘本地模拟成交，演练只生成委托请求。</div></div>`}
function quoteBox(){const q=ORD.q;if(ORD.qbusy&&!q)return '<div class="empty">查询中…</div>';if(ORD.qerr)return `<div class="warn small">${esc(ORD.qerr)}</div>`;
  if(!q)return '<div class="empty">输入 6 位代码后，这里显示名称、现价、涨跌停价和可卖数量</div>';
  const R=(l,v,id)=>`<div class="qrow"${id?` id="${id}"`:""}><span class="mute">${l}</span><b class="num">${v}</b></div>`;
  return `<div class="qname"><b>${esc(q.name||"")}</b> <span class="num mute">${esc(q.symbol)}</span></div>${R("现价",`${f2(q.price)} ${q.preclose?pct((q.price/q.preclose-1)*100):""}`,"q_price")}${R("涨停价",`<span class="up">${f2(q.limit_up)}</span>`,"q_up")}${R("跌停价",`<span class="down">${f2(q.limit_down)}</span>`,"q_down")}${R("持仓",q.held??0)}${R("可卖",q.sellable??0,"q_sell")}${R("可用资金",money(q.cash??(ACCT&&ACCT.summary&&ACCT.summary.cash)))}
   ${q.error?`<div class="warn small">${esc(q.error)}</div>`:""}<div class="small mute">行情源 ${esc(q.source||"-")} · 涨跌停按 ±${q.limit_pct??"-"}% 计算</div>`}
function redrawOrd(){const el=$("#ordcard");if(el&&ACCT){const t=document.createElement("template");_IH.set.call(t,orderCard(ACCT));morph(el,_IH.get.call(t.content.firstElementChild))}}
function ordCalc(){const q=ORD.q,px=ORD.ptype==="market"?(q&&q.price):+ORD.price,amt=px&&+ORD.qty?px*ORD.qty:null;const el=$("#ordcalc");if(el)el.textContent=amt?`金额约 ${money(amt)} 元`:"\u00a0"}
function ordSet(k,v){ORD[k]=v;redrawOrd()}
function ordPx(v){ORD.price=String(v);redrawOrd()}
let ORDQT=null;
function ordCode(v){v=v.trim();ORD.symbol=v;clearTimeout(ORDQT);if(!/^\d{6}$/.test(v)){ORD.q=null;ORD.qerr="";redrawOrd();return}
  ORD.qbusy=true;ORD.q=null;ORD.qerr="";redrawOrd();ORDQT=setTimeout(async()=>{try{const q=await api("/api/trade/quote/"+v,{quiet:true,timeout:15000});if(ORD.symbol!==v)return;ORD.q=q;if(!ORD.price&&q.price)ORD.price=String(q.price)}catch(e){if(ORD.symbol===v)ORD.qerr="查询失败："+e.message}finally{ORD.qbusy=false;redrawOrd()}},250)}
function ordReview(){const q=ORD.q,buy=ORD.side==="buy",mkt=ORD.ptype==="market",qty=+ORD.qty,px=+ORD.price;
  if(!/^\d{6}$/.test(ORD.symbol)){toast("先填 6 位代码",true);return}if(!(qty>0)){toast("数量要大于 0",true);return}if(!mkt&&!(px>0)){toast("限价单要填价格",true);return}
  if(mkt&&!(q&&q.price)){toast("没有现价，市价单发不了；请改用限价",true);return}
  const ref=mkt?q.price:px,lim=mkt?Math.round(q.price*(buy?1.01:0.99)*100)/100:px,md=(ST&&ST.mode)||(ACCT&&ACCT.mode),dest={live:"发给下单程序（真实委托）",paper:"模拟盘本地模拟成交",dry_run:"演练：只生成委托请求，不发出"}[md]||md;
  const rows=[["方向",buy?'<b class="up">买入</b>':'<b class="down">卖出</b>'],["股票",`<b>${esc(ORD.symbol)}</b> ${esc((q&&q.name)||"")}`],["委托类型",mkt?"市价":"限价"],["委托价",mkt?`约 ${f2(lim)}（现价 ${f2(q.price)} ${buy?"+":"−"}1% 转成限价）`:f2(px)],["数量",`${qty} 股`],["金额约",money(lim*qty)+" 元"],
    ...(q?[["现价 / 涨停 / 跌停",`${f2(q.price)} / ${f2(q.limit_up)} / ${f2(q.limit_down)}`],[buy?"可用资金":"可卖",buy?money(q.cash??(ACCT&&ACCT.summary&&ACCT.summary.cash)):String(q.sellable??0)]]:[]),["去向",esc(dest)]];
  const warn=[];if(q&&!buy&&qty>(q.sellable||0))warn.push(`卖出数量超过可卖（${q.sellable||0}），风控会拒绝`);if(qty%100&&buy)warn.push("买入数量不是 100 的整数倍");if(!mkt&&q&&((q.limit_up&&px>q.limit_up)||(q.limit_down&&px<q.limit_down)))warn.push("委托价超出涨跌停范围");
  modal(`<h3>${md==="live"?'<span class="up">确认实盘委托</span>':"确认委托"}</h3><table class="kvt">${rows.map(([k,v])=>`<tr><td class="mute">${k}</td><td>${v}</td></tr>`).join("")}</table>${warn.map(w=>`<div class="warn small">${esc(w)}</div>`).join("")}<div class="small mute" style="margin-top:8px">提交后先过风控，被拒会说明原因。</div>`,
    buy?"确认买入":"确认卖出",()=>ordSubmit(),"ordconfirm")}
async function ordSubmit(){const b=$("#ordsubmit");if(b)b.disabled=true;
  try{const r=await api("/api/trade/order",{method:"POST",timeout:60000,body:{symbol:ORD.symbol,side:ORD.side,price_type:ORD.ptype,price:ORD.ptype==="market"?null:+ORD.price,quantity:+ORD.qty}});
    const o={...(r.result||{}),...(r.order||{})};ORD.last=o;const ok=!["risk_rejected","rejected","unknown"].includes(o.status);toast(`${ok?"已提交":"被拒"}：${o.status_label||o.status||""} ${o.message||""}`,!ok);if(ok){ORD.qty=""}await reloadAcct(true)}
  catch(e){ORD.last={status:"error",status_label:"提交失败",message:e.message};redrawOrd()}finally{if(b)b.disabled=false}}
function ordResult(r){const bad=["risk_rejected","rejected","unknown","error"].includes(r.status);
  return `<div class="${bad?"warn":"ok-box"} small" style="margin-top:8px"><b>上一笔：</b>${esc(r.side==="sell"?"卖":r.side==="buy"?"买":"")} ${esc(r.symbol||"")} ${esc(r.name||"")} ${r.quantity?r.quantity+" 股":""} ${r.price?"@ "+f2(r.price):""} → <b>${esc(r.status_label||r.status||"")}</b> ${esc(r.message||"")}${r.checks&&r.checks.length?`<div>${r.checks.filter(c=>!c.ok).map(c=>`✗ ${esc(c.name)}：${esc(c.note)}`).join("<br>")}</div>`:""} <button class="btn small ghost" onclick="ORD.last=null;redrawOrd()">知道了</button></div>`}
/* 通用弹窗：确定前列出全部要素 */
function modal(html,okText,onOk,id,danger){const d=document.createElement("div");d.className="modal-bg";d.id=id||"modal";
  d.innerHTML=`<div class="modal">${html}<div class="row" style="justify-content:flex-end;margin-top:14px"><button class="btn" data-act="no">取消</button><button class="btn ${danger===false?"primary":"danger"}" data-act="ok">${esc(okText)}</button></div></div>`;
  document.body.appendChild(d);const close=()=>d.remove();d.querySelector('[data-act="no"]').onclick=close;d.addEventListener("click",e=>{if(e.target===d)close()});
  d.querySelector('[data-act="ok"]').onclick=async()=>{const r=await onOk(d);if(r!==false)close()};return d}
/* 委托记录 */
const JST={submitted:"blue",partial:"amber",filled:"green",cancelled:"",partial_cancelled:"amber",rejected:"red",risk_rejected:"red",unknown:"red",dry_run:"blue",signal_only:"amber",hold_skipped:"amber"};
function ordersCard(a){const all=a.orders||[],today=(ST&&ST.now||"").slice(0,10),live=a.mode==="live";
  const sig=o=>o.status==="signal_only"||o.status==="hold_skipped"||o.status==="risk_rejected";
  const F={all:o=>!sig(o),today:o=>(o.created_at||o.time||"").startsWith(today),open:o=>o.open??OPEN_ST.includes(o.status),signal:sig};const list=all.filter(F[OFILT]||F.all);
  const nOpen=all.filter(F.open).length,nToday=all.filter(F.today).length,nSig=all.filter(sig).length;
  const fb=(k,t)=>`<button class="${OFILT===k?"on":""}" onclick="OFILT='${k}';redrawAcct()">${t}</button>`;
  return `<div class="card" id="ordlist"><h3>委托记录 <span class="r">${live?`<button class="btn small" id="ordrefresh" onclick="ordRefresh(this)">刷新委托状态</button>`:""}<button class="btn small" id="ordadd" onclick="ordEdit()">补记一笔</button></span></h3>
   <div class="seg small" id="ordfilt" style="margin-bottom:8px">${fb("all","委托 "+(all.length-nSig))}${fb("today","今天 "+nToday)}${fb("open","未完成 "+nOpen)}${fb("signal","只记信号/拦截 "+nSig)}</div>
   ${list.length?`<div class="tw"><table class="ordt"><tr><th>时间</th><th>股票</th><th>方向</th><th class="n">委托价</th><th class="n">数量</th><th class="n">已成交</th><th class="n">成交均价</th><th>状态</th><th>来源</th><th></th></tr>${list.map(o=>{const open=o.open??OPEN_ST.includes(o.status),canCx=open&&live&&o.origin!=="手动补记";
    return `<tr id="jo_${esc(o.id)}"><td class="small num">${esc((o.created_at||o.time||"").slice(5,19))}</td><td><b>${esc(o.symbol)}</b> ${esc(o.name||"")}</td><td>${o.side==="buy"?'<span class="up">买</span>':'<span class="down">卖</span>'}</td><td class="n">${f2(o.price)}${o.price_type==="market"?'<div class="small mute">市价转限价</div>':""}</td><td class="n">${o.quantity??""}</td><td class="n">${o.filled_qty||0}</td><td class="n">${o.fill_price?f2(o.fill_price):"-"}</td>
     <td><span class="pill ${JST[o.status]||""}">${esc(o.status_label||o.status)}</span>${o.entrust_no?`<div class="small mute num">合同 ${esc(o.entrust_no)}</div>`:""}${o.cancel_status?`<div class="small mute">撤单：${esc(o.cancel_status==="cancel_submitted"?"已提交":o.cancel_status)}</div>`:""}${o.message||o.reason?`<div class="small mute">${esc(o.message||o.reason)}</div>`:""}</td><td class="small">${esc(o.origin||"")}${o.strategy&&o.strategy!==o.origin&&o.strategy!=="手动"?`<div class="mute">${esc(o.strategy)}</div>`:""}</td>
     <td class="nowrap">${canCx?`<button class="btn small danger" id="cx_${esc(o.id)}" onclick="ordCancel('${esc(o.id)}')">撤单</button> `:""}${sig(o)?"":`<button class="btn small ghost" onclick="ordEdit('${esc(o.id)}')">修改</button>`}${o.origin==="手动补记"?` <button class="btn small ghost" onclick="ordDel('${esc(o.id)}')">删除</button>`:""}</td></tr>`}).join("")}</table></div>`:'<div class="empty">没有符合条件的委托</div>'}
   <div class="small mute" style="margin-top:6px">系统发出的每笔委托自动记录。在同花顺里自己下的单可以「补记一笔」；修改成交数量后，新增的成交自动计入持仓和可用资金，已记账的成交不能改小。${live?"未成交或部分成交的委托可以按合同号撤单。":""}</div></div>`}
async function ordCancel(id){const o=((ACCT&&ACCT.orders)||[]).find(x=>x.id===id);if(!o)return;
  modal(`<h3>撤单</h3><table class="kvt"><tr><td class="mute">股票</td><td><b>${esc(o.symbol)}</b> ${esc(o.name||"")}</td></tr><tr><td class="mute">方向</td><td>${o.side==="buy"?"买入":"卖出"}</td></tr><tr><td class="mute">委托</td><td>${f2(o.price)} × ${o.quantity}（已成交 ${o.filled_qty||0}）</td></tr><tr><td class="mute">合同号</td><td class="num">${esc(o.entrust_no||"还没对上，撤单前先读一次委托表")}</td></tr></table><div class="small mute">只按合同号撤，撤完自动刷新委托状态。</div>`,"确认撤单",async()=>{
    const r=await api("/api/trade/cancel/"+encodeURIComponent(id),{method:"POST",timeout:60000,body:{}}).catch(()=>null);const rr=r&&r.result||{};if(r)toast(`撤单${rr.status==="cancel_submitted"?"已提交":"失败"}：${rr.message||""}`,rr.status!=="cancel_submitted");await reloadAcct()},"cxconfirm")}
async function ordRefresh(b){b.disabled=true;try{const r=await api("/api/orders/refresh",{method:"POST",timeout:60000,body:{}});toast(r&&r.message?r.message:"委托状态已刷新");await reloadAcct()}finally{b.disabled=false}}
async function ordDel(id){if(!confirm("删除这条手动补记的委托？（已计入持仓的成交不会回退）"))return;await api("/api/orders/manual/"+encodeURIComponent(id),{method:"DELETE"});toast("已删除");await reloadAcct()}
function ordEdit(id){const o=id?((ACCT&&ACCT.orders)||[]).find(x=>x.id===id):null,v=o||{side:"buy",status:"filled",time:""};
  const STS=(ACCT&&ACCT.order_statuses)||[["submitted","已报"],["partial","部分成交"],["filled","已成交"],["cancelled","已撤"],["partial_cancelled","部成部撤"],["rejected","废单"]];
  const I=(k,l,t,val,ex)=>`<label class="fl"><span>${l}</span><input data-k="${k}" type="${t||"text"}" value="${esc(val??"")}" ${ex||""}></label>`;
  modal(`<h3>${o?"修改委托":"补记一笔委托"}</h3><div class="fgrid">${I("symbol","代码","text",v.symbol,`maxlength="6" ${o?"readonly":""}`)}${I("name","名称","text",v.name)}
    <label class="fl"><span>方向</span><select data-k="side" ${o?"disabled":""}><option value="buy" ${v.side==="buy"?"selected":""}>买入</option><option value="sell" ${v.side==="sell"?"selected":""}>卖出</option></select></label>
    ${I("price","委托价","number",v.price,'step="0.01"')}${I("quantity","委托数量","number",v.quantity,'step="100"')}
    <label class="fl"><span>状态</span><select data-k="status">${STS.map(([k,l])=>`<option value="${k}" ${v.status===k?"selected":""}>${l}</option>`).join("")}</select></label>
    ${I("filled_qty","已成交数量","number",v.filled_qty||(o?0:""),'step="100"')}${I("fill_price","成交均价","number",v.fill_price,'step="0.001"')}${I("fee","费用","number",v.fee,'step="0.01"')}${I("entrust_no","合同号","text",v.entrust_no)}${I("time","时间","text",(v.created_at||v.time||""),'placeholder="空 = 现在"')}${I("strategy","备注/策略","text",v.strategy)}</div>
    <div class="small mute">成交数量比上次多出的部分会计入持仓和可用资金；已记账的成交不能改小。</div>`,"保存",async d=>{
    const bd={};d.querySelectorAll("[data-k]").forEach(e=>{const k=e.dataset.k,x=e.value.trim();if(x!=="")bd[k]=e.type==="number"?+x:x});if(o){bd.id=o.id;bd.side=o.side;bd.symbol=o.symbol}
    if(!/^\d{6}$/.test(bd.symbol||"")){toast("代码应为 6 位数字",true);return false}
    try{const r=await api("/api/orders/manual",{method:"POST",body:bd});const ap=r&&r.applied;toast("委托已保存"+(ap&&ap.qty?`，计入成交 ${ap.qty} 股`:""));await reloadAcct(true)}catch(e){return false}},"ordedit",false)}
/* 收益与已平仓（折叠） */
function perfCard(a){const s=a.summary||{};
  return `<details class="card" id="perf"><summary><b>收益与已平仓</b> <span class="small mute">已实现 ${money(s.total_realized_pnl)} · 胜率 ${s.win_rate??"-"}%（${s.win_count??0} 胜 / ${s.lose_count??0} 负）${a.mode==="paper"?" · 最大回撤 "+f2(s.max_drawdown_pct)+"%":""}</span></summary>
   <div class="grid2" style="margin-top:10px"><div><div class="small mute">收益曲线</div>${curve((a.nav||[]).map(x=>[x.date,x.equity]))}</div>
   <div><div class="small mute">已平仓明细</div>${(a.closed||[]).length?`<div class="tw" style="max-height:260px;overflow:auto"><table><tr><th>日期</th><th>股票</th><th class="n">数量</th><th class="n">卖价</th><th class="n">盈亏</th><th>原因</th></tr>${a.closed.map(c=>`<tr><td class="num">${c.date}</td><td>${c.symbol} ${esc(c.name)}</td><td class="n">${c.quantity}</td><td class="n">${f2(c.sell_price)}</td><td class="n ${c.pnl>0?"up":"down"}">${money(c.pnl)}</td><td class="small">${esc(c.reason)}</td></tr>`).join("")}</table></div>`:'<div class="mute small">无</div>'}</div></div></details>`}
/* 股池走势：当天分时（等权）+ 近 N 日累计。只有一个点时从 0 起画，仍是一条像样的线 */
function lineChart(pts,o={}){const W=o.w||300,H=o.h||86,pl=34,pr=6,pt=8,pb=16;pts=(pts||[]).filter(p=>p&&p.v!=null&&isFinite(p.v));
  if(!pts.length)return `<div class="chart empty small" style="height:${H}px">${esc(o.empty||"暂无数据")}</div>`;
  if(pts.length===1)pts=[{t:o.origin||"起点",v:0},pts[0]];
  const vs=pts.map(p=>p.v);let lo=Math.min(0,...vs),hi=Math.max(0,...vs);if(hi-lo<0.5){const c=(hi+lo)/2;lo=c-0.25;hi=c+0.25}const pad=(hi-lo)*0.08;lo-=pad;hi+=pad;
  const X=i=>pl+(W-pl-pr)*(pts.length===1?0.5:i/(pts.length-1)),Y=v=>pt+(H-pt-pb)*(1-(v-lo)/(hi-lo)),last=pts[pts.length-1].v,col=last>0?"var(--up)":last<0?"var(--down)":"var(--mute)";
  const line=pts.map((p,i)=>`${X(i).toFixed(1)},${Y(p.v).toFixed(1)}`).join(" "),y0=Y(0).toFixed(1);
  const ti=o.ticks||[0,Math.floor((pts.length-1)/2),pts.length-1],tk=[...new Set(ti.filter(i=>i>=0&&i<pts.length))];
  const dots=o.dots?pts.map((p,i)=>`<circle cx="${X(i).toFixed(1)}" cy="${Y(p.v).toFixed(1)}" r="2.6" fill="${p.v>0?"var(--up)":p.v<0?"var(--down)":"var(--mute)"}"><title>${esc(p.t)}：${p.v>0?"+":""}${(+p.v).toFixed(2)}%${p.tip?" · "+esc(p.tip):""}</title></circle>`).join(""):"";
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:${H}px" role="img" aria-label="${esc(o.label||"")}">
   <line x1="${pl}" x2="${W-pr}" y1="${y0}" y2="${y0}" class="zero"/><text x="${pl-4}" y="${Y(hi-pad)+3}" class="ax" text-anchor="end">${(hi-pad)>0?"+":""}${(hi-pad).toFixed(1)}%</text><text x="${pl-4}" y="${+y0+3}" class="ax" text-anchor="end">0</text><text x="${pl-4}" y="${Y(lo+pad)+3}" class="ax" text-anchor="end">${(lo+pad).toFixed(1)}%</text>
   ${tk.map(i=>`<text x="${X(i).toFixed(1)}" y="${H-3}" class="ax" text-anchor="${i===0?"start":i===pts.length-1?"end":"middle"}">${esc(pts[i].t)}</text>`).join("")}
   <polygon points="${X(0).toFixed(1)},${y0} ${line} ${X(pts.length-1).toFixed(1)},${y0}" fill="${col}" opacity=".12"/><polyline points="${line}" fill="none" stroke="${col}" stroke-width="1.8" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>${dots}</svg>`}
function intraPts(it){return ((it&&it.points)||[]).map(p=>({t:p.t,v:p.v}))}
function lbPts(lb){return ((lb&&lb.days)||[]).map(d=>({t:(d.date||"").slice(5),v:d.cum_pct,tip:`当日 ${d.avg_pct>0?"+":""}${(+d.avg_pct).toFixed(2)}%，涨 ${d.up} / 跌 ${d.down}${d.live?"（盘中）":""}`}))}
function poolCard(p){const open=POOLOPEN.has(p.key),it=p.intraday||{},lb=p.lookback||{},ip=intraPts(it),lp=lbPts(lb),cum=lp.length?lp[lp.length-1].v:null,id=p.key.replace(/[^\w]/g,"_");
  return `<div class="poolc" id="pc_${id}"><div class="poolh clk" onclick="togglePool2('${esc(p.key)}')"><span class="caret">${open?"▾":"▸"}</span><b>${esc(p.name)}</b><span class="small mute">${p.count} 只${p.trading_date?" · "+esc(p.trading_date):""}</span>
    <span class="r num"><span class="small mute">当日</span> <b class="${p.avg_change>0?"up":p.avg_change<0?"down":""}">${pct(p.avg_change)}</b> <span class="small"><span class="up">${p.up}</span>/<span class="down">${p.down}</span></span>${cum!=null?` <span class="small mute">· 近${(lb.days||[]).length}日</span> <b class="${cum>0?"up":cum<0?"down":""}">${pct(cum)}</b>`:""}</span></div>
   <div class="poolcharts"><div><div class="small mute">当天分时 ${it.date?esc(it.date):""}${it.source?` · ${esc(it.source)}`:""}</div>${lineChart(ip,{label:"当天分时",empty:it.error?"分时取不到："+it.error:"还没有分时数据",origin:"昨收",ticks:ip.length>60?[0,ip.findIndex(x=>x.t>="11:30"),ip.length-1]:undefined})}</div>
    <div><div class="small mute">近 ${(lb.days||[]).length||"N"} 个交易日累计</div>${lineChart(lp,{label:"近N日累计",dots:true,empty:lb.error?"日K取不到："+lb.error:"还没有日K数据",origin:"起点",ticks:lp.map((_,i)=>i)})}</div></div>
   <div class="small mute basis">${it.basis?"分时："+esc(it.basis)+"。":""}${lb.basis?"多日："+esc(lb.basis)+"。":""}${p.best?` 领涨 ${esc(p.best.name)} ${pct(p.best.change_pct)}。`:""}${p.error?`<span class="up"> ${esc(p.error)}</span>`:""}</div>
   ${open?`<div class="pooldet" id="pd_${id}"><div class="empty">读取成分股…</div></div>`:""}</div>`}
const PSORT={k:"change_pct",d:-1};
function poolDetail(d,p){const ms=(d.members||[]).slice().sort((a,b)=>{const x=a[PSORT.k],y=b[PSORT.k];return ((x??-1e18)>(y??-1e18)?1:(x??-1e18)<(y??-1e18)?-1:0)*PSORT.d});
  const th=(k,t)=>`<th class="n clk" onclick="PSORT.d=PSORT.k==='${k}'?-PSORT.d:-1;PSORT.k='${k}';fillPools()">${t}${PSORT.k===k?(PSORT.d<0?" ↓":" ↑"):""}</th>`,lb=(d.lookback||(p&&p.lookback)||{}),days=(lb.days||[]).slice().reverse();
  return `<div class="grid2"><div><div class="small mute">成分股（${ms.length}）· 点表头排序</div><div class="tw" style="max-height:340px;overflow:auto"><table><tr><th>股票</th>${th("price","现价")}${th("change_pct","涨跌")}${th("amount","成交额")}</tr>${ms.map(x=>`<tr><td><span class="num">${x.code}</span> ${esc(x.name)}</td><td class="n">${f2(x.price)}</td><td class="n ${x.change_pct>0?"up":x.change_pct<0?"down":""}">${pct(x.change_pct)}</td><td class="n">${x.amount==null?"-":yi(x.amount)}</td></tr>`).join("")}</table></div></div>
   <div><div class="small mute">近 ${days.length} 个交易日（当前成分股回看）</div><div class="tw"><table><tr><th>日期</th><th class="n">当日平均</th><th class="n">累计</th><th class="n">涨 / 跌</th></tr>${days.map(s=>`<tr><td class="num">${esc(s.date)}${s.live?' <span class="pill blue">盘中</span>':""}</td><td class="n ${s.avg_pct>0?"up":s.avg_pct<0?"down":""}">${pct(s.avg_pct)}</td><td class="n ${s.cum_pct>0?"up":s.cum_pct<0?"down":""}">${pct(s.cum_pct)}</td><td class="n"><span class="up">${s.up}</span> / <span class="down">${s.down}</span></td></tr>`).join("")||'<tr><td colspan="4" class="mute">无</td></tr>'}</table></div>
   ${(d.series||[]).length?`<div class="small mute" style="margin-top:8px">本系统的运行记录</div><div class="tw"><table><tr><th>日期</th><th class="n">成分</th><th class="n">入池以来</th><th>入选</th></tr>${d.series.slice().reverse().map(s=>`<tr><td class="num">${esc(s.date)}${s.backfill?' <span class="pill amber">补录</span>':""}</td><td class="n">${s.count}</td><td class="n">${pct(s.avg_since_entry)}</td><td class="small">${esc((s.picked||[]).join("、")||"-")}</td></tr>`).join("")}</table></div>`:""}</div></div>`}
function drawHome(m,o){if(!o)return;const mk=o.market,pd=o.pools;
  const idxH=`<h3>大盘指数 <span class="r small mute num">${mk?`数据日 ${esc(mk.trading_date||"-")} · ${esc(mk.index_source||"-")} · 更新于 ${esc((mk.updated_at||"").slice(11))}`:""} <button class="btn small" onclick="homeRefresh(this)">刷新</button></span></h3>${idxTiles(mk)}`;
  const statH=`<h3>市场情绪</h3>${statsCard(mk)}`;
  const errs=[o.market_job.error&&["大盘行情这次没更新成功，先显示上次的数据",o.market_job.error],o.pools_job.error&&["股池走势这次没更新成功，先显示上次的数据",o.pools_job.error],...(((pd||{}).errors)||[]).map(e=>[zhErr(e),e])].filter(Boolean);
  const errH=errs.map(([t,d])=>`<div class="warn small">${esc(t)}${d&&d!==t?`<details><summary>详情</summary><pre class="detail">${esc(d)}</pre></details>`:""}</div>`).join("");
  if($("#hc_idx")&&$("#homePT")){morph($("#hc_idx"),idxH);morph($("#hc_stat"),statH);morph($("#hc_ptbar"),ptBar(pd));morph($("#hc_err"),errH);if(!PT.data&&PT.key)poolTrend();return}
  m.innerHTML=`<div id="hc_tick" class="hometick small"></div><div class="card" id="hc_idx">${idxH}</div><div class="card" id="hc_stat">${statH}</div>
   <div class="card" id="hc_pool"><div id="hc_ptbar">${ptBar(pd)}</div><div id="homePT"></div></div><div id="hc_err">${errH}</div>`;PT.data=null;poolTrend($("#homePT"))}
function diagHtml(d){if(!d)return '<div class="empty small">读取中…</div>';const c=d.conn||{},C=CONN[c.state]||[c.state||"未知",""];
  return `<div class="small"><span class="pill ${C[1]}" id="diagconn"><span class="d"></span>${esc(C[0])}</span> ${esc(c.message||"")}${c.latency_ms!=null?` · ${c.latency_ms} ms`:""}${c.checked_at?` · 查于 ${esc(c.checked_at.slice(11))}`:""}${c.error?`<div class="up">${esc(c.error)}</div>`:""}</div>
   <div class="small mute">当前生效：${esc(d.base_url||"-")} · 口令来源 ${esc(d.auth||"-")} · 后台监控 ${d.monitor?"开":"关"}${c.executor?" · "+esc(c.executor):""}</div>
   ${(d.calls||[]).length?`<details><summary class="small">最近的接口调用（${d.calls.length}）</summary><div class="tw"><table><tr><th>时间</th><th>请求</th><th>结果</th><th class="n">耗时</th></tr>${d.calls.slice().reverse().map(x=>`<tr><td class="num small">${esc(x.at)}</td><td class="small num">${esc(x.method)} ${esc(x.path)}</td><td class="small">${esc(x.result)}</td><td class="n small">${x.ms??""}</td></tr>`).join("")}</table></div></details>`:""}
   ${(d.events||[]).length?`<details><summary class="small">对账事件（${d.events.length}）</summary>${eventList(d.events)}</details>`:""}`}
async function loadDiag(force){try{const d=await load("/api/broker/diag");const el=$("#xddiag");if(el)morph(el,diagHtml(d))}catch(e){const el=$("#xddiag");if(el&&force)el.innerHTML=`<div class="warn small">${esc(e.message)}</div>`}}
window.addEventListener("hashchange",()=>{const [t,sub]=parseHash(location.hash);const nh="#"+hashOf(t,sub);
  if(t!==TAB||(sub&&sub!==curSub())||(t==="config"&&sub&&sub!==CFGTAB)){go(location.hash.slice(1));return}if(location.hash!==nh)history.replaceState(null,"",nh)});
(()=>{const [t,sub]=parseHash(location.hash);TAB=t;if(t==="config"&&sub)CFGTAB=sub;else if(SUBTABS[t]&&sub)SUB[t]=sub;const nh="#"+hashOf(t,sub);if(location.hash!==nh)history.replaceState(null,"",nh)})();
nav();render();
/* 定时刷新只拉轻量接口，且都带超时；编辑中的页面（策略、配置）不自动重画 */
setInterval(()=>{if(document.hidden)return;refreshHeader()},30000);

