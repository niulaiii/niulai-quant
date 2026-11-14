/* ---------------- 股池走势：左边列表（可排序），右边详情（分时 / 日K、入池日、指标、走到哪一层、持仓的止盈止损） ---------------- */
const PT={key:null,code:null,sort:"change_pct",desc:true,chart:"minute"};
/* 股池走势（第六轮）：内嵌在首页大盘指数、市场情绪下方；上面按股池中文名切换，左边成分股列表，右边单只详情 */
function ptPools(){const o=cget("/api/overview")||{};let ps=((o.pools||{}).pools)||[];
  if(!ps.length&&META)ps=(META.sources||[]).flatMap(s=>(s.pools||[]).map(p=>({key:s.name+":"+p.key,name:p.name||p.key})));return ps}
function ptBar(pd){const ps=ptPools();if(!PT.key||!ps.some(p=>p.key===PT.key))PT.key=(ps[0]||{}).key;
  return `<h3>股池走势 <span class="r small mute num">${pd&&pd.updated_at?"更新于 "+esc((pd.updated_at||"").slice(11)):""}</span></h3>
   ${ps.length?`<div class="chips ptpools">${ps.map(p=>`<button class="chip ${p.key===PT.key?"on":""}" data-pool="${esc(p.key)}" onclick="ptPick('${esc(p.key)}')"><b>${esc(pname(p.key,p.name))}</b>${p.count!=null?` <span class="small mute">${p.count} 只</span>`:""}${p.avg_change!=null?` <span class="small">${pct(p.avg_change)}</span>`:""}</button>`).join("")}</div>`:'<div class="empty">没有配置股池。去「设置 → 股池」勾选。</div>'}`}
function ptPick(k){PT.key=k;PT.code=null;document.querySelectorAll(".ptpools .chip").forEach(b=>b.classList.toggle("on",b.dataset.pool===k));poolTrend()}
async function poolTrend(box){box=box||$("#homePT");if(!box)return;if(!PT.key){const ps=ptPools();PT.key=(ps[0]||{}).key}
  if(!PT.key){box.innerHTML="";return}
  if(!box.querySelector("#ptleft"))box.innerHTML=`<div class="ptwrap"><div class="ptleft" id="ptleft"><div class="empty">读取中…</div></div><div class="ptright" id="ptright"><div class="empty">点左边的一只票看详情</div></div></div>`;
  else{$("#ptright").innerHTML='<div class="empty">点左边的一只票看详情</div>'}
  const key=PT.key;let r;try{r=await load("/api/pool-trend?key="+encodeURIComponent(key))}catch(e){const l=$("#ptleft");if(l)l.innerHTML=`<div class="warn bad">${esc(e.message)}</div>`;return}if(key!==PT.key)return;PT.data=r;drawPtLeft();if(PT.code)ptOpen(PT.code)}
function ptSort(k){if(PT.sort===k)PT.desc=!PT.desc;else{PT.sort=k;PT.desc=true}drawPtLeft()}
function drawPtLeft(){const r=PT.data,el=$("#ptleft");if(!el||!r)return;const rows=(r.rows||[]).slice(),k=PT.sort;rows.sort((a,b)=>{const x=a[k],y=b[k];if(x==null&&y==null)return 0;if(x==null)return 1;if(y==null)return -1;return (typeof x==="string"?x.localeCompare(y):x-y)*(PT.desc?-1:1)});
  const th=(k,n)=>`<th class="sortable ${PT.sort===k?"on":""}" onclick="ptSort('${k}')">${n}${PT.sort===k?(PT.desc?" ↓":" ↑"):""}</th>`;
  const mini=(r.series||[]).length>1?`<span class="ptmini" title="股池整体走势">${sparkSeries((r.series||[]).map(x=>x[1]??x.value??x))}</span>`:"";
  morph(el,`<div class="subh">${esc(pname(r.key,r.name))} 成分股 ${mini}<span class="small mute">${rows.length} 只</span></div>${r.pending?'<div class="small mute">行情还在拉，先显示存档里的成分股</div>':""}
   <div class="tw"><table class="pttable"><tr>${th("code","代码")}${th("name","名称")}${th("price","现价")}${th("change_pct","涨跌幅")}${th("since_entry_pct","入池以来")}</tr>${rows.map(x=>`<tr class="${x.code===PT.code?"sel":""}" data-code="${esc(x.code)}" onclick="ptOpen('${esc(x.code)}')"><td class="num">${esc(x.code)}</td><td>${esc(x.name)}</td><td class="num">${x.price==null?"—":f2(x.price)}</td><td class="num">${x.change_pct==null?"—":pct(x.change_pct)}</td><td class="num" title="${x.entry_date?"入池日 "+esc(x.entry_date):"没有存档"}">${x.since_entry_pct==null?"—":pct(x.since_entry_pct)}</td></tr>`).join("")}</table></div>
   <div class="small mute">${esc(r.note||"")}</div>`)}
function sparkSeries(vs){vs=(vs||[]).filter(v=>typeof v==="number");if(vs.length<2)return "";const mn=Math.min(...vs),mx=Math.max(...vs),w=90,h=24,d=mx-mn||1;return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><polyline fill="none" stroke="${vs[vs.length-1]>=vs[0]?"var(--up,#e5484d)":"var(--down,#30a46c)"}" stroke-width="1.5" points="${vs.map((v,i)=>`${(i/(vs.length-1)*w).toFixed(1)},${(h-2-(v-mn)/d*(h-4)).toFixed(1)}`).join(" ")}"/></svg>`}
function dash(v,f){return v==null||v===""||(typeof v==="number"&&isNaN(v))?"—":(f?f(v):esc(String(v)))}
async function ptOpen(code){PT.code=code;document.querySelectorAll(".pttable tr").forEach(tr=>tr.classList.toggle("sel",tr.dataset.code===code));const el=$("#ptright");el.innerHTML='<div class="empty">读取中…</div>';
  let d;try{d=await load(`/api/pool-stock/${encodeURIComponent(code)}?pool=${encodeURIComponent(PT.key)}`)}catch(e){el.innerHTML=`<div class="warn bad">${esc(e.message)}</div>`;return}PT.detail=d;drawPtRight();if(window.innerWidth<760)el.scrollIntoView({block:"start"})}
function ptChart(d){const W=520,H=180;if(PT.chart==="minute"){const pts=((d.minute||{}).points)||[];if(pts.length<2)return '<div class="empty small">没有分时数据</div>';
    const px=pts.map(p=>p.p??p.price),av=pts.map(p=>p.avg),all=[...px,...av].filter(v=>v!=null),mn=Math.min(...all),mx=Math.max(...all),dd=mx-mn||1,X=i=>(i/(pts.length-1)*W).toFixed(1),Y=v=>(H-6-(v-mn)/dd*(H-12)).toFixed(1);
    const line=(vs,c,dash)=>`<polyline fill="none" stroke="${c}" stroke-width="1.4" ${dash?'stroke-dasharray="4 3"':""} points="${vs.map((v,i)=>v==null?"":`${X(i)},${Y(v)}`).filter(Boolean).join(" ")}"/>`;
    return `<svg class="ptsvg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${line(px,"var(--accent,#3b82f6)")}${line(av,"#f5a524",true)}</svg><div class="small mute">实线：价格；虚线：均价线${d.minute&&d.minute.date?" · "+esc(d.minute.date):""}${d.minute&&d.minute.source?" · "+esc(d.minute.source):""}</div>`}
  const ks=((d.daily||{}).bars)||d.daily||[];if(!Array.isArray(ks)||ks.length<2)return '<div class="empty small">没有日K数据</div>';
  const lo=Math.min(...ks.map(k=>k.low)),hi=Math.max(...ks.map(k=>k.high)),dd=hi-lo||1,bw=W/ks.length,Y=v=>(H-6-(v-lo)/dd*(H-12)).toFixed(1),ed=d.entry_date||((d.entry||{}).date);
  return `<svg class="ptsvg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${ks.map((k,i)=>{const x=(i+.5)*bw,up=k.close>=k.open,c=up?"var(--up,#e5484d)":"var(--down,#30a46c)";return `${k.date===ed?`<line x1="${x}" x2="${x}" y1="0" y2="${H}" stroke="#f5a524" stroke-dasharray="3 3"/>`:""}<line x1="${x}" x2="${x}" y1="${Y(k.high)}" y2="${Y(k.low)}" stroke="${c}"/><rect x="${x-bw*.35}" width="${bw*.7}" y="${Math.min(Y(k.open),Y(k.close))}" height="${Math.max(1,Math.abs(Y(k.open)-Y(k.close)))}" fill="${c}"/>`}).join("")}</svg>
   <div class="small mute">日K ${esc(ks[0].date)} ~ ${esc(ks[ks.length-1].date)}${ed?` · 橙色虚线：入池日 ${esc(ed)}`:" · 没有入池日（没有存档）"}</div>`}
function drawPtRight(){const d=PT.detail,el=$("#ptright");if(!d||!el)return;const ind=Array.isArray(d.indicators)?d.indicators:Object.entries(d.indicators||{}).map(([key,value])=>({key,label:key,value})),fun=d.funnel||{},pos=d.position;
  const ed=d.entry_date||((d.entry||{}).date);
  morph(el,`<div class="subh">${esc(d.code)} ${esc(d.name||"")} <span class="small mute">${ed?"入池日 "+esc(ed):"入池日 —"}</span></div>
   <div class="seg" style="margin-bottom:6px"><button class="${PT.chart==="minute"?"on":""}" onclick="PT.chart='minute';drawPtRight()">分时</button><button class="${PT.chart==="daily"?"on":""}" onclick="PT.chart='daily';drawPtRight()">日K</button></div>${ptChart(d)}
   <div class="subh">各项指标</div><div class="indgrid">${ind.length?ind.map(({key:k,label,value:v})=>`<div><span class="small mute">${esc(label||k)}</span><b class="num">${dash(v,x=>typeof x==="number"?(+x).toFixed(Math.abs(x)>=100?1:2):esc(String(x)))}</b></div>`).join(""):'<span class="small mute">—</span>'}</div>
   <div class="subh">在买入策略里走到哪一层</div>${fun&&(fun.path||fun.reached||fun.status)?`<div class="small">${esc(fun.status||(fun.picked?"入选":fun.dropped_at?"停在「"+fun.dropped_at+"」":""))}${fun.strategy?" · 策略："+esc(typeof fun.strategy==="string"?fun.strategy:fun.strategy.name||""):""}${fun.reason?" · "+esc(fun.reason):""}</div><ol class="path">${(fun.path||[]).map(s=>`<li class="${s.passed?"ok":"no"}"><b>${esc(s.stage)}</b> ${s.passed?"✓":"✗"} <span class="small">${esc(s.note||"")}</span></li>`).join("")}</ol>
     ${fun.score_parts?`<div class="small">打分明细：${Object.entries(fun.score_parts).map(([k,v])=>`${esc(k)} ${typeof v==="number"?v.toFixed(3):esc(JSON.stringify(v))}`).join("，")}${fun.score!=null?" · 总分 "+(+fun.score).toFixed(3):""}</div>`:""}`:'<div class="small mute">—（最近一次选股里没有这只票）</div>'}
   ${pos?`<div class="subh">持仓</div><div class="kvs small" id="ptpos"><div>持有 ${dash(pos.quantity)} 股 · 成本 ${dash(pos.avg_cost,f2)} · 浮盈 ${dash(pos.pnl_pct,pct)} · 已持有 ${dash(pos.held_days)} 天</div>
     <div>离止盈还差 <b>${dash(pos.to_take_profit_pct,v=>(+v).toFixed(2)+"%")}</b>${pos.take_profit_price?`（${f2(pos.take_profit_price)}）`:""} · 离止损还差 <b>${dash(pos.to_stop_loss_pct,v=>(+v).toFixed(2)+"%")}</b>${pos.stop_loss_price?`（${f2(pos.stop_loss_price)}）`:""}</div><div>${esc(pos.due_text||"")}</div>
     <div class="mute">按「${esc((pos.sell_by||{}).pool||"—")}」的卖出策略「${esc((pos.sell_by||{}).strategy||"—")}」卖${(pos.sell_by||{}).bound?"（股池绑定）":"（正在生效的）"}${(pos.sell_by||{}).override&&Object.keys(pos.sell_by.override).length?"，含按股池覆盖":""}</div></div>`:""}`)}

/* 顶栏小字：当前的买入 / 卖出策略 */
function drawTopStrat(){const el=$("#tbstrat");if(!el)return;const a=STRAT.active||cget("/api/strategies-active");
  if(!a){load("/api/strategies-active").then(x=>{STRAT.active=x;drawTopStrat()}).catch(()=>{});return}
  el.innerHTML=`<a href="#buy/strategy" onclick="go('buy/strategy');return false" title="正在生效的买入策略">买：${esc((a.buy||{}).name||"—")}</a><a href="#sell/strategy" onclick="go('sell/strategy');return false" title="正在生效的卖出策略">卖：${esc((a.sell||{}).name||"—")}</a>`}
