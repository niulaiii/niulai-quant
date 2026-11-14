/* ---------------- 漏斗 / 持仓公用 ---------------- */
function picksTable(r){const ps=(r.picks||[]).filter(p=>typeof p==="object");if(!ps.length)return (r.picks||[]).length?`<div class="small">入选：${esc(r.picks.map(pcode).join(" "))}</div>`:'<div class="empty">本次没有入选</div>';
  return `<div class="tw"><table><tr><th>入选</th><th class="n">现价</th><th class="n">涨幅</th><th>来源</th></tr>${ps.map(p=>`<tr><td><b>${p.code}</b> ${esc(p.name)}</td><td class="n">${f2(p.fields.minute_last??p.fields.price)}</td><td class="n">${pct(p.fields.change_rate)}</td><td class="small mute">${esc((p.sources||[]).join(" + "))}</td></tr>`).join("")}</table></div>`}
function fillRun(el,id){const p="/api/runs/"+id,draw=r=>{const box=$(el);if(!box||!r)return;const today=(ST&&ST.now||"").slice(0,10);
   box.innerHTML=`<div class="small mute" style="margin-bottom:6px">${r.trading_date===today?"今天":"最近一次（数据日 "+esc(r.trading_date||"-")+"）"} · ${esc(r.started_at||"")} · ${esc(r.origin||"")}${r.errors&&r.errors.length?' · <span class="up">有错误</span>':""}</div>${funnelBars(r)}${picksTable(r)}`};
  draw(cget(p));load(p).then(draw).catch(e=>{const box=$(el);if(box&&!cget(p))box.innerHTML=`<div class="warn small">读取运行记录失败：${esc(e.message)}</div>`})}
/* 卖出因子在后台算（要拉行情），算完前每 1.5 秒问一次，最多 40 次 */
function pollPreview(n=0){const my=SEQ;const pv=cget("/api/sell/preview"),acc=cget("/api/account");
  const running=(pv&&pv.job&&pv.job.running)||(acc&&acc.preview&&acc.preview.running);if(!running||n>40)return;
  setTimeout(async()=>{if(my!==SEQ)return;try{const [a,p]=await Promise.all([load("/api/account"),load("/api/sell/preview")]);if(my!==SEQ)return;
    if(TAB==="account")drawAccount($("#main"),a,p);else{const el=$("#sellMatrix");if(el)morph(el,sellMatrix(p))}pollPreview(n+1)}catch(e){}},1500)}
async function runNow(trade,b){b.disabled=true;const t=b.textContent;b.textContent="运行中…";try{const d=$("#rdate");const r=await api("/api/run",{method:"POST",timeout:300000,body:{trade,date:d&&d.value.trim()||null}});
  toast(`完成：入选 ${r.picks.length} 只，委托 ${r.orders.length} 笔${r.errors.length?"，有错误":""}`);delete CACHE["/api/runs"];if(TAB==="buy"&&curSub()==="results")render();else go("buy/results")}finally{b.disabled=false;b.textContent=t}}
async function checkExits(b){b.disabled=true;try{const r=await api("/api/exits/check",{method:"POST",timeout:120000,body:{}});const acts=r.results.filter(x=>x.action);
  toast(`卖出检查完成：${r.results.length} 只持仓，${acts.length} 只有动作`);SX.fresh=true;SX.cid="";SX.day="";if(TAB==="sell")sellResults($("#main"));else go("sell/results")}finally{b.disabled=false}}

