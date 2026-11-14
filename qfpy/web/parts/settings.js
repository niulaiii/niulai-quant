/* ---------- 设置：数据源与行情（带「测试连接」） ---------- */
function checksHtml(r){return (r.checks||[]).map(c=>`<span class="pill ${c.ok?"green":"red"}" title="${esc(c.detail||"")}">${c.ok?"✓":"✗"} ${esc(c.check||"")}</span> <span class="small ${c.ok?"mute":"up"}">${esc(c.detail||"")}</span>`).join("<br>")+(r.note?`<div class="small mute">${esc(r.note)}</div>`:"")}
/* 数据源（第六轮）：股池数据源、行情数据源分成两块，各写回 [pool_sources.*] / [market_sources.*]；内存里的 sources 是两类合起来的总表 */
let SRCST=null;
function srcRole(n){return W.pool_sources&&W.pool_sources[n]?"pool_sources":"market_sources"}
function syncSources(){W.pool_sources=W.pool_sources||{};W.market_sources=W.market_sources||{};W.sources={...W.market_sources,...W.pool_sources}}
async function testSrc(n,b){const ae=document.activeElement;if(ae&&ae.blur)ae.blur();b.disabled=true;const t=b.textContent;b.textContent="测试中…";const el=$("#ts_"+n);syncSources();
  try{const r=await api("/api/test/source",{method:"POST",timeout:40000,quiet:true,body:{name:n,config:JSON.parse(JSON.stringify(W.sources[n]))}});if(el)el.innerHTML=checksHtml(r);toast(`${n}：${r.ok?"连接正常":"有检查没通过"}（用的是表单里当前的值，未保存）`,!r.ok);load("/api/sources/status").then(j=>{SRCST=j;srcStatusFill()}).catch(()=>{})}
  catch(e){if(el)el.innerHTML=`<span class="pill red">✗</span> <span class="small up">${esc(zhErr(e.message))}</span><details class="small"><summary>详情</summary>${esc(e.message)}</details>`}finally{b.disabled=false;b.textContent=t}}
function proxySel(p){const v=get(p)??"auto",mode=v==="auto"||v===""?"auto":v==="none"?"none":"custom";
  return `<select class="proxysel" onchange="if(this.value==='custom'){set('${p}','http://127.0.0.1:7890');drawCfg()}else{set('${p}',this.value);drawCfg()}"><option value="auto" ${mode==="auto"?"selected":""}>跟随系统（失败再直连）</option><option value="none" ${mode==="none"?"selected":""}>直连（不走代理）</option><option value="custom" ${mode==="custom"?"selected":""}>自定义代理网址</option></select>`+
   (mode==="custom"?` <input value="${esc(v)}" style="width:240px" placeholder="http://127.0.0.1:7890" onchange="set('${p}',this.value.trim()||'auto')">`:"")}
function srcField(n,s,f){const p=`${srcRole(n)}.${n}.${f.name}`,secret=/key|token|password|secret/i.test(f.name);
  if(f.name==="proxy")return proxySel(p);
  if(f.name==="codes"||Array.isArray(s[f.name]))return inp(p,"list",f.hint);if(typeof s[f.name]==="boolean")return inp(p,"bool",f.hint);
  if(typeof s[f.name]==="number")return inp(p,"number",f.hint);return inp(p,"text",(f.hint||"")+(secret?"（已遮蔽，不改就保持原值）":""))}
function srcStatusHtml(n){const all=SRCST?[...(SRCST.pool||[]),...(SRCST.market||[])]:[];const x=all.find(r=>r.name===n);if(!SRCST)return '<span class="small mute">正在读取状态…</span>';
  if(!x)return '<span class="small mute">还没保存，保存后才有状态</span>';const h=x.health||{};
  const use=[...(x.pools||[]).map(k=>"股池 "+pname(k,k)),...(x.market_use||[]).map(u=>"行情："+u)];
  const cls=x.state==="ok"?"green":x.state==="error"?"red":x.state==="off"?"":"amber";
  return `<div class="srcst"><span class="pill ${cls}">${esc(x.state_cn||x.state)}</span>${x.shared?' <span class="pill">股池和行情共用</span>':""}
   <div class="small"><span class="mute">用在：</span>${use.length?use.map(esc).join("、"):'<span class="mute">没被用到</span>'}</div>
   <div class="small"><span class="mute">最近一次成功：</span>${h.ok_at?esc(h.ok_at)+(h.ok_what?`（${esc(h.ok_what)}）`:""):"还没有"}</div>
   ${h.err_at?`<div class="small up"><span class="mute">最近的报错：</span>${esc(h.err_at)}${h.err_what?` ${esc(h.err_what)}`:""}：${esc(zhErr(h.error))}<details><summary>详情</summary><code>${esc(h.error||"")}</code></details></div>`:""}</div>`}
function srcStatusFill(){document.querySelectorAll("[data-srcst]").forEach(el=>el.innerHTML=srcStatusHtml(el.dataset.srcst))}
function srcBlock(role){const types=META.source_types,m=W[role]||{};
  return Object.entries(m).map(([n,s])=>{const t=types.find(x=>x.type===s.type)||{fields:[]};return `<div class="factor" id="src_${n}"><div class="row"><b>${esc(n)}</b> <span class="pill">${esc(t.title||s.type)}</span> <span class="small mute">${esc(t.description||"")}</span><span style="flex:1"></span>
     <button class="btn small" onclick="testSrc('${n}',this)">测试连接</button><button class="btn small ghost" onclick="delSrc('${n}')">删除</button></div>
     <div data-srcst="${esc(n)}">${srcStatusHtml(n)}</div>
     ${kv([["启用",inp(`${role}.${n}.enabled`,"bool")],...t.fields.map(f=>[f.name==="proxy"?lab("sources.proxy","代理"):(f.label||f.name),srcField(n,s,f)])])}<div id="ts_${n}" class="small"></div></div>`}).join("")||'<div class="empty small">还没有</div>'}
function cfgSources(b){const types=META.source_types;syncSources();
  if(!SRCST)load("/api/sources/status").then(j=>{SRCST=j;srcStatusFill()}).catch(e=>{SRCST={pool:[],market:[]};srcStatusFill()});
  const add=role=>`<div class="row" style="margin-top:8px"><input id="nsname_${role}" placeholder="新数据源名称（英文）" style="width:180px"><select id="nstype_${role}">${types.map(t=>`<option value="${t.type}">${esc(t.title||t.type)}</option>`).join("")}</select><button class="btn" onclick="addSrc('${role}')">添加${role==="pool_sources"?"股池":"行情"}数据源</button></div>`;
  const names=Object.keys(W.sources);
  b.innerHTML=`<div class="card" id="poolsrc"><h3>股池数据源</h3><div class="small mute">提供股池名单（选股从这里来）。写在配置文件的 [pool_sources.名字] 里。gushi 既出股池也出分时，放在这里，行情那边可以直接选它共用。「测试连接」用表单里当前填的值去试，不用先保存。</div>${srcBlock("pool_sources")}${add("pool_sources")}</div>
   <div class="card" id="marketsrc"><h3>行情数据源</h3><div class="small mute">提供实时行情、指数、市场情绪、分时和日K。写在配置文件的 [market_sources.名字] 里。第三方行情按 docs/GUIDE.md 里的「标准数据接口」实现后，用「标准接口」类型填网址和 token 就能接入。</div>${srcBlock("market_sources")}${add("market_sources")}
   <div class="subh">行情用哪些数据源（按顺序，前一个失败自动用下一个）</div>${kv([["实时行情 / 指数 / 市场情绪",inp("market.quote_sources","list","首页和股池走势用")],["分时",inp("market.minute_sources","list")],["日K",inp("market.daily_sources","list")],["日K回看多少个自然日",inp("market.daily_lookback_days")]])}
   <div class="small mute">填数据源名称，逗号分隔，股池数据源和行情数据源都能填。现有：${names.map(esc).join("、")}</div></div>`}
function addSrc(role){role=role||"market_sources";const n=$("#nsname_"+role).value.trim(),t=$("#nstype_"+role).value;syncSources();if(!/^[A-Za-z_][\w-]*$/.test(n)){toast("名称只能用英文、数字、下划线",true);return}if(W.sources[n]){toast("已有同名数据源",true);return}
  const tp=META.source_types.find(x=>x.type===t)||{fields:[]};const o={type:t,enabled:true};for(const f of tp.fields)if(f.default!==undefined)o[f.name]=f.default;W[role][n]=o;syncSources();drawCfg()}
function delSrc(n){if(!confirm(`删除数据源 ${n}？（保存后生效；用到它的股池和行情顺序也要一起改掉）`))return;delete W.pool_sources[n];delete W.market_sources[n];syncSources();drawCfg()}
function cfgPools(b){const pools=W.pool.pools||[];if(!PNAMES)loadPNames().then(()=>{if(TAB==="config"&&CFGTAB==="pools"&&$("#aliascard"))cfgPools(b)});
  b.innerHTML=`<div class="card"><h3>用哪些股池选股</h3><div class="small mute">勾上的股池各自分开跑自己绑定的买入策略；一只票同时在几个股池里时，按股池顺序归第一个池，命中几个池会记下来。牛来时段里每个时段用哪些股池，在「牛来时段与补录」里选。</div>
   ${META.sources.map(s=>`<div style="margin:8px 0"><b>${esc(s.name)}</b> <span class="small mute">${esc(s.title)}</span><div class="row">${s.pools.map(p=>{const k=s.name+":"+p.key;return `<label><input type="checkbox" ${pools.includes(k)?"checked":""} onchange="togglePool('${k}',this.checked)"> ${esc(p.name)} <span class="small mute">${esc(p.key)}</span></label>`}).join("")||'<span class="small mute">无股池（或还在后台读取）</span>'}</div></div>`).join("")}
   <div class="small">当前：${pools.map(k=>esc(pname(k,k))).join("，")||"（空）"}</div><div class="small mute">每个股池用哪套买入 / 卖出策略、先后顺序、新入池方式，在 <a href="#buy/pools" onclick="go('buy/pools');return false">买入 → 股池分配</a> 里点选。</div></div>
   <div class="card" id="aliascard"><h3>股池别名</h3><div class="small mute">给股池起个中文名，页面上所有显示股池的地方都用它；留空就用数据源给的中文名，没有就显示代码。改完点下面的「保存并生效」。</div>
   ${kv(pools.map(k=>{const al=(W.pool.aliases||{})[k]||"",dn=PNAMES&&PNAMES[k]&&PNAMES[k].name;return [`<div class="lab"><b>${esc(dn&&dn!==k?dn:k)}</b> <span class="keyname">${esc(k)}</span></div>`,`<input class="aliasin" value="${esc(al)}" placeholder="${esc(dn||"不起别名")}" maxlength="30" onchange="setAlias('${esc(k)}',this.value)">`]}))||'<div class="empty small">先在上面勾选股池</div>'}</div>
   <div class="card"><h3>自选股 CSV</h3><div id="wl" class="small mute">加载中…</div></div>`;loadWl()}
function setAlias(k,v){W.pool.aliases=W.pool.aliases||{};v=v.trim();if(v)W.pool.aliases[k]=v;else delete W.pool.aliases[k];PNAMES=null}
function allPoolKeys(){const s=new Set(META.sources.flatMap(s=>s.pools.map(p=>s.name+":"+p.key)));for(const r of (W.schedule.runs||[]))for(const p of (r.pools||[]))s.add(p);for(const p of (W.pool.pools||[]))s.add(p);return [...s]}
function cfgSchedule(b){const all=allPoolKeys(),runs=W.schedule.runs||[];
  b.innerHTML=`<div class="card"><h3>牛来策略时段</h3>${kv([["启用定时",inp("schedule.enabled","bool","serve 运行期间生效；是否真的下单由自动交易开关决定")],["股池没数据时重试间隔（秒）",inp("schedule.retry_secs")]])}
  <div class="small mute">时段内跑完整链路（股池 → 买入漏斗 → 下单或只记信号）；股池还没出数据就按间隔重试，到时段结束还没数据就推送提醒。</div>
  ${runs.map((r,i)=>`<div class="factor"><div class="row"><b>名称</b> <input value="${esc(r.name||"")}" style="width:140px" onchange="W.schedule.runs[${i}].name=this.value"><b>开始</b> <input value="${esc(r.start||r.time||"")}" style="width:70px" onchange="setRunT(${i},'start',this.value)"><b>结束</b> <input value="${esc(r.end||"")}" style="width:70px" onchange="setRunT(${i},'end',this.value)"><span style="flex:1"></span><button class="btn small ghost" onclick="W.schedule.runs.splice(${i},1);drawCfg()">删除</button></div>
   <div class="row small">${all.map(p=>`<label><input type="checkbox" ${(r.pools||[]).includes(p)?"checked":""} onchange="togRunPool(${i},'${p}',this.checked)"> ${esc(p)}</label>`).join("")}</div></div>`).join("")}
  <button class="btn" onclick="W.schedule.runs=W.schedule.runs||[];W.schedule.runs.push({name:'新时段',start:'13:00',end:'13:05',pools:[]});drawCfg()">添加时段</button></div>
  <div class="card"><h3>补录</h3>${kv([["启动时补录",inp("schedule.backfill","bool","当天已经过了、但还没跑过的时段，启动后自动补跑股池和漏斗，记录标「补录」并推送")],["补录也下单",inp("schedule.backfill_trade","bool","默认关闭：补录只记录不下单。打开后按自动交易开关下单（价格已不是时段内的价格）")]])}
   <div class="small mute">非交易日不补录；同一天重启不会重复补录。卖出检查和卖出前提醒的时刻在「卖出 → 卖出策略 → 卖出默认参数」里设置，会自动加入定时任务。</div></div>`}
function setRunT(i,k,v){const r=W.schedule.runs[i];r[k]=v.trim();if(k==="start")delete r.time}
