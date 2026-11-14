/* ---------------- 配置各分页 ---------------- */
function drawCfg(){if(TAB!=="config"){drawEmb();return}const m=$("#main");m.innerHTML=`<div class="card"><div class="row"><div class="seg">${CFGTABS.map(([k,n])=>`<button class="${k===CFGTAB?"on":""}" data-cfg="${k}" onclick="cfgTab('${k}')">${n}</button>`).join("")}</div>
  <span style="flex:1"></span><button class="btn" onclick="validateCfg()">校验</button><button class="btn primary" onclick="saveCfg()">保存并生效</button></div>
  <div class="small mute">这里只放系统配置。点「保存并生效」后写回 config/pipeline.toml（自动备份 .bak），不用重启；保存前会校验，有错误不会写入；密码、token 显示为掩码，不改就保持原值。设置项后面的小字是配置文件里的键名。</div><div class="small" id="movedto">按用途搬走的：选股与打分、卖出默认参数和按股池覆盖在 <a href="#buy/strategy" onclick="go('buy/strategy');return false">买入策略</a> / <a href="#sell/strategy" onclick="go('sell/strategy');return false">卖出策略</a>；每个股池用哪套策略、股池先后顺序、新入池方式在 <a href="#buy/pools" onclick="go('buy/pools');return false">买入 → 股池分配</a>；仓位与风控在 <a href="#account/risk" onclick="go('account/risk');return false">账户 → 仓位与风控</a>。</div><div id="cfgmsg"></div></div><div id="cfgbody"></div>`;
  (({sources:cfgSources,pools:cfgPools,plugins:cfgPlugins,schedule:cfgSchedule,live:cfgLive,notifycfg:cfgNotify,api:cfgApi,home:cfgHome,mode:cfgMode})[CFGTAB]||cfgSources)($("#cfgbody"))}
function cfgTab(k){CFGTAB=k;const nh="#"+hashOf("config");if(location.hash!==nh)history.replaceState(null,"",nh);drawCfg()}
/* 设置项标签：中文名 + 小字英文键名 + settings_docs 里的说明（干什么、什么时候生效、留空会怎样） */
function lab(key,title){const d=docOf(key);return `<div class="lab" data-doc="${esc(key)}"><b>${esc(title||(d&&d.title)||key)}</b> <span class="keyname">${esc(key)}</span></div>`}
/* 设置项说明放在输入框下方（小字）：干什么 / 生效 / 留空 */
function docBelow(key){const d=docOf(key);if(!d)return "";return `<div class="sd">${d.what?`<div class="small sd-what">${esc(sdClean(d.what))}</div>`:""}${sdLines(d)}</div>`}
/* 提交：两类数据源各自一段（不再提交旧的 sources 总表）；嵌在别的页面时只提交那一页的几段 */
function body(){const o={},secs=TAB!=="config"&&EMB?EMB.secs:null;for(const k in W)if(!RO.includes(k)&&k!=="sources"&&(!secs||secs.includes(k)))o[k]=W[k];return o}
async function validateCfg(){const r=await api("/api/config/validate",{method:"POST",body:body()});$("#cfgmsg").innerHTML=r.ok?'<div class="ok-box small">校验通过</div>':`<div class="warn small">${r.errors.map(esc).join("<br>")}</div>`;return r.ok}
const set=(path,v)=>{const ks=path.split(".");let o=W;for(let i=0;i<ks.length-1;i++){o[ks[i]]=o[ks[i]]??{};o=o[ks[i]]}o[ks.at(-1)]=v};
const get=path=>path.split(".").reduce((o,k)=>o==null?undefined:o[k],W);
function inp(path,type="number",hint=""){const v=get(path);if(type==="bool")return `<input type="checkbox" ${v?"checked":""} onchange="set('${path}',this.checked)"> <span class="small mute">${esc(hint)}</span>`;
  if(type==="list")return `<input value="${esc((v||[]).join(","))}" style="width:320px" onchange="set('${path}',this.value.split(/[,，\\s]+/).filter(Boolean))"> <span class="small mute">${esc(hint)}</span>`;
  if(type==="number")return `<input type="number" step="any" value="${v??""}" onchange="set('${path}',this.value===''?null:+this.value)"> <span class="small mute">${esc(hint)}</span>`;
  return `<input value="${esc(v??"")}" style="width:320px" onchange="set('${path}',this.value)"> <span class="small mute">${esc(hint)}</span>`}
function kv(rows){return `<div class="kv">${rows.map(([l,h])=>{const m=/data-doc="([^"]+)"/.exec(l||"");return `<div class="kl">${l}</div><div class="kc">${h}${m?docBelow(m[1].replace(/&amp;/g,"&")):""}</div>`}).join("")}</div>`}
function togglePool(k,on){let p=W.pool.pools||[];p=on?[...new Set([...p,k])]:p.filter(x=>x!==k);W.pool.pools=p;drawCfg()}
async function loadWl(){const w=await api("/api/watchlist");$("#wl").innerHTML=Object.entries(w).map(([n,x])=>`<div><b>${esc(n)}</b> · ${esc(x.csv||"未配置 csv")}</div>${x.csv?`<textarea id="wl_${n}" style="min-height:120px">${x.csv_rows.map(r=>r.join(",")).join("\n")}</textarea><button class="btn small" onclick="saveWl('${n}')">保存 CSV</button> <span class="small mute">每行：代码,名称；CSV 保存后立即生效</span>`:""}`).join("")}
async function saveWl(n){const rows=$("#wl_"+n).value.split("\n").map(l=>l.split(/[,，\t]/).map(x=>x.trim())).filter(r=>r[0]);const r=await api("/api/watchlist/"+n,{method:"PUT",body:{rows}});toast(`已保存 ${r.count} 只`)}
function cfgRisk(b){if(!DOCS){load("/api/settings-docs").then(d=>{DOCS=d;if(TAB==="account"||(TAB==="config"&&CFGTAB==="risk"))cfgRisk(b)}).catch(()=>{})}
  b.innerHTML=`<div class="card"><h3>仓位</h3>${kv([[lab("sizing.per_stock_amount","每只计划金额（元）"),inp("sizing.per_stock_amount","number","股数向下取整到一手")],[lab("sizing.buy_price_offset_pct","买入限价上浮 %"),inp("sizing.buy_price_offset_pct")],[lab("sizing.sell_price_offset_pct","卖出限价下浮 %"),inp("sizing.sell_price_offset_pct")]])}</div>
  <div class="card" id="riskcard"><h3>风控（所有模式生效）</h3>
   <div class="explain small" id="risknote"><b>新规则：</b>不限下单笔数（旧配置里的 max_daily_orders 不再生效，也不报错）。单笔上限、每日金额、黑白名单<b>只管买入</b>；止损、止盈、到点卖出和手动卖出都不受这些限制，止损永远能卖出去。</div>
   <div class="subh">只管买入</div>${kv([[lab("risk.max_order_amount","单笔买入上限（元）"),inp("risk.max_order_amount")],[lab("risk.max_daily_amount","每日买入金额上限（元）"),inp("risk.max_daily_amount")],
   [lab("risk.max_per_industry","同一行业最多买几只"),inp("risk.max_per_industry","number","含已有持仓；0 = 不限")],
   [lab("risk.whitelist","白名单"),inp("risk.whitelist","list","非空时只允许买这些代码")],[lab("risk.blacklist","黑名单"),inp("risk.blacklist","list","永远不买")],[lab("risk.one_buy_per_symbol_per_day","同票每天只买一次"),inp("risk.one_buy_per_symbol_per_day","bool")]])}
   <div class="subh">买卖都管</div>${kv([[lab("risk.max_price_deviation_pct","限价偏离参考价上限 %"),inp("risk.max_price_deviation_pct")],[lab("risk.enforce_session","交易时段检查"),inp("risk.enforce_session","bool","实盘始终强制检查")],
   ["<b>交易时段</b> <span class=\"keyname\">risk.sessions</span>",`<input value="${esc((W.risk.sessions||[]).map(s=>s.join("-")).join(","))}" onchange="W.risk.sessions=this.value.split(/[,，]/).filter(Boolean).map(x=>x.trim().split('-'))"> <span class="small mute">如 09:30-11:30,13:00-14:57</span>`]])}</div>
  <div class="card"><h3>模拟盘</h3>${kv([["初始资金",inp("paper.initial_cash","number","只在新建/重置账户时生效")],["佣金费率",inp("paper.commission_rate")],["最低佣金",inp("paper.min_commission")],["印花税（卖出）",inp("paper.stamp_tax_rate")],["滑点",inp("paper.slippage_rate")],["允许收盘后回放",inp("paper.ignore_session","bool")]])}</div>`}
/* 历史缓存清理插件：配置在 config/pipeline.toml 的 [cache_pruner]，插件每 5 秒重读，
   改完点「保存并生效」几秒内就用新值，不用重启/reload。命中清单走已有的
   /api/logs?q=历史缓存清理（插件不能挂路由），确认删除走已有的 /api/config。 */
var CP_LAST_MATCHED=0,CP_LAST_CUTOFF="",CP_LAST_DRY=1;
function cpFmtSize(n){n=+n||0;if(n<1024)return n+" B";if(n<1048576)return (n/1024).toFixed(1)+" KB";return (n/1048576).toFixed(1)+" MB"}
async function cpLoadReport(){const box=$("#cp_report");if(!box)return;
  box.innerHTML='<div class="small mute">读取清单中…</div>';
  let r;try{r=await api("/api/logs?q="+encodeURIComponent("历史缓存清理")+"&n=200",{quiet:true})}catch(e){box.innerHTML=`<div class="warn small">读取失败：${esc(e.message)}</div>`;return}
  const lines=r.lines||[];let idx=-1;for(let i=lines.length-1;i>=0;i--){if(lines[i].includes("历史缓存清理报告")){idx=i;break}}
  if(idx<0){CP_LAST_MATCHED=0;box.innerHTML='<div class="small mute">还没有清理报告。插件启动 20 秒后跑第一轮；日志里搜「历史缓存清理」。</div><div class="row" style="margin-top:8px"><button class="btn small" onclick="cpLoadReport()">刷新清单</button></div>';return}
  const parts=lines[idx].split("|").map(s=>s.trim()),kv={};for(let i=1;i<parts.length;i++){const m=/^(\w+)=(.*)$/.exec(parts[i]);if(m)kv[m[1]]=m[2]}
  const det=[];for(let i=idx+1;i<lines.length;i++){if(!lines[i].includes("历史缓存清理明细"))break;const p=lines[i].split("|").map(s=>s.trim());det.push({day:p[1]||"",size:p[2]||"0",path:p[3]||""})}
  CP_LAST_MATCHED=+(kv.matched||0);CP_LAST_CUTOFF=kv.cutoff||"";CP_LAST_DRY=+(kv.dry||1);
  const rows=det.map(d=>`<div class="row small" style="gap:10px;border-bottom:1px solid rgba(128,128,128,.25);padding:2px 0"><span style="width:92px">${esc(d.day)}</span><span style="width:72px;text-align:right">${esc(cpFmtSize(d.size))}</span><span style="flex:1;word-break:break-all">${esc(d.path)}</span></div>`).join("");
  const st=CP_LAST_DRY?'<span class="pill green">试运行（未删文件）</span>':'<span class="pill">已清理 '+esc(kv.removed||"0")+' 个</span>';
  box.innerHTML=`<div class="small" style="margin-top:6px">
    <div class="row" style="gap:12px;align-items:center">${st}<span>命中 <b>${CP_LAST_MATCHED}</b> 个 · 删除 <b>${esc(kv.removed||"0")}</b> 个 · 合计 <b>${esc(cpFmtSize(kv.bytes))}</b></span></div>
    <div class="mute">最近一轮 ${esc(kv.ran||"")} · 保留最近 ${esc(kv.keep||"")} 天（目录日期早于 ${esc(kv.cutoff||"")} 才算过期）</div>
    ${rows?`<div style="max-height:200px;overflow:auto;margin-top:6px">${rows}</div>`:'<div class="mute" style="margin-top:6px">这一轮没有命中任何过期文件。</div>'}
    <div class="row" style="gap:8px;margin-top:8px;align-items:center">
      <button class="btn small" onclick="cpLoadReport()">刷新清单</button>
      ${CP_LAST_MATCHED?`<button class="btn small" onclick="cpConfirmDelete()">${CP_LAST_DRY?"确认删除这 "+CP_LAST_MATCHED+" 个":"自动清理已开启"}</button>`:""}
      <span class="small mute">${CP_LAST_DRY?"先看清单，确认无误再点删除。":"当前已开启自动清理；想恢复只出报告，把上面「只出报告」重新勾上即可。"}</span>
    </div></div>`}
async function cpConfirmDelete(){
  if(!CP_LAST_MATCHED){toast("当前没有过期缓存，不需要删除",1);return}
  if(!CP_LAST_DRY){toast("自动清理已经是开启状态",1);return}
  if(!confirm("将永久删除 "+CP_LAST_MATCHED+" 个过期缓存文件（目录日期早于 "+CP_LAST_CUTOFF+"）。\n\n只删 data/cache/ 下的历史行情缓存，账本、策略、运行记录一律不动。\n\n确认后会开启自动清理（关掉「只出报告」）：这批立即删，以后每轮也会自动删过期缓存。想回到只出报告，把上面的勾重新打上即可。"))return;
  set("cache_pruner.dry_run",false);
  try{await saveCfg()}catch(e){toast("保存失败："+e.message,1);return}
  toast("已提交，插件 5 秒内执行清理；约 10 秒后自动刷新清单");
  setTimeout(cpLoadReport,11000)}
function cachePrunerCard(){W.cache_pruner=W.cache_pruner||{keep_days:30,dry_run:true};
  return `<div class="card" id="cacheprunercard"><h3>历史缓存清理 <span class="pill">插件</span></h3>
   <div class="explain small">按目录日期清理 <b>data/cache/</b> 下过期的历史行情缓存（分时、股池、指数分时），只保留最近 N 天。账本、策略、运行记录等状态文件一律不碰；其它自定义数据目录也不在清理范围内。</div>
   ${kv([[lab("cache_pruner.keep_days","只保留最近几天"),inp("cache_pruner.keep_days","number","天；填 0 = 完全不清理")],
         [lab("cache_pruner.dry_run","只出报告（不删文件）"),inp("cache_pruner.dry_run","bool","默认开着＝安全；关掉才会真的删文件")]])}
   <div class="small mute">改完点右上「保存并生效」，插件 <b>5 秒内</b> 用上新值，不用重启、不用重新加载插件。默认是「只出报告」，不会删任何文件；想真删，先看下面的命中清单再点确认。</div>
   <div id="cp_report" style="margin-top:8px"></div></div>`}
/* 插件列表：注册了哪些因子、数据源、渠道；加载失败或重名被拒的原因 */
async function cfgPlugins(b){b.innerHTML='<div class="card"><div class="empty">读取中…</div></div>';let r;try{r=await load("/api/plugins")}catch(e){b.innerHTML=`<div class="card warn bad">${esc(e.message)}</div>`;return}
  const its=r.items||[];const reg=x=>{const o=x.registered||x.registers||{};return [["因子",o.factors||x.factors],["数据源",o.sources||x.sources],["推送渠道",o.channels||x.channels]].map(([n,v])=>`<div><span class="small mute">${n}：</span>${(v||[]).length?(v||[]).map(k=>`<span class="pill">${esc(typeof k==="string"?k:(k.kind||k.name||JSON.stringify(k)))}</span>`).join(" "):'<span class="small mute">—</span>'}</div>`).join("")};
  b.innerHTML=`<div class="card" id="plugincard"><h3>插件 <span class="r"><button class="btn small" onclick="api('/api/plugins/reload',{method:'POST'}).then(()=>cfgPlugins($('#cfgbody')))">重新加载</button></span></h3>
   <div class="explain small">${esc(r.note||"")} 详细写法见项目里的 <b>${esc(r.doc||"docs/GUIDE.md")}</b>（「插件」一节）。</div>
   ${its.length?its.map(x=>{const bad=x.error||x.ok===false;const rej=x.conflicts||x.rejected||[];return `<div class="factor plug"><div class="row"><b>${esc(x.name||x.module||x.file)}</b><span class="keyname">${esc(x.file||x.path||"")}</span>${bad?'<span class="pill red">加载失败</span>':'<span class="pill green">已加载</span>'}</div>
     ${x.description?`<div class="small mute">${esc(x.description)}</div>`:""}${reg(x)}${bad&&x.error?`<div class="warn bad small">失败原因：${esc(x.error)}</div>`:""}${(rej||[]).length||x.warning?`<div class="warn small">重名被拒：${esc(x.warning||(rej.join("、")+" 和内置因子重名，没有注册"))}</div>`:""}</div>`}).join(""):'<div class="empty">plugins/ 目录里没有插件</div>'}</div>${cachePrunerCard()}`;cpLoadReport()}
function togRunPool(i,p,on){const r=W.schedule.runs[i];r.pools=on?[...new Set([...(r.pools||[]),p])]:(r.pools||[]).filter(x=>x!==p)}
function cfgNotify(b){const ch=W.notify.channels||[];b.innerHTML=`<div class="card"><h3>推送</h3>${kv([["启用推送",inp("notify.enabled","bool")]])}
  <h3 style="margin-top:10px">渠道</h3>${META.channels.map(c=>`<div class="factor"><label><input type="checkbox" ${ch.includes(c.type)?"checked":""} onchange="togCh('${c.type}',this.checked)"> <b>${esc(c.title)}</b></label>
   <button class="btn small" onclick="testCh('${c.type}',this)">发测试消息</button> <span id="tr_${c.type}" class="small"></span><div class="small mute">${esc(c.help)}</div>
   ${kv(c.fields.map(f=>[f.name,inp(`notify.${c.type}.${f.name}`,f.type==="list"?"list":"text",f.hint+(f.secret?"（已遮蔽，不改就保持原值）":""))]))}</div>`).join("")}
  <div class="small mute">「发测试消息」直接用本页表单里当前填的值，不用先保存，测试也不会写入配置；遮蔽显示的密钥没改就沿用已保存的值。确认能收到后，再点上方「保存并生效」。</div>
  <h3>事件</h3><div class="row">${Object.entries(META.events).filter(([k])=>k!=="test").map(([k,n])=>`<label><input type="checkbox" ${(W.notify.events||{})[k]!==false?"checked":""} onchange="W.notify.events=W.notify.events||{};W.notify.events['${k}']=this.checked"> ${esc(n)}</label>`).join("")}</div></div>`}
function togCh(t,on){let c=W.notify.channels||[];W.notify.channels=on?[...new Set([...c,t])]:c.filter(x=>x!==t)}
async function testCh(t,b){const ae=document.activeElement;if(ae&&ae.blur)ae.blur();b.disabled=true;const tx=b.textContent;b.textContent="发送中…";
  try{const r=await api("/api/notify/test",{method:"POST",body:{channel:t,config:JSON.parse(JSON.stringify((W.notify||{})[t]||{}))}});const ok=r.results.every(x=>x.ok);
   toast("测试（未保存的设置）\n"+r.results.map(x=>x.channel+"："+x.status).join("\n"),!ok);const el=$("#tr_"+t);if(el)el.innerHTML=r.results.map(x=>`<span class="pill ${x.ok?"green":"red"}">${esc(x.status)}</span>`).join(" ")}finally{b.disabled=false;b.textContent=tx}}
