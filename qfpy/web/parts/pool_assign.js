/* ================= 第六轮（二）：股池分配搬进买入 / 卖出页，仓位与风控搬进账户页 ================= */
/* 股池分配：每个股池用哪套买入 / 卖出策略、优先顺序（拖动）、新入池方式。点选后立即保存（写回 config/pipeline.toml） */
let PA={side:"buy",pb:null,list:null,pool:null},PDRAG=null;
const ENTRY_OPTS=[["","跟随默认"],["day","每日重算"],["continuous","跨日连续"],["all","全部"]];
function pLabel(k){const n=pname(k,"");return n&&n!==k?`<b>${esc(n)}</b> <span class="keyname">${esc(k)}</span>`:`<b class="num">${esc(k)}</b>`}
function usedPools(side,sid,pb){return ((pb||cget("/api/pool-bind")||{}).rows||[]).filter(r=>r[side]&&r[side].id===sid).map(r=>r.pool)}
function usedChips(ks){return ks.length?ks.map(k=>`<span class="pill">${esc(pname(k,k))}</span>`).join(" "):'<span class="small mute">没有股池在用</span>'}
async function poolAssign(m){const side=TAB==="sell"?"sell":"buy",my=SEQ;PA.side=side;
  const c0=[cget("/api/pool-bind"),cget("/api/strategies/"+side),cget("/api/config")];if(c0.every(Boolean)){PA.pb=c0[0];PA.list=c0[1];PA.pool=c0[2].config.pool;await loadPNames();drawPA()}
  try{const [pb,lst,c]=await Promise.all([load("/api/pool-bind"),load("/api/strategies/"+side),load("/api/config"),loadPNames(),DOCS?null:load("/api/settings-docs").then(d=>DOCS=d)]);if(my!==SEQ)return;
    PA.pb=pb;PA.list=lst;PA.pool=c.config.pool;PA.exits=c.config.exits;drawPA()}
  catch(e){if(my===SEQ)m.innerHTML=`<div class="warn bad">读取股池分配失败：${esc(e.message)} <button class="btn small" onclick="render()">重试</button></div>`}}
function paOrder(){const pb=PA.pb||{},rows=pb.rows||[],pr=pb.buy_priority||[];return pr.filter(k=>rows.some(r=>r.pool===k)).concat(rows.map(r=>r.pool).filter(k=>!pr.includes(k)))}
function drawPA(){const m=$("#main"),side=PA.side,pb=PA.pb||{},rows=pb.rows||[],lst=PA.list||{items:[]},ks=paOrder(),P=PA.pool||{},sc=P.entry_scope_by_pool||{},sn=side==="buy"?"买入":"卖出";
  const row=k=>rows.find(r=>r.pool===k)||{pool:k,buy:{},sell:{}};
  const opts=r=>`<option value="" ${r[side].bound?"":"selected"}>跟随正在生效的：${esc((lst.active||{}).name||"")}</option>`+(lst.items||[]).map(x=>`<option value="${esc(x.id)}" ${r[side].bound&&r[side].id===x.id?"selected":""}>${esc(x.name)}${x.active?"（生效中）":""}</option>`).join("");
  const entrySel=k=>`<select class="paentry" onchange="paEntry('${esc(k)}',this.value)">${ENTRY_OPTS.map(([v,n])=>`<option value="${v}" ${(sc[k]||"")===v?"selected":""}>${n}</option>`).join("")}</select>`;
  const ovTxt=r=>r.override&&Object.keys(r.override).length?Object.entries(r.override).map(([k,v])=>`${esc(({max_hold_days:"持有天数",exit_time:"卖出时间",take_profit_pct:"止盈%",stop_loss_pct:"止损%"})[k]||k)} ${esc(v)}`).join("、"):'<span class="mute">沿用默认参数</span>';
  const tbl=`<div class="tw"><table class="prio" id="patable"><tr>${side==="buy"?"<th></th>":""}<th>顺序</th><th>股池</th><th>${sn}策略</th>${side==="buy"?"<th>新入池方式</th><th></th>":"<th>按股池覆盖的卖出参数</th>"}</tr>${ks.map((k,i)=>{const r=row(k);return `<tr data-pool="${esc(k)}" ${side==="buy"?`draggable="true" ondragstart="PDRAG=this.dataset.pool;this.classList.add('drag')" ondragend="this.classList.remove('drag')" ondragover="event.preventDefault()" ondrop="event.preventDefault();paDrop(this.dataset.pool)"`:""}>
     ${side==="buy"?'<td class="grip" title="按住拖动调整顺序">⠿</td>':""}<td class="num">${i+1}</td><td>${pLabel(k)}${r.configured===false?' <span class="pill amber">没启用</span>':""}</td>
     <td><select class="pastrat" onchange="paSet('${esc(k)}',this.value)">${opts(r)}</select><div class="small mute">${r[side].bound?"本池单独指定":"跟随正在生效的"}</div></td>
     ${side==="buy"?`<td>${entrySel(k)}</td><td class="nowrap"><button class="btn small ghost" ${i?"":"disabled"} onclick="paMove('${esc(k)}',-1)" title="上移">↑</button><button class="btn small ghost" ${i<ks.length-1?"":"disabled"} onclick="paMove('${esc(k)}',1)" title="下移">↓</button></td>`:`<td class="small">${ovTxt(r)}</td>`}</tr>`}).join("")||`<tr><td colspan="6" class="empty">还没有启用股池，去 <a href="#config/pools" onclick="go('config/pools');return false">设置 → 股池列表</a> 勾选</td></tr>`}</table></div>`;
  const used=`<div class="card" id="pausedby"><h3>每套${sn}策略在哪些股池用</h3><div class="kv">${(lst.items||[]).map(x=>`<div class="kl"><b>${esc(x.name)}</b>${x.active?' <span class="pill green">生效中</span>':""}</div><div class="kc">${usedChips(usedPools(side,x.id,pb))}</div>`).join("")}</div></div>`;
  const head=side==="buy"?`<div class="card" id="pacard"><h3>每个股池用哪套买入策略</h3>
    <div class="explain small">按住左边的 ⠿ 拖动整行调整<b>股池优先顺序</b>（手机上用 ↑ ↓）。一只票同时在几个股池里时，归排在前面的池、用那个池的买入策略；今天已经由一个池入选过的票，别的池不再重复买。卖出时按买入时记下的来源股池找卖出策略，顺序买卖共用。点选后立即保存。</div>${tbl}
    <div class="subh">新入池</div>${kv([[lab("pool.entry_mode","送进策略的票"),`<select id="paemode" onchange="paPool(o=>o.entry_mode=this.value)"><option value="new" ${(P.entry_mode||"new")==="new"?"selected":""}>只送新入池的票</option><option value="all" ${P.entry_mode==="all"?"selected":""}>每次送全部成分股</option></select>`],
      [lab("pool.entry_scope","新入池默认怎么算"),`<select id="paescope" onchange="paPool(o=>o.entry_scope=this.value)"><option value="day" ${(P.entry_scope||"day")==="day"?"selected":""}>每日重算</option><option value="continuous" ${P.entry_scope==="continuous"?"selected":""}>跨日连续</option></select>`]])}
    <div class="small mute">表格里每个股池的「新入池方式」选「跟随默认」就用上面这两项；选「全部」时这个池每次都把全部成分股送进策略。</div></div>
   <div class="card small mute">打分与最终选择（每次最多买几只、打分权重）属于每套买入策略自己，在 <a href="#buy/strategy" onclick="go('buy/strategy');return false">买入 → 买入策略</a> 里改。</div>`
   :`<div class="card" id="pacard"><h3>每个股池用哪套卖出策略</h3>
    <div class="explain small">从某个股池买进的票，卖出时用这个池选的卖出策略；没单独选的跟随正在生效的那套。止盈止损、持有天数、几点卖这些数字（默认参数和按股池覆盖）是每套卖出策略自己的，在 <a href="#sell/strategy" onclick="go('sell/strategy');return false">卖出 → 卖出策略</a> 里改。股池的先后顺序在 <a href="#buy/pools" onclick="go('buy/pools');return false">买入 → 股池分配</a> 里拖动调整，买卖共用。点选后立即保存。</div>${tbl}</div>
   <div class="card" id="exitswitch"><h3>卖出检查</h3>${kv([[lab("exits.enabled","启用卖出检查"),`<label><input type="checkbox" id="exen" ${(PA.exits||{}).enabled!==false?"checked":""} onchange="paExits(this.checked)"> 到了检查时刻就看持仓、按卖出策略卖</label>`]])}</div>`;
  m.innerHTML=head+used+'<div id="pamsg"></div>'}
function paErr(e){const el=$("#pamsg");if(el)el.innerHTML=`<div class="warn bad small">没有保存：${esc(zhErr(e.message))}<details><summary>详情</summary>${esc(e.message)}</details></div>`}
async function paSet(pool,v){try{PA.pb=await api("/api/pool-bind",{method:"PUT",quiet:true,body:{pool,[PA.side]:v}});cput("/api/pool-bind",PA.pb);toast("已保存");drawPA()}catch(e){paErr(e)}}
async function paOrderSave(ks){try{PA.pb=await api("/api/pool-bind",{method:"PUT",quiet:true,body:{order:ks}});cput("/api/pool-bind",PA.pb);toast("已保存股池顺序");drawPA()}catch(e){paErr(e)}}
function paMove(k,d){const ks=paOrder(),i=ks.indexOf(k);if(i<0||!ks[i+d])return;[ks[i],ks[i+d]]=[ks[i+d],ks[i]];paOrderSave(ks)}
function paDrop(k){const s=PDRAG;PDRAG=null;if(!s||s===k)return;const ks=paOrder().filter(x=>x!==s);ks.splice(ks.indexOf(k),0,s);paOrderSave(ks)}
async function cfgPatch(sec,fn){const c=await api("/api/config",{quiet:true});const o=JSON.parse(JSON.stringify(c.config[sec]||{}));fn(o);await api("/api/config",{method:"PUT",quiet:true,body:{[sec]:o}});const c2=await load("/api/config");return c2.config}
async function paPool(fn){try{const c=await cfgPatch("pool",fn);PA.pool=c.pool;toast("已保存");drawPA()}catch(e){paErr(e)}}
function paEntry(k,v){paPool(o=>{o.entry_scope_by_pool=o.entry_scope_by_pool||{};if(v)o.entry_scope_by_pool[k]=v;else delete o.entry_scope_by_pool[k]})}
async function paExits(on){try{const c=await cfgPatch("exits",o=>{o.enabled=on});PA.exits=c.exits;toast(on?"已启用卖出检查":"已关闭卖出检查");drawPA()}catch(e){paErr(e)}}
/* 设置项嵌进别的页面（账户 → 仓位与风控）：同一套表单函数，保存时只提交本页这几段 */
let EMB=null;
async function embCfg(m,panels,secs){const my=SEQ;META=cget("/api/meta")||await load("/api/meta");
  const [c,docs]=await Promise.all([load("/api/config"),DOCS?Promise.resolve(DOCS):load("/api/settings-docs").catch(()=>null)]);if(my!==SEQ)return;
  DOCS=docs||DOCS;W=JSON.parse(JSON.stringify(c.config));RO=c.readonly;CFGX=c;EMB={panels,secs};drawEmb()}
function drawEmb(){if(!EMB)return;const m=$("#main");m.innerHTML=`${EMB.panels.map((_,i)=>`<div id="emb${i}"></div>`).join("")}<div class="card savebar" id="embsave"><div class="row"><span class="small mute">本页改完点「保存并生效」，写回 config/pipeline.toml（自动备份 .bak），不用重启；保存前会校验，有错误不会写入。</span><span style="flex:1"></span><button class="btn" onclick="validateCfg()">校验</button><button class="btn primary" onclick="saveCfg()">保存并生效</button></div><div id="cfgmsg"></div></div>`;EMB.panels.forEach((f,i)=>f($("#emb"+i)))}
/* 卖出 / 买入策略页上：哪些股池在用这一套 */
function usedByLine(side){const sid=(STRAT.doc&&STRAT.doc.id)||STRAT.sid||(activeOf(side)||{}).id;const pb=cget("/api/pool-bind");if(!pb){load("/api/pool-bind").then(()=>{const el=$("#usedby");if(el)el.innerHTML=usedByLine(side)}).catch(()=>{});return ""}
  return `<span class="small mute">在用这一套的股池：</span>${usedChips(usedPools(side,sid,pb))} <a class="small" href="#${side}/pools" onclick="go('${side}/pools');return false">改股池分配</a>`}
