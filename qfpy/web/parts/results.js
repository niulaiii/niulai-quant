/* ---------------- 选股结果（第六轮：选股结果和分级股池合并成一页，按股池标签点选） ---------------- */
let FVIEW="layers",FRID=null,FPOOLS=new Set(),PNAMES=null;
const MODE_N={paper:"模拟盘",dry_run:"演练",live:"实盘"};
const ENTRY_N={new:"只送新入池",all:"全部成分股"};
/* 股池中文名：你起的别名 > 数据源给的中文名 > 代码本身 */
function pname(k,fb){const n=PNAMES&&PNAMES[k];return (n&&(n.alias||n.name))||fb||k}
/* 股池名（HTML）：有中文名就用中文名；拿不到中文名时显示代码，并提示去 设置 → 股池列表与别名 起名 */
const PN_HINT="还没有中文名：可以到 设置 → 股池列表与别名 起一个";
function pnoname(k){const n=pname(k);return !k||(n===k&&String(k).includes(":"))}
function pnameH(k){if(!pnoname(k))return esc(pname(k));return `<span class="pnraw" title="${PN_HINT}">${esc(k)}</span><a href="#config/pools" class="pnhint" title="${PN_HINT}" onclick="event.stopPropagation();go('config/pools');return false">起名</a>`}
async function loadPNames(){if(PNAMES)return PNAMES;PNAMES=cget("/api/pools/names");try{PNAMES=await load("/api/pools/names")}catch(e){PNAMES=PNAMES||{}}return PNAMES}
function fpOn(k){return !FPOOLS.size||FPOOLS.has(k)}
function fpToggle(k){if(k==="*")FPOOLS.clear();else if(FPOOLS.has(k))FPOOLS.delete(k);else FPOOLS.add(k);funnel($("#main"),FRID)}
function poolChips(keys){return `<div class="chips" id="fpools"><button class="chip ${FPOOLS.size?"":"on"}" onclick="fpToggle('*')">全部股池</button>${keys.map(k=>`<button class="chip ${FPOOLS.has(k)?"on":""}" data-pool="${esc(k)}" onclick="fpToggle('${esc(k)}')" ${pnoname(k)?`title="${PN_HINT}"`:""}>${esc(pname(k))}</button>`).join("")}${keys.some(pnoname)?`<a href="#config/pools" class="small mute pnhint" onclick="go('config/pools');return false" title="${PN_HINT}">有股池还没有中文名，去起名</a>`:""}</div>`}
/* 报错只显示一次：哪个池、什么问题、现在怎么处理的；原始英文收进「详情」 */
function poolProblems(r){const ps=(r.pools||[]).filter(p=>(p.error||p.problem||p.stale_at)&&fpOn(p.key));const seen=new Set();
  const ws=(r.warnings||[]).filter(w=>{if(seen.has(w))return false;seen.add(w);return !ps.some(p=>(p.error&&w.includes(p.error))||(p.detail&&w.includes(p.detail)))});
  if(!ps.length&&!ws.length&&!(r.errors||[]).length)return "";
  return `<div class="card probs" id="fprobs"><h3>需要注意</h3>${ps.map(p=>`<div class="prob"><div><b>${esc(pname(p.key,p.name))}</b>：${esc(zhErr(p.problem||p.error||""))}${p.stale_at?`（用的是 ${esc(p.stale_at)} 的缓存）`:""}</div>
     ${p.handling?`<div class="small">现在：${esc(p.handling)}</div>`:""}${p.hint?`<div class="small mute">怎么排查：${esc(p.hint)}</div>`:""}
     ${p.detail&&p.detail!==p.problem?`<details class="small"><summary>详情</summary><pre class="detail">${esc(p.detail)}</pre></details>`:""}</div>`).join("")}
   ${ws.map(w=>`<div class="prob small">${esc(zhErr(w))}${zhErr(w)!==w?`<details><summary>详情</summary><pre class="detail">${esc(w)}</pre></details>`:""}</div>`).join("")}
   ${(r.errors||[]).map(w=>`<div class="prob bad small">运行出错，这次选股没跑完。<details><summary>详情</summary><pre class="detail">${esc(w)}</pre></details></div>`).join("")}</div>`}
/* 常见英文报错翻成一句中文；认不出的原样显示 */
function zhErr(s){s=String(s||"");if(/[\u4e00-\u9fa5]/.test(s))return s;
  if(/UNEXPECTED_EOF|SSLError|SSL/i.test(s))return "连接在加密握手时被中途断开（多半是代理或加速器），没拉到数据";
  if(/timed? ?out|Timeout/i.test(s))return "请求超时，没拉到数据";
  if(/Connection(Error| refused| reset)|Max retries|NameResolution|getaddrinfo/i.test(s))return "连不上数据源，没拉到数据";
  if(/\b(401|403)\b/.test(s))return "数据源拒绝访问（口令或权限不对）";
  if(/\b(404)\b/.test(s))return "数据源上找不到这个地址";
  if(/\b5\d\d\b/.test(s))return "数据源服务器出错";
  return s}
/* 选股结果 · 按每只票看：每只票走到哪一层、为什么被淘汰、打分明细（可按股池筛） */
async function runStocks(rid,runs){const el=$("#runstocks")||$("#rundetail");const q=FPOOLS.size?"?pools="+encodeURIComponent([...FPOOLS].join(",")):"";let r;try{r=await load(`/api/runs/${encodeURIComponent(rid)}/stocks${q}`)}catch(e){el.innerHTML=`<div class="warn bad">${esc(e.message)}</div>`;return}
  const ss=(r.stocks||[]).filter(c=>!FPOOLS.size||!c.pool||FPOOLS.has(c.pool)||(c.sources||[]).some(s=>FPOOLS.has(s)));
  el.innerHTML=`<div class="card" id="bystock"><div class="row"><input id="stq" placeholder="搜代码或名字" oninput="stFilter(this.value)" style="width:180px"><span class="small mute">共 ${ss.length} 只，入选 ${ss.filter(x=>x.picked).length} 只</span></div>
   <div class="stlist">${ss.map(c=>`<details class="strow" data-q="${esc(c.code+" "+(c.name||""))}"><summary><b class="num">${esc(c.code)}</b> ${esc(c.name||"")} ${c.picked?'<span class="pill green">入选</span>':c.missing?'<span class="pill amber">数据缺失</span>':`<span class="pill">停在「${esc(c.dropped_at||c.reached||"")}」</span>`}
      <span class="small mute">${esc(c.picked?(c.score!=null?"打分 "+(+c.score).toFixed(3):""):(c.reason||""))}</span></summary>
     <div class="small mute">股池：${esc((c.pool?[c.pool]:(c.sources||[])).map(k=>pname(k)).join("、"))}${c.status?" · "+esc(c.status):""}</div>
     <ol class="path">${(c.path||[]).map(s=>`<li class="${s.passed?"ok":"no"}"><b>${esc(s.stage)}</b> ${s.passed?"✓":"✗"} <span class="small">${esc(s.note||"")}</span>${(s.checks||[]).length?`<div class="small">${s.checks.map(k=>`<span class="${k.passed?"up-ok":k.passed===false?"warn-t":"mute"}">${esc(k.label)}${k.passed==null?"（不适用 / 缺数据）":""}：${esc(k.note||"")}</span>`).join("<br>")}</div>`:""}</li>`).join("")}</ol>
     ${c.score_parts?`<div class="small">打分明细：${Object.entries(c.score_parts).map(([k,v])=>`${esc(k)} ${typeof v==="number"?v.toFixed(3):esc(JSON.stringify(v))}`).join("，")}</div>`:""}</details>`).join("")||'<div class="empty">这次没有候选</div>'}</div></div>`}
function stFilter(q){q=q.trim();document.querySelectorAll(".strow").forEach(d=>{d.style.display=!q||d.dataset.q.includes(q)?"":"none"})}
/* 选股结果（第六轮小修）：默认「全部结果」——所有运行记录合成一张表、默认全部股池，不用先选时间。
   按日期、按某一次运行看是右上角「历史记录」里的可选筛选；选了某一次运行才显示那一次的漏斗。
   原来的「分级跟踪」块去掉了，级别变成表里的一列，跨天的级别变化和手动升降级在点开一只票的详情里。 */
let FDATE="",FTIER=false,FLIMIT=100,FOPEN=null,FROWS=[];
function runLabel(x){x=x||{};const o=String(x.origin||"");return `${x.trading_date||""} ${(x.started_at||"").slice(11,16)} · ${/定时|自动|计划/.test(o)?"定时":/手动|网页/.test(o)?"手动":o}${o&&!/^(定时|手动)$/.test(o)?`（${o}）`:""}`}
function modeTxt(r){r=r||{};const m=r.mode_label||MODE_N[r.mode]||"未记录";const cur=ST&&ST.mode;return cur&&r.mode&&MODE_N[r.mode]&&r.mode!==cur?`记录时是${m}（现在是${MODE_N[cur]||cur}）`:m}
async function funnel(m,id){await loadPNames();if(id!==undefined)FRID=id||null;
  let runs=cget("/api/runs")||[];try{runs=await load("/api/runs")}catch(e){}
  if(FRID&&!runs.some(x=>x.id===FRID))FRID=null;
  const days=[...new Set(runs.map(x=>x.trading_date).filter(Boolean))];
  const keys=[...new Set([...Object.keys(PNAMES||{}),...runs.flatMap(x=>(x.pools||[]).map(p=>p.key))])];
  const hist=`<details class="fhist" id="fhist" ${FRID||FDATE?"open":""}><summary class="small">历史记录${FRID||FDATE?"（已筛选）":""}</summary><div class="fhbody small">
     <label class="inl">按日期 <select id="fdate" onchange="FDATE=this.value;FRID=null;FLIMIT=100;funnel($('#main'),null)"><option value="">全部日期</option>${days.map(d=>`<option ${d===FDATE?"selected":""}>${d}</option>`).join("")}</select></label>
     <label class="inl">某一次运行 <select id="frun" onchange="FVIEW='layers';funnel($('#main'),this.value||null)"><option value="">不选（看全部结果）</option>${runs.filter(x=>!FDATE||x.trading_date===FDATE).map(x=>`<option value="${esc(x.id)}" ${x.id===FRID?"selected":""}>${esc(runLabel(x))} · 入选 ${esc((x.picks||[]).map(pcode).join(",")||"无")}</option>`).join("")}</select></label></div></details>`;
  const tierSw=`<label class="inl small"><input type="checkbox" id="ftier" ${FTIER?"checked":""} onchange="FTIER=this.checked;FLIMIT=100;funnel($('#main'))"> 只看重点及以上</label>`;
  const scope=FRID?`<div class="fscope small" id="fscope">正在看一次运行：<b>${esc(runLabel(runs.find(x=>x.id===FRID)))}</b> · <a href="#" onclick="funnel($('#main'),null);return false">回到全部结果</a></div>`:"";
  const manual=`<details class="card fmanual" id="fmanual"><summary><b>手动运行</b> <span class="small mute">平时不用点，到点会自动跑</span></summary><div class="row" style="margin-top:8px"><label class="inl">数据日期 <input id="rdate" placeholder="留空 = 最新交易日" style="width:160px"></label>
    <button class="btn" onclick="runNow(false,this)">只选股（不交易）</button><button class="btn primary" onclick="runNow(true,this)">选股 + 交易</button></div>
    <div class="small mute">「选股 + 交易」同样受自动交易开关约束，开关关着时只记信号。买入的因子在 <a href="#buy/strategy" onclick="go('buy/strategy');return false">买入策略</a> 里改。</div></details>`;
  m.innerHTML=`<div class="card ftop"><div class="ftopbar"><div id="fchips">${poolChips(keys)}</div><div class="fside">${tierSw}${hist}</div></div>${scope}</div><div id="rundetail"><div class="card"><div class="empty">读取中…</div></div></div>${manual}`;
  if(!runs.length){$("#rundetail").innerHTML='<div class="card"><div class="empty">还没有运行记录。到点会自动跑，也可以在下面「手动运行」里跑一次。</div></div>';return}
  if(FRID){const p="/api/runs/"+FRID;const rid=FRID;
    const view=`<div class="row" style="margin-bottom:8px"><span class="seg" id="fview"><button class="${FVIEW==="layers"?"on":""}" onclick="FVIEW='layers';funnel($('#main'))">按股池看</button><button class="${FVIEW==="stocks"?"on":""}" onclick="FVIEW='stocks';funnel($('#main'))">按每只票看</button></span></div>`;
    const draw=r=>{const el=$("#rundetail");if(!r||!el||FRID!==rid)return;if(FVIEW==="stocks"){el.innerHTML=view+'<div id="runstocks"></div>';runStocks(rid,runs);return}el.innerHTML=view+runDetail(r)};
    draw(cget(p));load(p).then(draw).catch(e=>toast(e.message,true));return}
  allResults()}
async function allResults(){const el=$("#rundetail");if(!el)return;const q=new URLSearchParams({limit:FLIMIT});if(FPOOLS.size)q.set("pools",[...FPOOLS].join(","));if(FDATE)q.set("date",FDATE);if(FTIER)q.set("tier","focus");
  let r;try{r=await load("/api/results?"+q)}catch(e){el.innerHTML=`<div class="card warn bad">${esc(zhErr(e.message))}</div>`;return}if(!$("#rundetail")||FRID)return;
  const sm=r.summary||[],rows=r.rows||[];FROWS=rows;
  const sumH=`<div class="card" id="fsum"><h3>汇总 <span class="small mute">${FDATE?esc(FDATE)+" · ":""}${(r.runs||[]).length} 次运行 · ${FPOOLS.size?"选中的股池":"全部股池"}${FTIER?" · 只看重点及以上":""}</span>${r.latest_run?`<span class="r"><a href="#" class="small" id="flatest" onclick="FVIEW='layers';funnel($('#main'),'${esc(r.latest_run)}');return false">看最近一次的漏斗</a></span>`:""}</h3>
    ${sm.length?`<div class="tw"><table class="poolsum rtab" id="fsumtbl"><colgroup><col class="c-pool"><col class="c-n"><col class="c-n"><col class="c-n"><col class="c-n"></colgroup><thead><tr><th>股池</th><th class="n">运行次数</th><th class="n">新入池（合计）</th><th class="n">进入策略（只次）</th><th class="n">入选（只次）</th></tr></thead><tbody>${sm.map(x=>`<tr class="${FPOOLS.has(x.pool)?"sel":""}" data-pool="${esc(x.pool)}" style="cursor:pointer" onclick="fpToggle('${esc(x.pool)}')"><td><b>${pnameH(x.pool)}</b></td><td class="n">${x.runs}</td><td class="n">${x.new==null?'<span class="mute" title="这些运行记录是第六轮之前的旧版写的，没有记新入池">—</span>':x.new}${x.new!=null&&x.new_missing?`<span class="small mute" title="其中 ${x.new_missing} 次是旧记录，没有新入池数据，没算进来">（另 ${x.new_missing} 次旧记录无此项）</span>`:""}</td><td class="n">${x.entered}</td><td class="n">${x.picked}</td></tr>`).join("")}</tbody></table></div>`:'<div class="small mute">没有符合条件的结果</div>'}
    <div class="small mute">「只次」：同一只票在几次运行里出现就算几次。${sm.some(x=>x.new==null||x.new_missing)?"新入池显示「—」的，是第六轮之前的旧记录，当时没有记新入池这一项，不是 0。":""}想看某一次的漏斗，点右上角，或在「历史记录」里选一次运行。</div></div>`;
  const tier=x=>x.tier?`<span class="pill ${x.tier==="buy"?"red":x.tier==="focus"?"amber":""}">${esc(x.tier_name||x.tier)}</span>`:'<span class="mute">—</span>';
  const res=x=>x.picked?'<span class="pill green">入选</span>':x.dropped_at?`<span class="small">停在「${esc(x.dropped_at)}」</span>`:'<span class="small mute">没入选</span>';
  const tbl=`<div class="card" id="fall"><h3>全部结果 <span class="small mute">共 ${r.total} 行，新的在上面；点一行看这只票的详情</span></h3>${rows.length?`<div class="tw"><table class="fall rtab" id="falltbl"><colgroup><col class="c-date"><col class="c-time"><col class="c-orig"><col class="c-pool"><col class="c-stock"><col class="c-layer"><col class="c-res"><col class="c-tier"></colgroup><thead><tr><th>日期</th><th>时间</th><th>方式</th><th>股池</th><th>股票</th><th>走到第几层</th><th>结果</th><th>级别</th></tr></thead><tbody>${rows.map((x,i)=>`<tr class="fres" data-code="${esc(x.code)}" data-i="${i}" onclick="fOpen(${i})" style="cursor:pointer"><td class="num" data-l="日期">${esc(x.trading_date)}</td><td class="num" data-l="时间">${esc(x.time||"—")}</td><td class="small" data-l="方式" title="${esc(x.origin||"")}">${esc(fOrigin(x.origin))}</td><td class="small" data-l="股池">${(x.pools||[]).map(k=>pnameH(k)).join("、")||"—"}</td><td data-l="股票"><b class="num">${esc(x.code)}</b> ${esc(x.name||"")}</td><td class="small" data-l="走到第几层">第 ${x.layer}/${x.layers} 层${x.reached?` · ${esc(x.reached)}`:""}</td><td data-l="结果">${res(x)}</td><td data-l="级别">${tier(x)}</td></tr>`).join("")}</tbody></table></div>${r.more?`<div class="row" style="justify-content:center;margin-top:8px"><button class="btn small" id="fmore" onclick="FLIMIT+=100;allResults()">加载更多</button></div>`:""}`:'<div class="empty">没有符合条件的结果</div>'}</div>`;
  $("#rundetail").innerHTML=sumH+tbl;FOPEN=null}
function fOrigin(o){o=String(o||"");return /定时|自动|计划/.test(o)?"定时":/手动|网页/.test(o)?"手动":(o||"—")}
async function fOpen(i){const x=FROWS[i];if(!x)return;document.querySelectorAll(".fdet").forEach(e=>e.remove());const rowsEl=document.querySelectorAll("#falltbl tr.fres");
  if(FOPEN===i){FOPEN=null;rowsEl.forEach(t=>t.classList.remove("sel"));return}FOPEN=i;rowsEl.forEach(t=>t.classList.toggle("sel",+t.dataset.i===i));
  const tr=document.querySelector(`#falltbl tr.fres[data-i="${i}"]`);if(!tr)return;const d=document.createElement("tr");d.className="fdet";d.innerHTML='<td colspan="8"><div class="empty small">读取中…</div></td>';tr.after(d);
  let h;try{h=await load("/api/results/stock/"+encodeURIComponent(x.code))}catch(e){d.firstChild.innerHTML=`<div class="warn bad small">${esc(zhErr(e.message))}</div>`;return}
  if(d.isConnected)d.firstChild.innerHTML=stockDetail(h,x)}
function stockDetail(h,x){const names=h.tier_names||{},cur=h.current,keys=["candidate","focus","buy"];
  const tl=(h.tiers||[]).filter(t=>t.change).map(t=>`<li><span class="num">${esc(t.date)}</span> ${esc(t.change)}${(t.log||[]).length?` <span class="mute">${esc(t.log.map(l=>l.note||l.reason||"").filter(Boolean).join("；"))}</span>`:""}</li>`).join("");
  const mv=cur?`<div class="row small" style="margin-top:6px">现在（${esc(cur.date)}）是 <b>${esc(names[cur.tier]||cur.tier)}</b>：${keys.filter(k=>k!==cur.tier).map(k=>`<button class="btn small ghost" onclick="event.stopPropagation();moveTier('${esc(cur.date)}','${esc(h.code)}','${k}')">调到${esc(names[k]||k)}</button>`).join("")}<span class="mute">手动升级要逐级，降级可以跨级；只用于跟踪，不触发下单</span></div>`:"";
  const cr=(h.runs||[]).find(r=>r.run_id===x.run_id&&(!x.pool||(r.pools||[]).includes(x.pool)))||{};
  const path=(cr.path||[]).map(s=>`<li class="${s.passed?"ok":"no"}">${s.passed?"✓":"✗"} ${esc(s.stage||"")} <span class="mute">${esc(s.note||"")}</span></li>`).join("");
  const runs=(h.runs||[]).slice(0,10).map(r=>`<li><span class="num">${esc(r.trading_date||"")} ${esc(r.time||"")}</span> ${esc(r.origin||"")} · ${esc((r.pools||[]).map(k=>pname(k)).join("、"))} · ${r.picked?"入选":esc(r.status||"")}</li>`).join("");
  return `<div class="fdetail" onclick="event.stopPropagation()"><div><b class="num">${esc(h.code)}</b> ${esc(h.name||x.name||"")} <span class="small mute">${esc(x.trading_date)} ${esc(x.time||"")} 这一次的路径：</span></div>${path?`<ol class="path small">${path}</ol>`:""}
   <div class="g2"><div><div class="subh">级别变化</div>${tl?`<ul class="small" id="ftierlog">${tl}</ul>`:'<div class="small mute">没有分级记录</div>'}${mv}</div>
   <div><div class="subh">在哪些运行里出现过（最近 10 次）</div>${runs?`<ul class="small">${runs}</ul>`:'<div class="small mute">无</div>'}</div></div></div>`}
function stageTable(list){const row=c=>`<tr><td class="num">${c.code}</td><td>${esc(c.name)}</td><td class="n">${f2(c.fields.minute_last??c.fields.price)}</td><td class="n">${pct(c.fields.change_rate)}</td><td class="n">${c.value==null?"":(+c.value).toFixed(3)}</td><td class="small">${esc(c.note||"")}${c.returned?' <span class="pill">移出后又回来</span>':""}</td></tr>`;
  return list.length?`<div class="tw"><table><tr><th>代码</th><th>名称</th><th class="n">现价</th><th class="n">涨幅</th><th class="n">值</th><th>说明</th></tr>${list.map(row).join("")}</table></div>`:'<div class="mute small">无</div>'}
function stageBlock(s){return `<div class="stage"><div><span class="pill">${s.index}</span> <b>${esc(s.label)}</b>${s.by_pool?` <span class="small mute">（${Object.entries(s.by_pool).map(([k,n])=>esc(pname(k))+" "+n+" 只").join("、")}）</span>`:""}${s.mode_label?` <span class="pill blue">${esc(s.mode_label)}</span>`:""} · 输入 <span class="cnt">${s.input}</span> → ${s.type==="pool"?"送进策略":"保留"} <span class="cnt">${s.kept.length}</span>${s.dropped.length?` · ${s.type==="pool"?"没送进":"淘汰"} ${s.dropped.length}`:""} ${s.needs&&s.needs.length?`<span class="pill amber">${s.needs.map(x=>x==="minute"?"分时":x==="daily"?"日K":x).join("+")}</span>`:""} ${s.error?`<span class="pill red">${esc(zhErr(s.error))}</span>`:""}</div>
     ${s.dropped.length?`<details><summary>${s.type==="pool"?"没送进策略的":"淘汰"} ${s.dropped.length} 只及原因</summary>${stageTable(s.dropped)}</details>`:""}
     ${s.kept.length?`<details ${s.type==="select"?"open":""}><summary>${s.type==="pool"?"送进策略":s.type==="select"?"入选":"保留"} ${s.kept.length} 只</summary>${stageTable(s.kept)}</details>`:""}</div>`}
function runDetail(r){const prs=(r.pool_runs||[]);const on=prs.filter(x=>fpOn(x.pool));
  const legacy=!prs.length;   /* 第六轮以前的记录没有按股池的汇总，按原来的整体漏斗显示 */
  const sum=prs.length?`<div class="tw"><table class="poolsum" id="fpoolsum"><tr><th>股池</th><th>买入策略</th><th>送进策略的规则</th><th class="n">成分股</th><th class="n">新入池</th><th class="n">进入策略</th><th class="n">通过因子</th><th>入选</th></tr>${prs.map(x=>`<tr class="${fpOn(x.pool)&&FPOOLS.size?"sel":""}" data-pool="${esc(x.pool)}" onclick="fpToggle('${esc(x.pool)}')" style="cursor:pointer"><td><b>${esc(pname(x.pool,x.name))}</b>${x.stale_at?' <span class="pill amber">缓存</span>':""}</td><td>${esc(x.strategy||"")}${x.active?' <span class="small mute">（正在生效）</span>':""}</td><td class="small">${esc(ENTRY_N[x.mode]||x.mode||"")}${x.mode==="new"?(x.scope==="day"?"，每天重新算":"，跨日连续算"):""}</td><td class="n">${x.members}</td><td class="n">${x.new}</td><td class="n">${x.input}</td><td class="n">${x.kept}</td><td class="num">${(x.picks||[]).length?esc(x.picks.join(" ")):'<span class="mute">无</span>'}</td></tr>`).join("")}</table></div>`:"";
  const byPool=on.map(x=>{const st=(x.stages||[]).map(i=>r.stages[i]).filter(Boolean);const picks=(r.picks||[]).filter(p=>(x.picks||[]).includes(pcode(p)));
    return `<div class="card poolrun" data-pool="${esc(x.pool)}"><h3>${esc(pname(x.pool,x.name))} <span class="small mute">买入策略：${esc(x.strategy||"")} · 成分股 ${x.members} 只，新入池 ${x.new} 只</span></h3>${funnelBars({stages:st,picks})}
     <details class="small"><summary>逐层明细</summary>${st.map(stageBlock).join("")}</details></div>`}).join("");
  const old=legacy?`<div class="card"><h3>漏斗：${r.stages.map(s=>s.input).join(" → ")} → ${r.picks.length}</h3>${funnelBars(r)}<details class="small"><summary>逐层明细</summary>${r.stages.map(stageBlock).join("")}</details></div>`:"";
  return `<div class="card"><div class="small mute">数据日期 ${r.trading_date||"-"} · ${esc(r.origin)} · ${esc(modeTxt(r))} · 自动交易${r.auto_trade?"开":"关"} · ${esc(r.started_at)} ~ ${esc(r.finished_at||"")}</div>${prs.length?`<div class="small fmerged" id="fmerged"><b>进入策略（全部股池）</b>：共 ${prs.reduce((a,x)=>a+(x.input||0),0)} 只 = ${prs.map(x=>`${esc(pname(x.pool,x.name))} ${x.input} 只（新入池 ${x.new}）`).join(" + ")}</div>`:""}${sum}</div>
   ${poolProblems(r)}${byPool}${old}
   <div class="card"><h3>本次委托</h3>${orderTable(r.orders)}${r.exits&&r.exits.length?`<h3 style="margin-top:12px">本次卖出检查</h3>${exitTable(r.exits)}`:""}</div>`}
