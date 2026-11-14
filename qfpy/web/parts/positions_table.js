/* ---------------- 持仓表（首页和「持仓与收益」共用）：含保留按钮和卖出因子触发情况 ---------------- */
const PST={confirmed:"已确认",unconfirmed_buy:"买入待确认",selling:"卖出待确认",uncertain:"结果未知"};
function chkLine(c){return `<div class="chk ${c.passed?"y":"n"}">${c.passed?"✓":"✗"} ${esc(c.label||c.kind||"")}${c.value!=null&&c.value!==""?` <span class="num">${esc(typeof c.value==="number"?(+c.value).toFixed(2):c.value)}</span>`:""}${c.note?` <span class="mute">${esc(c.note)}</span>`:""}${c.error?` <span class="up">${esc(c.error)}</span>`:""}</div>`}
function layerState(e,l){const due=(e.due||[]).find(d=>d.layer===l.id);return l.triggered?["hit","触发"]:due?["due",`今天 ${due.at||""}`]:["idle","未触发"]}
function sellChips(e){if(!e||!e.layers)return `<span class="small mute">${esc((e&&e.reason)||"-")}</span>`;
  if(!e.layers.length)return `<span class="small mute">${esc(e.reason||e.note||"没有启用的卖出层")}</span>`;
  const chips=e.layers.map(l=>{const [c,t]=layerState(e,l);return `<span class="chip ${c}" title="${esc(l.name+"："+t+"\n"+(l.checks||[]).map(x=>(x.passed?"✓ ":"✗ ")+(x.label||x.kind)+(x.note?"  "+x.note:"")).join("\n"))}">${l.stop_loss&&!/止损/.test(l.name)?"止损·":""}${esc(l.name)}${c==="idle"?"":" · "+esc(t)}</span>`}).join("");
  let verdict;if(e.decision){verdict=`<span class="pill red">将卖出：${esc(e.decision_label||e.decision)}</span>`;
    if(e.hold)verdict+=e.hold_skippable?' <span class="pill amber">已点「保留」，这次不卖</span>':' <span class="pill red">保留挡不住这层</span>'}
  else if(e.due_at_exit_time)verdict='<span class="pill amber">今天到点会卖</span>';else verdict='<span class="small mute">继续持有</span>';
  return `<div class="chips">${chips}</div><div style="margin-top:3px">${verdict} <span class="small mute">${esc(e.reason||"")}</span>${e.note?` <span class="small down">${esc(e.note)}</span>`:""}</div>
   <details><summary>逐层明细</summary>${e.layers.map(l=>{const [c,t]=layerState(e,l);return `<div class="lyd ${c}"><b>${esc(l.name)}</b> <span class="small mute">${esc(l.mode==="any"?"任一满足":"全部满足")}${l.hold_skippable===false?" · 保留挡不住":""}</span> <span class="pill ${c==="hit"?"red":c==="due"?"amber":""}">${esc(t)}</span>${(l.checks||[]).map(chkLine).join("")}</div>`}).join("")}</details>`}
function holdCell(p){const h=p.hold;return h?`<span class="pill amber">已保留</span><div class="small mute">${esc(h.set_at||"")}</div><div class="small mute">${esc((typeof HOLD_MODE_LABEL!=="undefined"&&HOLD_MODE_LABEL[h.mode])||"到下一次检查")}</div><button class="btn small" onclick="setHold('${p.symbol}',false)">取消保留</button>`:
  `<button class="btn small primary" onclick="setHold('${p.symbol}',true)">保留</button>`}
/* 这只票按哪个池的哪套策略在卖（/api/positions-sell-by）；显示在「卖出因子」列最下面 */
let SELLBY=null,SELLSTRATS=null;
function loadSellStrats(){if(SELLSTRATS)return;SELLSTRATS=[];load("/api/strategies/sell").then(r=>{SELLSTRATS=r.items||[];document.querySelectorAll("[data-sellsel]").forEach(el=>{el.innerHTML=sellOpts(el.dataset.sellsel)})}).catch(()=>{})}
function sellOpts(sym){const x=(SELLBY||{})[sym]||{},cur=x.sell_strategy_id||"";return `<option value="">跟随股池 / 生效策略</option>`+(SELLSTRATS||[]).map(s=>`<option value="${esc(s.id)}"${s.id===cur?" selected":""}>${esc(s.name)}${s.active?"（生效中）":""}</option>`).join("")}
function sellByHtml(sym){const x=(SELLBY||{})[sym];return `${x?sellByTxt(x):'<span class="mute">卖出策略：</span>'}<select class="small" data-sellsel="${esc(sym)}" onchange="setPosSell('${esc(sym)}',this.value)" title="单独指定这只票用哪套卖出策略，优先于股池绑定" style="margin-top:2px;max-width:260px">${sellOpts(sym)}</select>`}
function posSellBy(p){const sym=p.symbol;loadSellStrats();if(!SELLBY){SELLBY={};load("/api/positions-sell-by").then(r=>{SELLBY=Object.fromEntries((r.items||[]).map(x=>[x.symbol,x]));document.querySelectorAll("[data-sellby]").forEach(el=>{el.innerHTML=sellByHtml(el.dataset.sellby)})}).catch(()=>{});}
  return `<div class="small sellby" data-sellby="${esc(sym)}">${sellByHtml(sym)}</div>`}
function sellByTxt(x){const s=x.sell_by||x;if(s.forced)return `持仓手动指定「${esc(s.strategy||"—")}」卖`;return `按 ${esc(poolName(s.pool)||s.pool||"—")} 的「${esc(s.strategy||"—")}」卖${s.bound?"":"（生效中）"}`}
async function setPosSell(sym,sid){try{const r=await api(`/api/positions/${encodeURIComponent(sym)}/sell_strategy`,{method:"POST",body:{strategy_id:sid}});toast(sid?`${sym} 已改为「${(r.strategy||{}).name||sid}」卖`:`${sym} 已恢复跟随股池 / 生效策略`);SELLBY=null;await posChanged()}catch(e){toast(e.message,true)}}
function posTable(a,pv,acct){const ps=a.positions||[];if(!ps.length)return '<div class="empty">当前没有持仓</div>';
  const ev=Object.fromEntries(((pv&&pv.results)||[]).map(r=>[r.symbol,r])),busy=(pv&&pv.job&&pv.job.running)||(a.preview&&a.preview.running);
  const rows=ps.map(p=>{const e=ev[p.symbol]||p.exit_preview||null,st=p.state||"confirmed",ok=st==="confirmed";
   return `<tr id="lp_${p.symbol}"><td><b>${p.symbol}</b> ${esc(p.name)}${ok?"":` <span class="pill red" title="${esc(p.state_note||"")}">${esc(PST[st]||st)}</span>`}<div class="small mute">${esc(p.first_buy_date||"")} 买入${e&&e.held_days!=null?` · 已持有 ${e.held_days} 天`:""}${acct&&(p.sources||[]).length?" · "+esc((p.sources||[]).map(x=>x==="券商持仓"?"自动获取时导入":x).join(" + ")):""}</div></td>
    <td class="n">${p.quantity}<div class="small mute">可卖 ${e&&e.sellable!=null?e.sellable:(p.sellable??"-")}</div></td>
    <td class="n">${f2(p.avg_cost)}<div class="small mute">现 ${f2((e&&e.price)??p.last_price)}</div></td><td class="n ${e&&e.pnl_pct>0?"up":e&&e.pnl_pct<0?"down":""}">${pct(e&&e.pnl_pct)}</td>
    <td class="sellcol">${e?sellChips(e):busy?'<span class="small mute">卖出因子计算中…</span>':'<span class="small mute">-</span>'}${posSellBy(p)}</td>
    <td class="nowrap">${holdCell(p)}${acct?`${ok?"":` <button class="btn small" onclick="confirmPos('${p.symbol}',${p.quantity},${p.avg_cost})">核对确认</button>`} <button class="btn small ghost" id="pe_${p.symbol}" onclick="posEdit('${p.symbol}')">修改</button> <button class="btn small ghost" id="pdl_${p.symbol}" onclick="manDel('${p.symbol}')">删除</button>`:""}</td></tr>`}).join("");
  return `<div class="tw"><table class="pos"><tr><th>股票</th><th class="n">数量</th><th class="n">成本</th><th class="n">浮盈</th><th>卖出因子（红=触发，黄=今天到点）</th><th>${acct?"操作":"保留"}</th></tr>${rows}</table></div>
   <div class="small mute" style="margin-top:6px">点「保留」时选挡多久：到下一次检查 / 到该交易日结束 / 到手工解除。${a.hold_can_skip_stop_loss?"当前设置：止损也能挡。":"止损除外，照卖。"}</div>
   ${a.preview_error?`<div class="warn small">卖出预览失败：${esc(a.preview_error)}</div>`:""}${pv&&pv.job&&pv.job.error?`<div class="warn small">卖出因子计算失败：${esc(pv.job.error)}</div>`:""}`}
/* 卖出结果矩阵：行=持仓，列=卖出层 */
function sellMatrix(pv){const res=(pv&&pv.results)||[],busy=pv&&pv.job&&pv.job.running;
  if(!res.length)return busy?'<div class="empty">卖出因子计算中…</div>':'<div class="empty">当前没有持仓。有持仓后，这里显示每只票在每一层的判断。</div>';
  const cols=[];res.forEach(r=>(r.layers||[]).forEach(l=>{if(!cols.some(c=>c.id===l.id))cols.push({id:l.id,name:l.name,stop:l.stop_loss})}));
  return `<div class="tw"><table class="mx"><tr><th>持仓</th><th class="n">浮盈</th><th class="n">持有</th>${cols.map(c=>`<th>${c.stop&&!/止损/.test(c.name)?"止损·":""}${esc(c.name)}</th>`).join("")}<th>结论</th></tr>
   ${res.map(r=>`<tr><td><b>${r.symbol}</b> ${esc(r.name||"")}${r.hold?' <span class="pill amber">保留</span>':""}</td><td class="n">${pct(r.pnl_pct)}</td><td class="n">${r.held_days??"-"} 天</td>
    ${cols.map(c=>{const l=(r.layers||[]).find(x=>x.id===c.id);if(!l)return '<td class="mute small">—</td>';const [k,t]=layerState(r,l);return `<td class="cell ${k}"><b>${esc(t)}</b>${(l.checks||[]).map(chkLine).join("")}</td>`}).join("")}
    <td>${r.decision?`<span class="pill red">卖出：${esc(r.decision_label||r.decision)}</span>${r.hold?(r.hold_skippable?'<div class="small warn-t">已点「保留」，这次不卖</div>':'<div class="small up">保留挡不住这层</div>'):""}`:r.due_at_exit_time?'<span class="pill amber">今天到点卖</span>':'<span class="small mute">持有</span>'}<div class="small mute">${esc(r.reason||"")}</div></td></tr>`).join("")}</table></div>
   <div class="small mute" style="margin-top:6px">${busy?"正在重算… ":""}${pv.job&&pv.job.finished_at?"计算于 "+esc(pv.job.finished_at)+"。":""}只是预览，不会下单；真正卖出在定时检查时刻执行，并受自动交易开关约束。</div>`}

