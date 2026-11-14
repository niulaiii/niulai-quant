/* ---------------- 买卖策略（多套）：正在生效条、设为生效确认、复制 / 剪贴板 / 导出 / 导入、模板、因子库弹窗、点选参数 ---------------- */
const STRAT={side:"buy",sid:null,doc:null,content:null,layers:null,loaded:null,dirty:false,list:null,active:null,tpls:null};
const SIDE_N={buy:"买入策略",sid:"",sell:"卖出策略"};
const L=i=>STRAT.layers[i],F=(i,j)=>STRAT.layers[i].factors[j];
let CAT=null,DOCS=null,INDS=null;
function catMap(){return Object.fromEntries(((CAT&&CAT.factors)||(META&&META.factors)||[]).map(f=>[f.kind,f]))}
function catName(id){const c=((CAT&&CAT.categories)||[]).find(x=>x.id===id);return c?c.name:(id||"")}
function poolName(k){const ps=((cget("/api/overview")||{}).pools||{}).pools||[];const p=ps.find(x=>x.key===k);return p&&p.name&&p.name!==k?p.name:""}
function poolLabel(k){const n=poolName(k);return n?`${esc(n)} <span class="small mute num">${esc(k)}</span>`:`<span class="num">${esc(k)}</span>`}
function poolKeys(){const pb=cget("/api/pool-bind");if(pb&&pb.rows)return pb.rows.map(r=>r.pool);return (((cget("/api/overview")||{}).pools||{}).pools||[]).map(p=>p.key)}
/* 难懂设置的说明（后端 settings_docs 一份）：干什么、什么时候生效、留空会怎样；英文键名小字放后面 */
function docOf(key){return (DOCS||cget("/api/settings-docs")||{})[key]||null}
function docHtml(key,title){const d=docOf(key);if(!d)return title?`<b>${esc(title)}</b> <span class="keyname">${esc(key)}</span>`:"";
  return `<div class="sdoc"><b>${esc(title||d.title)}</b> <span class="keyname">${esc(key)}</span><div class="small">${esc(d.what||"")}</div>
    ${sdLines(d)}</div>`}
/* 说明分行写，不再用「；」硬拼；句尾标点统一成一个句号 */
function sdClean(x){x=String(x||"").trim().replace(/[。；;，,、\s]+$/,"");return x?x+"。":""}
function sdLines(d){return (d.when?`<div class="small mute sd-when">生效：${esc(sdClean(d.when))}</div>`:"")+(d.empty?`<div class="small mute sd-empty">留空：${esc(sdClean(d.empty))}</div>`:"")}
function docTip(key){const d=docOf(key);return d?esc([d.what,d.when&&("生效："+d.when),d.empty&&("留空："+d.empty)].filter(Boolean).join("\n")):""}

/* 复制到剪贴板：先用 Clipboard API；局域网 IP（非 https）下浏览器不给用，换 execCommand；还不行就弹出全选好的文本框让人手动复制 */
async function copyText(text,what){what=what||"内容";
  try{if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(text);toast(`已复制${what}`);return "clipboard"}}catch(e){}
  try{const ta=document.createElement("textarea");ta.value=text;ta.setAttribute("readonly","");ta.style.cssText="position:fixed;top:0;left:0;opacity:0";document.body.appendChild(ta);ta.select();ta.setSelectionRange(0,text.length);
    const ok=document.execCommand&&document.execCommand("copy");ta.remove();if(ok){toast(`已复制${what}`);return "execCommand"}}catch(e){}
  copyBox(text,what);return "manual"}
function copyBox(text,what){const d=modal(`<h3>手动复制${esc(what||"")}</h3><div class="small mute">浏览器不允许网页直接写剪贴板（用局域网 IP 打开时常见）。下面已经全选好了，按 Ctrl+C（手机上长按 → 复制）。</div>
   <textarea id="copybox" class="copybox" readonly>${esc(text)}</textarea>`,"关闭",()=>true,"copymodal",false);
  const ta=d.querySelector("#copybox");setTimeout(()=>{ta.focus();ta.select()},30);return d}
function downloadText(name,text){const b=new Blob([text],{type:"application/json"}),a=document.createElement("a");a.href=URL.createObjectURL(b);a.download=name;document.body.appendChild(a);a.click();setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove()},500)}
function diffList(ch){ch=ch||[];if(!ch.length)return '<div class="ok-box small">没有差别</div>';
  return `<div class="tw"><table class="difft"><tr><th>哪里</th><th>原来</th><th>改成</th></tr>${ch.map(c=>`<tr><td>${esc(c.where)}</td><td class="small mute">${esc(c.before==null?"":typeof c.before==="string"?c.before:JSON.stringify(c.before))}</td><td class="small">${esc(c.after==null?"":typeof c.after==="string"?c.after:JSON.stringify(c.after))}</td></tr>`).join("")}</table></div>`}
function askText(title,label,val,okText,onOk){const d=modal(`<h3>${esc(title)}</h3><label class="fl">${esc(label)}<input id="asktxt" value="${esc(val||"")}"></label><div id="askmsg"></div>`,okText||"确定",async dd=>{const v=dd.querySelector("#asktxt").value.trim();if(!v){dd.querySelector("#askmsg").innerHTML='<div class="warn bad small">不能为空</div>';return false}
  try{await onOk(v);return true}catch(e){dd.querySelector("#askmsg").innerHTML=`<div class="warn bad small">${esc(e.message)}</div>`;return false}},"askmodal",false);setTimeout(()=>{const i=d.querySelector("#asktxt");i.focus();i.select()},30)}

async function strategy(m){const side=TAB==="sell"?"sell":"buy",my=SEQ;
  if(STRAT.side!==side){STRAT.side=side;STRAT.sid=null;STRAT.dirty=false;STRAT.loaded=null}
  const f=await show(["/api/catalog","/api/settings-docs","/api/strategies/"+side,"/api/strategy-templates/"+side,"/api/strategies-active","/api/pool-bind"],(cat,docs,lst,tpls,act)=>{
    CAT=cat;DOCS=docs;STRAT.list=lst;STRAT.tpls=tpls;STRAT.active=act;
    if(!STRAT.sid||!(lst.items||[]).some(x=>x.id===STRAT.sid))STRAT.sid=(lst.active||{}).id||((lst.items||[])[0]||{}).id;
    if(STRAT.loaded===side+"/"+STRAT.sid)drawStrat();else m.innerHTML='<div class="card"><div class="empty">正在读取策略…</div></div>'});
  if(!f||my!==SEQ)return;if(!STRAT.dirty||STRAT.loaded!==side+"/"+STRAT.sid)await openStrat(STRAT.sid,true)}
async function openStrat(sid,force){const side=STRAT.side;if(!sid){drawStrat();return}
  if(!force&&STRAT.dirty&&!confirm("当前策略有未保存的修改，切换会丢弃。继续？")){drawStrat();return}
  try{const d=await load(`/api/strategies/${side}/${encodeURIComponent(sid)}`);STRAT.sid=sid;STRAT.doc=d;STRAT.content=JSON.parse(JSON.stringify(d.content||{}));
    STRAT.layers=STRAT.content.layers=STRAT.content.layers||[];STRAT.loaded=side+"/"+sid;STRAT.dirty=false;drawStrat()}
  catch(e){toast("读取策略失败："+e.message,true)}}
function stratSide(s){go(s==="sell"?"sell/strategy":"buy/strategy")}
function dirty(redraw){STRAT.dirty=true;const d=$("#sdirty");if(d)d.innerHTML='<span class="pill amber">有未保存的修改</span>';const x=$("#sdiscard");if(x)x.disabled=false;if(redraw)drawStrat()}
function activeOf(side){return ((STRAT.active||cget("/api/strategies-active")||{})[side])||null}
function activeBar(side){const a=activeOf(side),act=STRAT.active||{};
  return `<div class="activebar"><span class="pill green"><span class="d"></span>正在生效</span> <b id="activename">${esc(a?a.name:"（没有）")}</b>
    <span class="small mute">${a&&a.since?"启用于 "+esc(a.since):""}${a&&a.by?"（"+esc(a.by)+"）":""}</span><span style="flex:1"></span>
    <span class="small">自动交易开关：${act.auto_trade?'<span class="pill red">开</span>':'<span class="pill">关</span>'}</span></div>
   <div class="row" id="usedby">${usedByLine(side)}</div>
   <div class="small mute" id="twothings">${esc(act.note||"自动交易开关和生效策略是两回事：生效策略决定怎么选、怎么卖；开关关着时照样运行，只是不下单。")}</div>`}
function drawStrat(){const m=$("#main"),side=STRAT.side,ls=STRAT.layers||[],lst=STRAT.list||{items:[]},a=activeOf(side),doc=STRAT.doc||{},isAct=a&&a.id===STRAT.sid;if(!STRAT.content){return}
  const y=window.scrollY;
  m.innerHTML=`<div class="card">${activeBar(side)}
   <div class="row" style="margin-top:10px"><label class="fl" style="min-width:220px">正在编辑<select id="stratpick" onchange="openStrat(this.value)">${(lst.items||[]).map(x=>`<option value="${esc(x.id)}" ${x.id===STRAT.sid?"selected":""}>${esc(x.name)}${x.active?"（生效中）":""}</option>`).join("")}</select></label>
    <span id="sdirty">${STRAT.dirty?'<span class="pill amber">有未保存的修改</span>':""}</span><span style="flex:1"></span>
    <button class="btn" onclick="stratCheck(this)">校验</button><button class="btn" id="sdiscard" onclick="stratDiscard()" ${STRAT.dirty?"":"disabled"}>放弃修改</button>
    <button class="btn primary" id="stratsave" onclick="stratSave(this)">保存</button>
    <button class="btn" id="stratact" onclick="stratActivate(this)" ${isAct?"disabled":""}>${isAct?"已在生效":"设为生效"}</button></div>
   ${isAct?`<div class="small mute" style="margin-top:6px">这套正在生效：保存后从下一次${side==="buy"?"选股":"卖出检查"}起按新内容来做，同时写回 config/pipeline.toml（自动备份 .bak）。</div>`:
     `<div class="warn" id="notactive" style="margin-top:8px">你正在改的是「${esc(doc.name||"")}」，它<b>现在没有生效</b>。保存只改这套，不影响自动${side==="buy"?"选股":"卖出"}；要用它，点「设为生效」。正在生效的是「${esc(a?a.name:"")}」。</div>`}
   ${doc.note?`<div class="small mute" style="margin-top:4px">备注：${esc(doc.note)}</div>`:""}
   <div class="row stratops" style="margin-top:10px"><button class="btn small primary" onclick="stratNewBlank()">新建空白策略</button><button class="btn small" onclick="stratCopyNew()">复制为新策略</button><button class="btn small" id="btnclip" onclick="stratClip()">复制到剪贴板</button>
    <button class="btn small" onclick="stratExport()">导出文件</button><button class="btn small" onclick="stratImport()">导入…</button><button class="btn small" onclick="stratSaveTpl()">存成模板</button>
    <button class="btn small ghost" onclick="stratRename()">改名</button><button class="btn small ghost" onclick="stratDelete()" ${isAct?"disabled title='正在生效的不能删'":""}>删除</button></div>
   <div class="small mute" style="margin-top:6px">${side==="buy"?"每个股池的新入池票逐层过滤：每层可放多个因子，按「全部满足 / 任一满足」组合；最后按打分取前 N 名买入。哪个股池用哪套买入策略在「买入 → 股池分配」里点选，没绑定的股池用正在生效的这套。":"每只持仓逐层检查，任何一层触发就卖。止损层永远最先检查、点「保留」也挡不住；买入当天不卖（T+1）。止损 / 止盈 / 持有天数 / 到点卖出的参数留空时用下面的「卖出默认参数」。"}</div><div id="smsg"></div></div>
   ${tplCard(side)}
   <div class="card"><h3>${SIDE_N[side]}的层（${ls.length} 层） <span class="r"><button class="btn small" onclick="addLayer()">+ 添加一层</button></span></h3>
    <div class="small mute" style="margin-bottom:6px">拖动左边的 ⋮⋮ 或点「上移 / 下移」调整顺序。</div>
    ${ls.length?ls.map((l,i)=>layerCard(l,i,ls.length,side)).join('<div class="arrow">↓</div>'):'<div class="empty">还没有层。套用一个模板，或点「添加一层」。</div>'}</div>
   ${side==="buy"?selectCard():exitsCard()}
   <details class="card"><summary><b>表达式可用字段</b> · ${((CAT&&CAT.fields)||[]).length} 个，加 [n] 回看前 n 天（如 close[1]）${side==="sell"?"；卖出另有 pnl_pct 浮盈%、held_days 持有天数等持仓字段":""}</summary>
    <div class="tw"><table>${((CAT&&CAT.fields)||[]).map(x=>`<tr><td class="num"><b>${esc(x.name)}</b></td><td class="small">${esc(x.desc)}</td><td class="small mute">${esc(catName(x.cat))}</td></tr>`).join("")}</table></div></details>
   <div class="card small mute">${side==="buy"?'选股结果（按层看、按每只票看）在 <a href="#buy/results" onclick="go(\'buy/results\');return false">买入 → 选股结果</a>。':'每次卖出检查的明细在 <a href="#sell/results" onclick="go(\'sell/results\');return false">卖出 → 卖出结果</a>。'}</div>`;
  window.scrollTo(0,y);bindDrag()}
/* 模板：自带的不可删改；套用前预览差异；设为默认 / 恢复默认 */
function tplCard(side){const ts=STRAT.tpls||[];
  return `<details class="card" id="tplbox"><summary><b>模板</b> · ${ts.length} 套。套用前先看差异，套用后只改本页草稿，点「保存」才写入</summary><div class="tpls">${ts.map(t=>`<div class="tpl"><b>${esc(t.title)}</b> ${t.builtin?'<span class="pill">自带</span>':'<span class="pill blue">自存</span>'}${t.is_default?' <span class="pill green">默认</span>':""}
     <div class="small mute">${esc(t.description||"")}</div><div class="small" style="margin:6px 0">${((t.content||{}).layers||[]).map(l=>esc(l.name)).join(" → ")}</div>
     <div class="row"><button class="btn small primary" onclick="tplPreview('${esc(t.id)}')">预览并套用</button>${t.is_default?"":`<button class="btn small ghost" onclick="tplDefault('${esc(t.id)}')">设为默认</button>`}
     ${t.builtin?"":`<button class="btn small ghost" onclick="tplRename('${esc(t.id)}')">改名</button><button class="btn small ghost" onclick="tplDelete('${esc(t.id)}')">删除</button>`}</div></div>`).join("")}</div>
   <div class="row" style="margin-top:8px"><button class="btn small ghost" onclick="tplDefault('')">恢复默认模板</button><span class="small mute">「默认」是点「新建空白策略」时用的模板；自带模板只读，想改就先套用再「存成模板」。</span></div></details>`}
async function tplPreview(tid){const side=STRAT.side;try{const r=await api(`/api/strategy-templates/${side}/${encodeURIComponent(tid)}/preview`,{method:"POST",body:{draft:STRAT.content}});
  modal(`<h3>套用模板「${esc(r.template||tid)}」</h3><div class="small mute">和本页当前内容比，会有这些变化。套用只改本页草稿，点「保存」才写入。</div>${diffList(r.changes)}`,"套用到本页",()=>{STRAT.content=JSON.parse(JSON.stringify(r.content));STRAT.layers=STRAT.content.layers=STRAT.content.layers||[];dirty(true);toast("已套用到本页，记得保存");return true},"tplmodal",false)}catch(e){}}
async function tplDefault(tid){try{await api(`/api/strategy-templates/${STRAT.side}/default`,{method:"POST",body:{id:tid||null}});toast(tid?"已设为默认模板":"已恢复默认模板");STRAT.tpls=await load("/api/strategy-templates/"+STRAT.side);drawStrat()}catch(e){}}
function tplRename(tid){const t=(STRAT.tpls||[]).find(x=>x.id===tid)||{};askText("模板改名","新名字",t.title,"改名",async v=>{await api(`/api/strategy-templates/${STRAT.side}/${encodeURIComponent(tid)}`,{method:"PUT",quiet:true,body:{title:v}});STRAT.tpls=await load("/api/strategy-templates/"+STRAT.side);drawStrat()})}
async function tplDelete(tid){if(!confirm("删除这个自存模板？"))return;try{await api(`/api/strategy-templates/${STRAT.side}/${encodeURIComponent(tid)}`,{method:"DELETE"});STRAT.tpls=await load("/api/strategy-templates/"+STRAT.side);drawStrat()}catch(e){}}
function stratSaveTpl(){askText("把本页内容存成模板","模板名字",(STRAT.doc||{}).name+" 模板","存成模板",async v=>{await api(`/api/strategy-templates/${STRAT.side}`,{method:"POST",quiet:true,body:{title:v,description:"",content:STRAT.content}});toast("已存成模板");STRAT.tpls=await load("/api/strategy-templates/"+STRAT.side);drawStrat()})}
/* 策略管理 */
async function refreshStratList(){STRAT.list=await load("/api/strategies/"+STRAT.side);STRAT.active=await load("/api/strategies-active");drawTopStrat()}
function stratCopyNew(){const d=STRAT.doc||{};askText("复制为新策略","新策略的名字",(d.name||"")+" 副本","复制",async v=>{
  const r=await api(`/api/strategies/${STRAT.side}`,{method:"POST",quiet:true,body:{name:v,content:STRAT.content,note:d.note||""}});toast(`已复制为「${r.name}」（没有生效）`);await refreshStratList();await openStrat(r.id,true)})}
async function stratNewBlank(){askText("新建空白策略","新策略的名字",STRAT.side==="buy"?"新买入策略":"新卖出策略","新建",async v=>{
  const r=await api(`/api/strategies/${STRAT.side}`,{method:"POST",quiet:true,body:{name:v,note:"从默认模板新建"}});toast(`已新建「${r.name}」（没有生效，改完点「设为生效」）`);await refreshStratList();await openStrat(r.id,true)})}
async function exportText(){const r=await api(`/api/strategies/${STRAT.side}/${encodeURIComponent(STRAT.sid)}/export`);if(STRAT.dirty)r.content=STRAT.content;return JSON.stringify(r,null,2)}
async function stratClip(){try{await copyText(await exportText(),"策略（JSON，已去掉密钥口令）")}catch(e){}}
async function stratExport(){try{const t=await exportText();downloadText(`${(STRAT.doc||{}).name||"strategy"}.${STRAT.side}.json`,t);toast("已导出（密钥口令已去掉）")}catch(e){}}
function stratImport(){const side=STRAT.side;let text="";
  const d=modal(`<h3>导入${SIDE_N[side]}</h3><div class="small mute">粘贴导出的 JSON，或选一个导出的文件。先列出和「${esc((STRAT.doc||{}).name||"")}」的差异，确认后存成新的一套（不会自动生效）。</div>
    <input type="file" id="impfile" accept=".json,application/json" style="margin:8px 0"><textarea id="imptext" class="copybox" placeholder="在这里粘贴"></textarea>
    <div class="row"><button class="btn small" id="impprev">列出差异</button><label class="fl" style="flex:1">存成的名字（可空）<input id="impname"></label></div><div id="impres"></div>`,"导入为新策略",async dd=>{
    text=dd.querySelector("#imptext").value;if(!text.trim()){dd.querySelector("#impres").innerHTML='<div class="warn bad small">先粘贴或选文件</div>';return false}
    try{const r=await api(`/api/strategies/${side}/import`,{method:"POST",quiet:true,body:{text,name:dd.querySelector("#impname").value.trim()||null}});toast(`已导入为「${r.name}」（没有生效）`);await refreshStratList();await openStrat(r.id,true);return true}
    catch(e){dd.querySelector("#impres").innerHTML=`<div class="warn bad small">没有导入：${esc(e.message)}</div>`;return false}},"impmodal",false);
  d.querySelector("#impfile").onchange=async e=>{const f=e.target.files[0];if(f){d.querySelector("#imptext").value=await f.text()}};
  d.querySelector("#impprev").onclick=async()=>{const t=d.querySelector("#imptext").value,box=d.querySelector("#impres");
    try{const r=await api(`/api/strategies/${side}/import/preview`,{method:"POST",quiet:true,body:{text:t,compare_to:STRAT.sid}});
      if(!d.querySelector("#impname").value)d.querySelector("#impname").value=r.name||"";
      box.innerHTML=`<div class="small" style="margin-top:8px">和「${esc(r.compare_to||"")}」比：</div>${diffList(r.changes)}${(r.errors||[]).length?`<div class="warn bad small">${r.errors.map(esc).join("<br>")}</div>`:""}`}
    catch(e){box.innerHTML=`<div class="warn bad small">${esc(e.message)}</div>`}}}
function stratRename(){const d=STRAT.doc||{};askText("策略改名","新名字",d.name,"改名",async v=>{await api(`/api/strategies/${STRAT.side}/${encodeURIComponent(STRAT.sid)}/rename`,{method:"POST",quiet:true,body:{name:v}});STRAT.doc.name=v;await refreshStratList();drawStrat()})}
async function stratDelete(){const d=STRAT.doc||{};if(!confirm(`删除策略「${d.name}」？删除后不能恢复（导出文件可以再导入）。`))return;
  try{await api(`/api/strategies/${STRAT.side}/${encodeURIComponent(STRAT.sid)}`,{method:"DELETE"});toast("已删除");STRAT.sid=null;STRAT.loaded=null;STRAT.dirty=false;render()}catch(e){}}
async function stratActivate(b){const side=STRAT.side,d=STRAT.doc||{},a=activeOf(side),act=STRAT.active||{};
  if(STRAT.dirty){toast("有未保存的修改：先保存，再设为生效",true);return}
  modal(`<h3>把「${esc(d.name)}」设为生效？</h3><div>从下一次${side==="buy"?"选股":"卖出检查"}起，${side==="buy"?"没绑定策略的股池":"没绑定策略的股池买进的持仓"}都按这套来做；现在生效的「${esc(a?a.name:"")}」会停用（不删除）。</div>
    <div class="small mute" style="margin-top:8px">会写进 config/pipeline.toml（自动备份 .bak）并推一条钉钉。自动交易开关不受影响，现在是「${act.auto_trade?"开":"关"}」：开关关着时照样运行，只是不下单。</div>`,"确认设为生效",async()=>{
    try{await api(`/api/strategies/${side}/${encodeURIComponent(STRAT.sid)}/activate`,{method:"POST",quiet:true,body:{confirm:true}});toast(`「${d.name}」已设为生效`);await refreshStratList();drawStrat();return true}
    catch(e){toast("没有设为生效："+e.message,true);return false}},"actmodal",false)}
function stratDiscard(){if(!confirm("放弃本页的修改，恢复成已保存的版本？"))return;STRAT.dirty=false;openStrat(STRAT.sid,true)}
function smsg(h){const el=$("#smsg");if(el)el.innerHTML=h}
async function stratCheck(b){b.disabled=true;try{const r=await api(`/api/strategies/${STRAT.side}/validate`,{method:"POST",quiet:true,body:{content:STRAT.content}});
  const errs=r.errors||[];smsg(errs.length?`<div class="warn bad small">校验没通过：${errs.map(esc).join("；")}</div>`:'<div class="ok-box small">校验通过（没有写入）</div>');return !errs.length}
  catch(e){smsg(`<div class="warn bad small">校验失败：${esc(e.message)}</div>`);return false}finally{b.disabled=false}}
async function stratSave(b){const side=STRAT.side,sid=STRAT.sid,d=STRAT.doc||{},isAct=(activeOf(side)||{}).id===sid;b.disabled=true;
  try{const pre=await api(`/api/strategies/${side}/${encodeURIComponent(sid)}`,{method:"PUT",quiet:true,body:{content:STRAT.content,dry_run:true}});
    modal(`<h3>保存「${esc(d.name)}」</h3>${isAct?'<div class="small">这套<b>正在生效</b>，保存后下一次运行就按新的来。</div>':'<div class="small">这套<b>没有生效</b>，保存不影响自动交易。</div>'}${diffList(pre.changes)}`,"保存",async()=>{
      try{await api(`/api/strategies/${side}/${encodeURIComponent(sid)}`,{method:"PUT",quiet:true,body:{content:STRAT.content}});toast(`已保存「${d.name}」`);STRAT.dirty=false;await refreshStratList();await openStrat(sid,true);smsg('<div class="ok-box small">已保存</div>');return true}
      catch(e){smsg(`<div class="warn bad small">没有保存：${esc(e.message)}</div>`);return true}},"savemodal",false)}
  catch(e){smsg(`<div class="warn bad small">没有保存：${esc(e.message)}</div>`)}finally{b.disabled=false}}

/* 层和因子 */
function layerCard(l,i,n,side){const cat=catMap(),fs=l.factors||[],stop=side==="sell"&&fs.some(f=>f.kind==="stop_loss"),on=l.enabled!==false,any=l.mode==="any";
  return `<div class="layer ${on?"":"off"} ${stop?"stop":""}" data-layer="${i}"><div class="lhead"><span class="drag" draggable="true" data-drag="L${i}" title="拖动调整顺序">⋮⋮</span><span class="ix">${i+1}</span>
    <input class="lname" value="${esc(l.name||"")}" onchange="L(${i}).name=this.value;dirty()">
    <span class="seg mode"><button class="${any?"":"on"}" onclick="setMode(${i},'all')">全部满足</button><button class="${any?"on":""}" onclick="setMode(${i},'any')">任一满足</button></span>
    <label class="small"><input type="checkbox" ${on?"checked":""} onchange="L(${i}).enabled=this.checked;dirty(true)"> 启用</label>
    ${side==="sell"?(stop?'<span class="pill red">止损层：永远最先检查</span>':`<label class="small" title="勾：这层说卖、票又点了「保留」时先不卖；挡多久在点「保留」时选（到下一次检查 / 到该交易日结束 / 到手工解除）。不勾：这层照卖，标记不消耗。止损层永远最先检查，保留挡不住。"><input type="checkbox" ${l.hold_skippable!==false?"checked":""} onchange="L(${i}).hold_skippable=this.checked;dirty()"> 点「保留」能挡卖出</label>`):""}
    <span style="flex:1"></span><button class="btn small ghost" ${i?"":"disabled"} onclick="mvL(${i},-1)">上移</button><button class="btn small ghost" ${i<n-1?"":"disabled"} onclick="mvL(${i},1)">下移</button><button class="btn small ghost" onclick="delL(${i})">删除层</button></div>
   <div class="small mute">${side==="buy"?(any?"本层任一开启的因子通过就保留":"本层所有开启的因子都通过才保留"):(any?"任一因子说该卖就卖":"所有因子都说该卖才卖")}</div>
   ${side==="sell"&&!stop?'<div class="small mute" style="margin-top:2px">不勾：这层照卖，保留标记还在</div>':''}
   ${fs.map((f,j)=>factorRow(i,j,f,fs.length,cat)).join("")||'<div class="empty" style="margin-top:6px">这一层还没有因子</div>'}
   <div class="row" style="margin-top:8px"><button class="btn small" onclick="openFacLib(${i})">+ 从因子库添加</button></div></div>`}
function setMode(i,m){L(i).mode=m;dirty(true)}
function facTags(c){const t=[];if(c.update)t.push(`<span class="pill ${c.update==="realtime"?"blue":""}">${esc((CAT&&CAT.update_names||{})[c.update]||c.update)}</span>`);
  if(c.backtest)t.push(`<span class="pill ${c.backtest==="no"?"amber":""}">${esc((CAT&&CAT.backtest_names||{})[c.backtest]||c.backtest)}</span>`);if(c.experimental)t.push('<span class="pill amber">实验性</span>');if(c.plugin)t.push('<span class="pill blue">插件</span>');return t.join("")}
function factorRow(i,j,f,n,cat){const c=cat[f.kind]||{title:f.kind,params:[],description:"未知因子（插件没加载？）"},on=f.enabled!==false;
  return `<div class="frow fac ${on?"":"off"}" data-fac="${i}.${j}"><div class="row"><span class="drag" draggable="true" data-drag="F${i}.${j}" title="拖动调整顺序">⋮⋮</span><label><input type="checkbox" ${on?"checked":""} onchange="F(${i},${j}).enabled=this.checked;dirty(true)"> <b>${esc(c.title)}</b></label><span class="keyname">${esc(f.kind)}</span>${facTags(c)}
   <input placeholder="显示名（可空）" value="${esc(f.label||"")}" style="width:150px" onchange="setLabel(${i},${j},this.value)">
   <span style="flex:1"></span><button class="btn small ghost" onclick="facPreview(${i},${j},this)" ${STRAT.side==="buy"?"":"hidden"}>试算</button><button class="btn small ghost" ${j?"":"disabled"} onclick="mvF(${i},${j},-1)">上移</button><button class="btn small ghost" ${j<n-1?"":"disabled"} onclick="mvF(${i},${j},1)">下移</button><button class="btn small ghost" onclick="delF(${i},${j})">删除</button></div>
   <div class="small mute">${esc(c.description||"")}</div>${c.params&&c.params.length?`<div class="params">${c.params.map(p=>paramIn(i,j,f,p)).join("")}</div>`:""}<div class="fprev small" id="fprev_${i}_${j}"></div></div>`}
function pval(f,name){const p=f.params||{};return name in p?p[name]:f[name]}
function defTxt(p){const d=p.default;if(d==null||d===""||(Array.isArray(d)&&!d.length))return "";if(p.choices){const c=p.choices.find(x=>x[0]===d);if(c)return c[1]}if(Array.isArray(d))return d.join("、");if(typeof d==="boolean")return d?"是":"否";return String(d)}
/* 参数一律点选：数字框 / 下拉 / 时间 / 多选 / 字段 / 行业 / 股池 / 数据源 → 因子；没填的显示沿用的默认值 */
function paramIn(i,j,f,p){const v=pval(f,p.name),dt=defTxt(p),ph=dt?"沿用默认 "+dt:"留空 = 沿用默认",id=`p_${i}_${j}_${p.name}`;let h,wide=false;
  const on=`onchange="setP(${i},${j},'${p.name}','${p.type}',this)"`;
  if(p.type==="bool")h=`<select ${on}><option value="" ${v==null?"selected":""}>沿用默认（${p.default?"是":"否"}）</option><option value="1" ${v===true?"selected":""}>是</option><option value="0" ${v===false?"selected":""}>否</option></select>`;
  else if(p.type==="number")h=`<span class="numu"><input type="number" step="any" value="${v??""}" placeholder="${esc(ph)}" ${on}>${p.unit?`<i>${esc(p.unit)}</i>`:""}</span>`;
  else if(p.type==="choice")h=`<select ${on}><option value="">${esc(dt?"沿用默认（"+dt+"）":"沿用默认")}</option>${(p.choices||[]).map(c=>`<option value="${esc(c[0])}" ${v!=null&&String(v)===String(c[0])?"selected":""}>${esc(c[1])}</option>`).join("")}</select>`;
  else if(p.type==="time")h=`<input type="time" value="${esc(v||"")}" ${on}><em class="small mute">${v?"":esc(ph)}</em>`;
  else if(p.type==="multi"){const cur=v==null?(p.default||[]):asList(v);h=`<span class="chips">${(p.choices||[]).map(c=>`<label class="chip ${cur.includes(c[0])?"on":""}"><input type="checkbox" ${cur.includes(c[0])?"checked":""} onchange="setMulti(${i},${j},'${p.name}','${esc(c[0])}',this.checked)">${esc(c[1])}</label>`).join("")}</span>${v==null?`<em class="small mute">${esc(ph)}</em>`:""}`}
  else if(p.type==="field")h=fieldSelect(v,on,ph);
  else if(p.type==="industries"){const cur=asList(v);h=`<span class="chips">${cur.map(x=>`<span class="chip on">${esc(x)}</span>`).join("")||'<span class="small mute">（没选）</span>'}</span> <button class="btn small" onclick="pickInds(${i},${j},'${p.name}')">点选行业</button>`}
  else if(p.type==="pools"){const cur=asList(v);h=`<span class="chips">${poolKeys().map(k=>`<label class="chip ${cur.includes(k)?"on":""}"><input type="checkbox" ${cur.includes(k)?"checked":""} onchange="setMulti(${i},${j},'${p.name}','${esc(k)}',this.checked)">${esc(poolName(k)||k)}</label>`).join("")||'<span class="small mute">没有配置股池</span>'}</span>`}
  else if(p.type==="ext_source"){wide=true;h=extSourcePick(i,j,f,p)}
  else if(p.type==="ext_factor"||p.type==="ext_field"){wide=true;h=extItemPick(i,j,f,p)}
  else if(p.type==="signal"){wide=true;h=signalPick(i,j,v)}
  else if(p.type==="conditions"){wide=true;h=condEditor(i,j,Array.isArray(v)?v:[])}
  else if(p.type==="expr"){wide=true;h=exprEditor(i,j,v||"")}
  else if(p.type==="list")h=`<input value="${esc(Array.isArray(v)?v.join(","):(v??""))}" placeholder="${esc(ph)}" ${on}>`;
  else h=`<input value="${esc(v??"")}" placeholder="${esc(ph)}" ${on}>`;
  const nm=p.name==="missing"?"数据缺失时":(p.label||p.hint||p.name);
  return `<label class="param ${wide?"wide":""}" id="${id}"><span>${esc(String(nm).split(/[（(，,；]/)[0]||p.name)} <span class="keyname">${esc(p.name)}</span></span>${h}${p.hint&&p.type!=="choice"?`<em class="small mute">${esc(p.hint)}</em>`:""}</label>`}
function fieldSelect(v,on,ph){const fs=(CAT&&CAT.fields)||[],cats=(CAT&&CAT.categories)||[];
  const groups=cats.map(c=>[c,fs.filter(x=>x.cat===c.id)]).filter(g=>g[1].length),rest=fs.filter(x=>!cats.some(c=>c.id===x.cat));
  return `<select ${on}><option value="">${esc(ph)}</option>${groups.map(([c,xs])=>`<optgroup label="${esc(c.name)}">${xs.map(x=>`<option value="${esc(x.name)}" ${v===x.name?"selected":""}>${esc(x.desc)}（${esc(x.name)}）</option>`).join("")}</optgroup>`).join("")}
    ${rest.map(x=>`<option value="${esc(x.name)}" ${v===x.name?"selected":""}>${esc(x.desc)}（${esc(x.name)}）</option>`).join("")}${v&&!fs.some(x=>x.name===v)?`<option value="${esc(v)}" selected>${esc(v)}</option>`:""}</select>`}
function setP(i,j,name,type,el){const f=F(i,j);f.params=f.params||{};let v;
  if(type==="bool")v=el.value===""?null:el.value==="1";else if(type==="number")v=el.value===""?null:+el.value;else if(type==="list")v=el.value.trim()?el.value.split(/[,，\s]+/).filter(Boolean):null;
  else v=el.value===""?null:el.value;
  if(name in f&&!(f.params&&name in f.params))delete f[name];if(v==null)delete f.params[name];else f.params[name]=v;dirty(["choice","time","bool","ext_source"].includes(type))}
/* 旧配置兼容：多选 / 行业 / 股池参数以前可能写成 "star,bse" 这样的字符串 */
function asList(v){return Array.isArray(v)?v.slice():(typeof v==="string"?v.split(/[,，、;；\s]+/).filter(Boolean):[])}
function setMulti(i,j,name,val,on){const f=F(i,j);f.params=f.params||{};const c=catMap()[f.kind]||{},p=(c.params||[]).find(x=>x.name===name)||{};
  let cur=f.params[name]!=null?asList(f.params[name]):(Array.isArray(p.default)?p.default.slice():[]);cur=cur.filter(x=>x!==val);if(on)cur.push(val);f.params[name]=cur;dirty(true)}
function setLabel(i,j,v){const f=F(i,j);if(v)f.label=v;else delete f.label;dirty()}
function mvL(i,d){const a=STRAT.layers;[a[i],a[i+d]]=[a[i+d],a[i]];dirty(true)}
function delL(i){if(!confirm(`删除第 ${i+1} 层「${L(i).name}」？`))return;STRAT.layers.splice(i,1);dirty(true)}
function addLayer(){STRAT.layers.push({name:"新的一层",mode:"all",enabled:true,factors:[]});dirty(true);setTimeout(()=>{const ls=document.querySelectorAll(".layer");if(ls.length)ls[ls.length-1].scrollIntoView({block:"center"})},0)}
function mvF(i,j,d){const a=L(i).factors;[a[j],a[j+d]]=[a[j+d],a[j]];dirty(true)}
function delF(i,j){L(i).factors.splice(j,1);dirty(true)}
function addFacKind(i,k){const c=catMap()[k];if(!c)return;const params=Object.fromEntries((c.params||[]).filter(p=>p.default!=null&&p.name!=="missing"&&!(Array.isArray(p.default)&&!p.default.length)&&p.default!=="").map(p=>[p.name,p.default]));
  L(i).factors=L(i).factors||[];L(i).factors.push({kind:k,enabled:true,params});dirty(true)}
/* 拖动排序：层拖到层上、因子拖到同一层的因子上 */
let DRAG=null;
function bindDrag(){document.querySelectorAll("[data-drag]").forEach(h=>{h.ondragstart=e=>{DRAG=h.dataset.drag;e.dataTransfer.effectAllowed="move";try{e.dataTransfer.setData("text/plain",DRAG)}catch(x){}}});
  document.querySelectorAll(".layer").forEach(el=>{el.ondragover=e=>{if(DRAG)e.preventDefault()};el.ondrop=e=>{e.preventDefault();if(!DRAG)return;const tl=+el.dataset.layer,fe=e.target.closest("[data-fac]");
    if(DRAG[0]==="L"){const s=+DRAG.slice(1);if(s!==tl){const a=STRAT.layers,[x]=a.splice(s,1);a.splice(tl,0,x);dirty(true)}}
    else{const [si,sj]=DRAG.slice(1).split(".").map(Number);const tj=fe?+fe.dataset.fac.split(".")[1]:(L(tl).factors||[]).length;const [x]=L(si).factors.splice(sj,1);L(tl).factors=L(tl).factors||[];L(tl).factors.splice(Math.min(tj,L(tl).factors.length),0,x);dirty(true)}
    DRAG=null}})}
async function facPreview(i,j,b){const box=$(`#fprev_${i}_${j}`);b.disabled=true;try{const r=await api("/api/factor/preview",{method:"POST",quiet:true,body:{factor:F(i,j)}});box.innerHTML=prevText(r)}catch(e){box.innerHTML=`<span class="warn-t">${esc(e.message)}</span>`}finally{b.disabled=false}}
function prevText(r){if(!r)return "";if(!r.ok)return `<span class="warn-t">${esc(r.error||"不能试算")}</span>`;
  return `今天能筛出 <b>${r.passed}</b> / ${r.checked} 只${r.missing?`，数据缺失 ${r.missing} 只`:""}${(r.examples||[]).length?"："+r.examples.slice(0,6).map(esc).join("、"):""} <span class="mute">（${esc(r.basis||"")}）</span>${(r.warnings||[]).length?`<div class="warn-t">${r.warnings.map(esc).join("；")}</div>`:""}`}

/* 因子库弹窗：12 类、搜索、按实时 / 盘后 / 能否回测 / 实验性筛选，每个因子有说明和例子 */
const LIB={cat:"",q:"",flt:{}};
function openFacLib(i){LIB.layer=i;const d=modal(`<h3>因子库 <span class="small mute">${esc(SIDE_N[STRAT.side])}</span></h3><input id="libq" placeholder="搜索：名字、说明、例子" value="${esc(LIB.q)}" style="width:100%">
    <div class="chips" id="libflt" style="margin:8px 0">${[["realtime","盘中实时"],["eod","盘后更新"],["bt","回测可用"],["exp","实验性"]].map(([k,t])=>`<label class="chip ${LIB.flt[k]?"on":""}"><input type="checkbox" data-f="${k}" ${LIB.flt[k]?"checked":""}>${t}</label>`).join("")}</div>
    <div class="libwrap"><div class="libcats" id="libcats"></div><div class="liblist" id="liblist"></div></div>`,"关闭",()=>true,"facLib",false);
  d.querySelector(".modal").classList.add("wide");d.querySelector('[data-act="no"]').style.display="none";
  d.querySelector("#libq").oninput=e=>{LIB.q=e.target.value;if(LIB.q.trim())LIB.cat="";drawLib()};d.querySelectorAll("#libflt input").forEach(x=>x.onchange=()=>{LIB.flt[x.dataset.f]=x.checked;x.parentNode.classList.toggle("on",x.checked);drawLib()});drawLib()}
function libMatch(f){const q=LIB.q.trim().toLowerCase(),fl=LIB.flt;if(!(f.sides||["buy"]).includes(STRAT.side))return false;
  if(q&&![f.title,f.kind,f.description,f.example,catName(f.cat)].join(" ").toLowerCase().includes(q))return false;
  if(fl.realtime&&f.update!=="realtime")return false;if(fl.eod&&f.update!=="eod")return false;if(fl.bt&&f.backtest==="no")return false;if(fl.exp&&!f.experimental)return false;return true}
function drawLib(){const fs=((CAT&&CAT.factors)||[]).filter(libMatch),cats=(CAT&&CAT.categories)||[],cb=$("#libcats"),lb=$("#liblist");if(!cb)return;
  cb.innerHTML=`<button class="${LIB.cat?"":"on"}" onclick="LIB.cat='';drawLib()">全部 <i>${fs.length}</i></button>`+cats.map(c=>{const n=fs.filter(f=>f.cat===c.id).length;return `<button class="${LIB.cat===c.id?"on":""}" title="${esc(c.desc||"")}" onclick="LIB.cat='${c.id}';drawLib()">${esc(c.name)} <i>${n}</i></button>`}).join("");
  const xs=fs.filter(f=>!LIB.cat||f.cat===LIB.cat);
  lb.innerHTML=xs.length?xs.map(f=>`<div class="libitem"><div class="row"><b>${esc(f.title)}</b><span class="keyname">${esc(f.kind)}</span>${facTags(f)}<span style="flex:1"></span><button class="btn small primary" onclick="addFacKind(${LIB.layer},'${f.kind}');document.getElementById('facLib').remove()">加入第 ${LIB.layer+1} 层</button></div>
    <div class="small">${esc(f.description||"")}</div>${f.example?`<div class="small mute">例子：${esc(f.example)}</div>`:""}${f.backtest_note?`<div class="small warn-t">${esc(f.backtest_note)}</div>`:""}</div>`).join(""):'<div class="empty">没有符合条件的因子</div>'}
/* 外部因子两级点选：先选数据源（显示是否可用），再选它提供的因子 / 字段 */
function extSources(){return (CAT&&CAT.ext_sources)||[]}
function extSourcePick(i,j,f,p){const v=pval(f,p.name)??p.default??"",xs=extSources(),cur=xs.find(x=>x.id===v);
  return `<span class="extpick"><span class="small mute">① 数据源</span><select class="extsrc" onchange="setP(${i},${j},'${p.name}','ext_source',this)"><option value="">（选数据源）</option>${xs.map(x=>`<option value="${esc(x.id)}" ${x.id===v?"selected":""}>${esc(x.title||x.id)}${x.available?"":"（不可用）"}</option>`).join("")}${v&&!cur?`<option value="${esc(v)}" selected>${esc(v)}（没配置）</option>`:""}</select>
   ${cur?(cur.available?'<span class="pill green">可用</span>':'<span class="pill amber">不可用</span>'):""}${cur?`<em class="small mute">${esc(cur.status||"")}${cur.latest_date?"；最近数据 "+esc(cur.latest_date):""}</em>`:""}</span>`}
function extItemPick(i,j,f,p){const src=pval(f,"source")??"",x=extSources().find(s=>s.id===src),v=pval(f,p.name)??"",items=x?(p.type==="ext_factor"?x.factors:x.fields)||[]:[];
  const nm=it=>typeof it==="string"?it:(it.name||it.id),ds=it=>typeof it==="string"?"":(it.desc||it.title||"");
  return `<span class="extpick"><span class="small mute">② ${p.type==="ext_factor"?"因子":"字段"}</span>${!src?'<span class="small mute">先在上面选数据源</span>':
    `<select onchange="setP(${i},${j},'${p.name}','${p.type}',this)"><option value="">（选${p.type==="ext_factor"?"因子":"字段"}）</option>${items.map(it=>`<option value="${esc(nm(it))}" ${nm(it)===v?"selected":""}>${esc(nm(it))}${ds(it)?" · "+esc(ds(it)):""}</option>`).join("")}${v&&!items.some(it=>nm(it)===v)?`<option value="${esc(v)}" selected>${esc(v)}（这个数据源还没提供）</option>`:""}</select>
     ${items.length?"":`<em class="small mute">这个数据源还没有提供${p.type==="ext_factor"?"因子":"字段"}${x&&!x.available?"（现在不可用）":""}</em>`}`}</span>`}
/* 行业点选 */
async function pickInds(i,j,name){let r;try{r=INDS=await load("/api/industries")}catch(e){r=INDS||{items:[]}}const f=F(i,j);f.params=f.params||{};const cur=new Set(asList(f.params[name]));
  const d=modal(`<h3>点选行业</h3><div class="small mute">${esc(r.note||"")}</div><input id="indq" placeholder="搜索行业" style="width:100%;margin:8px 0"><div class="chips indlist" id="indlist"></div>
    <div class="row" style="margin-top:8px"><input id="indadd" placeholder="列表里没有的行业名"><button class="btn small" id="indaddb">补一个</button></div>`,"确定",()=>{f.params[name]=[...cur];if(!cur.size)delete f.params[name];dirty(true);return true},"indmodal",false);
  const draw=()=>{const q=d.querySelector("#indq").value.trim(),all=[...new Set([...(r.items||[]).map(x=>x.name),...cur])];d.querySelector("#indlist").innerHTML=all.filter(x=>!q||x.includes(q)).map(x=>`<label class="chip ${cur.has(x)?"on":""}"><input type="checkbox" data-n="${esc(x)}" ${cur.has(x)?"checked":""}>${esc(x)}</label>`).join("")||'<span class="small mute">还没有行业数据：先运行一次选股，取过资料的票的行业会出现在这里</span>';
    d.querySelectorAll("#indlist input").forEach(el=>el.onchange=()=>{el.checked?cur.add(el.dataset.n):cur.delete(el.dataset.n);el.parentNode.classList.toggle("on",el.checked)})};
  d.querySelector("#indq").oninput=draw;d.querySelector("#indaddb").onclick=()=>{const v=d.querySelector("#indadd").value.trim();if(v){cur.add(v);d.querySelector("#indadd").value="";draw()}};draw()}
/* 自定义公式：点选插入字段和运算符、实时校验、预览当天能筛出几只 */
function exprEditor(i,j,v){const fs=(CAT&&CAT.fields)||[],ops=(CAT&&CAT.operators)||[];
  return `<span class="expred"><textarea id="expr_${i}_${j}" class="exprta" placeholder="例如 change_rate > 2 and turnover_rate < 15" oninput="exprInput(${i},${j},this)">${esc(v)}</textarea>
   <span class="row"><select id="exprf_${i}_${j}" onchange="exprIns(${i},${j},this.value);this.value=''"><option value="">+ 插入字段</option>${((CAT&&CAT.categories)||[]).map(c=>{const xs=fs.filter(x=>x.cat===c.id);return xs.length?`<optgroup label="${esc(c.name)}">${xs.map(x=>`<option value="${esc(x.name)}">${esc(x.desc)}（${esc(x.name)}）</option>`).join("")}</optgroup>`:""}).join("")}</select>
   <span class="opchips">${ops.map(o=>`<button class="btn small ghost" title="${esc(o.desc)}" onclick="exprIns(${i},${j},'${esc(o.op)}')">${esc(o.op)}</button>`).join("")}</span><button class="btn small" onclick="exprPreview(${i},${j},this)">预览今天能筛出几只</button></span>
   <span class="small" id="exprmsg_${i}_${j}"></span></span>`}
function exprIns(i,j,t){if(!t)return;const ta=$(`#expr_${i}_${j}`);if(!ta)return;const s=ta.selectionStart??ta.value.length,e=ta.selectionEnd??s;let ins=t;
  if(t==="( )")ins="( )";const pre=ta.value.slice(0,s),post=ta.value.slice(e);const sp=(pre&&!/\s$/.test(pre)&&!/^\[/.test(ins)?" ":"");ta.value=pre+sp+ins+(post&&!/^\s/.test(post)?" ":"")+post;const pos=(pre+sp+ins).length;ta.focus();ta.setSelectionRange(pos,pos);exprInput(i,j,ta)}
let EXPRT=null;
function exprInput(i,j,ta){const f=F(i,j);f.params=f.params||{};if(ta.value.trim())f.params.expr=ta.value;else delete f.params.expr;STRAT.dirty=true;const d=$("#sdirty");if(d)d.innerHTML='<span class="pill amber">有未保存的修改</span>';const x=$("#sdiscard");if(x)x.disabled=false;
  clearTimeout(EXPRT);EXPRT=setTimeout(async()=>{const box=$(`#exprmsg_${i}_${j}`);if(!box)return;if(!ta.value.trim()){box.innerHTML="";return}
    try{const r=await api("/api/formula/check",{method:"POST",quiet:true,body:{expr:ta.value,side:STRAT.side}});box.innerHTML=r.ok?`<span class="up-ok">✓ 公式没问题</span>，用到字段：${(r.fields||[]).map(esc).join("、")}`:`<span class="warn-t">✗ ${esc(r.error)}</span>`}catch(e){box.innerHTML=`<span class="warn-t">${esc(e.message)}</span>`}},350)}
async function exprPreview(i,j,b){const ta=$(`#expr_${i}_${j}`),box=$(`#exprmsg_${i}_${j}`);b.disabled=true;try{const r=await api("/api/formula/preview",{method:"POST",quiet:true,body:{expr:ta.value}});box.innerHTML=prevText(r)}catch(e){box.innerHTML=`<span class="warn-t">${esc(e.message)}</span>`}finally{b.disabled=false}}
/* 形态信号：选自带 / 自定义信号，或点选拼条件；自定义信号可以保存、改名、删除 */
function signalPick(i,j,v){const ss=(CAT&&CAT.signals)||[];
  return `<span class="row"><select onchange="setP(${i},${j},'file','signal',this)"><option value="">（不用现成信号，用下面的条件）</option><optgroup label="自带（只读）">${ss.filter(s=>s.builtin).map(s=>`<option value="${esc(s.file)}" ${v===s.file?"selected":""}>${esc(s.name)}</option>`).join("")}</optgroup>
    <optgroup label="自定义">${ss.filter(s=>!s.builtin).map(s=>`<option value="${esc(s.file)}" ${v===s.file?"selected":""}>${esc(s.name)}</option>`).join("")}</optgroup></select><button class="btn small" onclick="sigManager()">管理信号</button></span>
   ${v?`<em class="small mute">${esc((ss.find(s=>s.file===v)||{}).text||"")}</em>`:""}`}
function condFieldOpts(v){const fs=((CAT&&CAT.fields)||[]).filter(x=>x.daily);return fs.map(x=>`<option value="${esc(x.name)}" ${v===x.name?"selected":""}>${esc(x.desc)}（${esc(x.name)}）</option>`).join("")+(v&&!fs.some(x=>x.name===v)?`<option value="${esc(v)}" selected>${esc(v)}</option>`:"")}
function condRow(c,k,pre){const r=String(c.right??""),isF=r.startsWith("field:"),ops=[">",">=","<","<=","==","!="];
  return `<div class="cond" data-k="${k}"><select data-c="left">${condFieldOpts(c.left)}</select><label class="small">前<input type="number" min="0" data-c="leftDays" value="${c.leftDays||0}" style="width:52px">天</label>
   <select data-c="op">${ops.map(o=>`<option ${c.op===o?"selected":""}>${o}</option>`).join("")}</select>
   <select data-c="rtype"><option value="field" ${isF?"selected":""}>字段</option><option value="num" ${isF?"":"selected"}>数值</option></select>
   ${isF?`<select data-c="right">${condFieldOpts(r.slice(6))}</select><label class="small">前<input type="number" min="0" data-c="rightDays" value="${c.rightDays||0}" style="width:52px">天</label>`:`<input type="number" step="any" data-c="right" value="${esc(r)}" style="width:90px">`}
   <button class="btn small ghost" data-c="del">删除</button></div>`}
function readConds(box){return [...box.querySelectorAll(".cond")].map(el=>{const g=n=>el.querySelector(`[data-c="${n}"]`);const rt=g("rtype").value;
  const o={left:g("left").value,op:g("op").value,leftDays:+(g("leftDays").value||0)};if(rt==="field"){o.right="field:"+g("right").value;o.rightDays=+((g("rightDays")||{}).value||0)}else{o.right=g("right").value===""?0:+g("right").value;o.rightDays=0}return o})}
function condBox(box,conds,onChange){const draw=()=>{box.querySelector(".condrows").innerHTML=conds.map((c,k)=>condRow(c,k)).join("")||'<div class="small mute">还没有条件</div>';
    box.querySelectorAll(".cond").forEach(el=>{el.querySelectorAll("select,input").forEach(x=>x.onchange=()=>{const n=readConds(box);conds.splice(0,conds.length,...n);if(x.dataset.c==="rtype"){const k=+el.dataset.k;conds[k].right=x.value==="field"?"field:close":0;draw()}onChange(conds)});
      el.querySelector('[data-c="del"]').onclick=()=>{conds.splice(+el.dataset.k,1);draw();onChange(conds)}})};
  box.querySelector(".condadd").onclick=()=>{conds.push({left:"close",op:">",right:"field:ma5",leftDays:0,rightDays:0});draw();onChange(conds)};draw()}
function condEditor(i,j,conds){setTimeout(()=>{const box=document.getElementById(`cond_${i}_${j}`);if(!box||box.dataset.ok)return;box.dataset.ok=1;const cs=JSON.parse(JSON.stringify(conds));
    condBox(box,cs,n=>{const f=F(i,j);f.params=f.params||{};if(n.length)f.params.conditions=JSON.parse(JSON.stringify(n));else delete f.params.conditions;STRAT.dirty=true;const d=$("#sdirty");if(d)d.innerHTML='<span class="pill amber">有未保存的修改</span>'});
    box.querySelector(".condprev").onclick=async e=>{const out=box.querySelector(".condout");try{const f=F(i,j),p=f.params||{};const r=await api("/api/pattern-signals/preview",{method:"POST",quiet:true,body:p.file?{file:p.file}:{conditions:p.conditions||[]}});out.innerHTML=prevText(r)}catch(x){out.innerHTML=`<span class="warn-t">${esc(x.message)}</span>`}}},0);
  return `<span class="condbox" id="cond_${i}_${j}"><span class="condrows"></span><span class="row"><button class="btn small condadd">+ 加一个条件</button><button class="btn small condprev">预览今天能筛出几只</button><span class="small mute">条件之间是「并且」；选了上面的现成信号时以信号为准</span></span><span class="condout small"></span></span>`}
function sigItems(r){return Array.isArray(r)?r:((r||{}).items||[])}
async function sigManager(){let ss=[];try{ss=sigItems(await load("/api/pattern-signals"))}catch(e){}
  const d=modal(`<h3>形态信号</h3><div class="small mute">自带的只读；自定义的存在 data/signals/，可以保存、改名、删除。条件之间是「并且」。</div><div id="siglist"></div>
    <div class="subh">新建 / 修改自定义信号</div><label class="fl">名字<input id="signame"></label><div class="condbox" id="sigcond"><span class="condrows"></span><span class="row"><button class="btn small condadd">+ 加一个条件</button><button class="btn small" id="sigprev">预览今天能筛出几只</button></span></div><div id="sigout" class="small"></div>`,"保存为自定义信号",async dd=>{
    const name=dd.querySelector("#signame").value.trim(),out=dd.querySelector("#sigout");if(!name){out.innerHTML='<span class="warn-t">先写个名字</span>';return false}
    try{await api(SIGEDIT?`/api/pattern-signals/${encodeURIComponent(SIGEDIT)}`:"/api/pattern-signals",{method:SIGEDIT?"PUT":"POST",quiet:true,body:{name,conditions:conds}});toast("已保存信号");CAT=await load("/api/catalog");drawStrat();return true}catch(e){out.innerHTML=`<span class="warn-t">${esc(e.message)}</span>`;return false}},"sigmodal",false);
  d.querySelector(".modal").classList.add("wide");let SIGEDIT=null;const conds=[];const box=d.querySelector("#sigcond");condBox(box,conds,()=>{});
  const list=()=>{d.querySelector("#siglist").innerHTML=`<div class="tw"><table>${ss.map(s=>`<tr><td><b>${esc(s.name)}</b> ${s.builtin?'<span class="pill">自带</span>':'<span class="pill blue">自定义</span>'}</td><td class="small">${esc(s.text||"")}</td><td class="nowrap">${s.builtin?`<button class="btn small ghost" data-cp="${esc(s.id)}">以此为底新建</button>`:`<button class="btn small ghost" data-ed="${esc(s.id)}">修改</button><button class="btn small ghost" data-rn="${esc(s.id)}">改名</button><button class="btn small ghost" data-rm="${esc(s.id)}">删除</button>`}</td></tr>`).join("")}</table></div>`;
    const fill=(s,edit)=>{SIGEDIT=edit?s.id:null;d.querySelector("#signame").value=edit?s.name:s.name+" 改";conds.splice(0,conds.length,...JSON.parse(JSON.stringify(s.conditions||[])));condBox(box,conds,()=>{})};
    d.querySelectorAll("[data-cp]").forEach(b=>b.onclick=()=>fill(ss.find(s=>s.id===b.dataset.cp),false));d.querySelectorAll("[data-ed]").forEach(b=>b.onclick=()=>fill(ss.find(s=>s.id===b.dataset.ed),true));
    d.querySelectorAll("[data-rn]").forEach(b=>b.onclick=async()=>{const s=ss.find(x=>x.id===b.dataset.rn),n=prompt("新名字",s.name);if(!n)return;try{await api(`/api/pattern-signals/${encodeURIComponent(s.id)}/rename`,{method:"POST",body:{name:n}});ss=sigItems(await load("/api/pattern-signals"));list();CAT=await load("/api/catalog")}catch(e){}});
    d.querySelectorAll("[data-rm]").forEach(b=>b.onclick=async()=>{const s=ss.find(x=>x.id===b.dataset.rm);if(!confirm(`删除信号「${s.name}」？`))return;try{await api(`/api/pattern-signals/${encodeURIComponent(s.id)}`,{method:"DELETE"});ss=sigItems(await load("/api/pattern-signals"));list();CAT=await load("/api/catalog")}catch(e){}})};
  d.querySelector("#sigprev").onclick=async()=>{const out=d.querySelector("#sigout");try{out.innerHTML=prevText(await api("/api/pattern-signals/preview",{method:"POST",quiet:true,body:{conditions:conds}}))}catch(e){out.innerHTML=`<span class="warn-t">${esc(e.message)}</span>`}};list()}
/* 买入：选股打分（取前 N 名、打分权重） */
function selectCard(){const s=STRAT.content.select=STRAT.content.select||{},w=s.weights||{};
  return `<div class="card"><h3>选股与打分</h3>${docHtml("select.top_n","每次最多买几只")}<label class="fl" style="max-width:200px"><input type="number" min="0" value="${s.top_n??""}" placeholder="沿用默认" onchange="STRAT.content.select.top_n=this.value===''?undefined:+this.value;dirty()"></label>
   <div style="margin-top:10px">${docHtml("select.weights","打分权重")}</div><div id="wrows">${Object.entries(w).map(([k,v],x)=>`<div class="row" style="margin:4px 0">${fieldSelect(k,`onchange="wKey('${esc(k)}',this.value)"`,"选字段")}<input type="number" step="any" value="${v}" style="width:90px" onchange="STRAT.content.select.weights['${esc(k)}']=+this.value;dirty()"><button class="btn small ghost" onclick="delete STRAT.content.select.weights['${esc(k)}'];dirty(true)">删除</button></div>`).join("")||'<div class="small mute">没有权重：按漏斗通过的先后取前 N 名</div>'}</div>
   <button class="btn small" onclick="STRAT.content.select.weights=Object.assign(STRAT.content.select.weights||{}, {change_rate:1});dirty(true)">+ 加一个权重</button></div>`}
function wKey(old,nk){const w=STRAT.content.select.weights,o={};for(const [k,v] of Object.entries(w))o[k===old?nk:k]=v;STRAT.content.select.weights=o;dirty(true)}
/* 卖出：默认参数 + 按股池覆盖（黄金两点半例子） */
const EXK=[["take_profit_pct","止盈 %","number"],["stop_loss_pct","止损 %","number"],["max_hold_days","最多持有几天","number"],["exit_time","到点卖出的时间","time"]];
function exitsCard(){const e=STRAT.content.exits=STRAT.content.exits||{},bp=e.by_pool=e.by_pool||{},keys=Object.keys(bp),pk=poolKeys();
  const g=bp["gushi:gold_230"],ex=g?{k:"gushi:gold_230",o:g}:(keys.length?{k:keys[0],o:bp[keys[0]]}:null);
  const exTxt=ex&&ex.k==="gushi:gold_230"?`你现在的配置里，「黄金两点半」（gushi:gold_230）设成持有 ${ex.o.max_hold_days??e.max_hold_days} 天、${ex.o.exit_time||e.exit_time} 卖`:
    ex?`你现在的配置里，「${esc(poolName(ex.k)||ex.k)}」（${esc(ex.k)}）设成${EXK.filter(k=>ex.o[k[0]]!=null).map(k=>k[1]+" "+ex.o[k[0]]).join("、")}`:"比如「黄金两点半」（gushi:gold_230）设成持有 1 天、14:45 卖";
  return `<div class="card"><h3>卖出默认参数</h3><div class="small mute">卖出因子的参数留空时用这里的值；某个股池在下面单独设了的，那个池买进的票用它自己的。</div>
   <div class="fgrid" style="margin-top:8px">${EXK.map(([k,t,ty])=>`<label class="fl" title="${docTip("exits."+k)}">${t} <span class="keyname">${k}</span><input type="${ty}" ${ty==="number"?'step="any"':""} value="${esc(e[k]??"")}" onchange="exSet('${k}','${ty}',this.value)"></label>`).join("")}
    <label class="fl" title="${docTip("exits.remind_minutes_before")}">到点前几分钟提醒 <span class="keyname">remind_minutes_before</span><input type="number" value="${esc(e.remind_minutes_before??"")}" onchange="exSet('remind_minutes_before','number',this.value)"></label></div>
   <div class="fl">每天检查的时间 <span class="keyname">check_times</span><span class="chips">${(e.check_times||[]).map((t,x)=>`<span class="chip on">${esc(t)} <a href="#" onclick="STRAT.content.exits.check_times.splice(${x},1);dirty(true);return false">×</a></span>`).join("")}<input type="time" id="addct" style="width:110px"><button class="btn small" onclick="addCheckTime()">+ 加</button></span></div>
   <label class="small"><input type="checkbox" ${e.hold_can_skip_stop_loss?"checked":""} onchange="STRAT.content.exits.hold_can_skip_stop_loss=this.checked;dirty()"> 点「保留」也能挡住止损 <span class="keyname">hold_can_skip_stop_loss</span></label></div>
  <div class="card" id="bypool"><h3>不同股池用不同的卖出参数</h3>
   <div class="explain"><div>按<b>买入时记下的来源股池</b>生效：从某个池买进的票，卖的时候用这里给这个池设的数字，没设的项沿用上面的默认参数。一只票同时在几个池里时，按下面的顺序取第一个设了的。</div>
    <div style="margin-top:6px">这是<b>这套卖出策略的一部分</b>：哪个股池用哪套卖出策略，在「卖出 → 股池分配」里点选；选定那套之后，再用这里的数字微调止盈止损、持有天数、几点卖。两者不会互相打架。</div>
    <div class="example" id="goldex"><b>例子：</b>${exTxt}。从这个池买进的票，第二天（持有满 ${ex?(ex.o.max_hold_days??e.max_hold_days):1} 天）到 ${ex?(ex.o.exit_time||e.exit_time):"14:45"} 就卖；其他池买进的票仍按默认：持有 ${e.max_hold_days??"-"} 天、${esc(e.exit_time||"-")} 卖，止盈 ${e.take_profit_pct??"-"}% / 止损 ${e.stop_loss_pct??"-"}%。</div>
    <div class="small mute">配置里写在 <span class="keyname">[exits.by_pool]</span>（这套策略里），网页上点选就行，不用手改。</div></div>
   ${keys.map((k,x)=>`<div class="bprow"><div class="row"><b>${x+1}.</b> ${poolLabel(k)}<span style="flex:1"></span><button class="btn small ghost" ${x?"":"disabled"} onclick="bpMove(${x},-1)">上移</button><button class="btn small ghost" ${x<keys.length-1?"":"disabled"} onclick="bpMove(${x},1)">下移</button><button class="btn small ghost" onclick="delete STRAT.content.exits.by_pool['${esc(k)}'];dirty(true)">删除</button></div>
    <div class="fgrid">${EXK.map(([f,t,ty])=>`<label class="fl">${t}<input type="${ty}" ${ty==="number"?'step="any"':""} value="${esc(bp[k][f]??"")}" placeholder="沿用默认 ${esc(e[f]??"")}" onchange="bpSet('${esc(k)}','${f}','${ty}',this.value)">${ty==="time"&&bp[k][f]==null?`<em class="small mute">沿用默认 ${esc(e[f]||"")}</em>`:""}</label>`).join("")}</div></div>`).join("")||'<div class="small mute">还没有单独设参数的股池。</div>'}
   <div class="row" style="margin-top:8px"><select id="bpadd">${pk.filter(k=>!keys.includes(k)).map(k=>`<option value="${esc(k)}">${esc(poolName(k)||k)}（${esc(k)}）</option>`).join("")}</select><button class="btn small" onclick="bpAdd()">+ 给这个股池单独设</button></div></div>`}
function exSet(k,ty,v){const e=STRAT.content.exits;if(v==="")delete e[k];else e[k]=ty==="number"?+v:v;dirty()}
function addCheckTime(){const v=($("#addct")||{}).value;if(!v)return;const e=STRAT.content.exits;e.check_times=[...new Set([...(e.check_times||[]),v])].sort();dirty(true)}
function bpSet(k,f,ty,v){const o=STRAT.content.exits.by_pool[k];if(v==="")delete o[f];else o[f]=ty==="number"?+v:v;dirty()}
function bpAdd(){const k=($("#bpadd")||{}).value;if(!k)return;STRAT.content.exits.by_pool[k]={};dirty(true)}
function bpMove(x,d){const bp=STRAT.content.exits.by_pool,ks=Object.keys(bp);[ks[x],ks[x+d]]=[ks[x+d],ks[x]];STRAT.content.exits.by_pool=Object.fromEntries(ks.map(k=>[k,bp[k]]));dirty(true)}
async function refreshPreview(b){b.disabled=true;try{const p=await load("/api/sell/preview?refresh=1");cput("/api/sell/preview",p);const el=$("#sellMatrix");if(el)el.innerHTML=sellMatrix(p);pollPreview()}catch(e){toast(e.message,true)}finally{b.disabled=false}}
function pollSync(n=0){const my=SEQ;if(n>90)return;setTimeout(async()=>{if(my!==SEQ||TAB!=="account")return;try{const a=await load("/api/account");if(my!==SEQ||TAB!=="account")return;drawAccount($("#main"),a,cget("/api/sell/preview"));const sy=(a.broker||{}).sync||{};
  if(!a.auto_positions)return;if(sy.running)pollSync(n+1);else{refreshHeader();if(sy.sync_error)toast("读取失败："+sy.sync_error,true)}}catch(e){pollSync(n+1)}},1000)}
/* 自动获取持仓打开时：后台读券商数据 */
async function bsync(how,btn){btn.disabled=true;try{await api("/api/broker/sync",{method:"POST",body:{how}});await reloadAcct();pollSync()}catch(e){btn.disabled=false}}
