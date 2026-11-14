const $=s=>document.querySelector(s);
/* 原地更新：新 HTML 和现有 DOM 逐个节点比对，只改变化的文字和属性。节点不被整体替换，展开状态、焦点、正在输入的值都保留，刷新时不闪 */
const _IH=Object.getOwnPropertyDescriptor(Element.prototype,"innerHTML");
function morph(el,html){if(!el)return;const t=document.createElement("template");_IH.set.call(t,html);morphKids(el,t.content)}
function sameNode(a,b){return a&&b&&a.nodeType===b.nodeType&&a.nodeName===b.nodeName&&(a.nodeType!==1||(a.id||"")===(b.id||""))}
function morphKids(a,b){let x=a.firstChild,y=b.firstChild;
  while(y){const ny=y.nextSibling;
    if(sameNode(x,y)){morphNode(x,y);x=x.nextSibling}
    else if(x&&sameNode(x.nextSibling,y)){const nx=x.nextSibling;a.removeChild(x);x=nx;morphNode(x,y);x=x.nextSibling}
    else a.insertBefore(y,x);
    y=ny}
  while(x){const nx=x.nextSibling;a.removeChild(x);x=nx}}
function morphNode(x,y){if(x.nodeType!==1){if(x.nodeValue!==y.nodeValue)x.nodeValue=y.nodeValue;return}
  const tag=x.tagName,keepOpen=tag==="DETAILS",focus=document.activeElement===x;
  for(const at of [...x.attributes])if(!y.hasAttribute(at.name)&&!(keepOpen&&at.name==="open"))x.removeAttribute(at.name);
  for(const at of y.attributes){if(keepOpen&&at.name==="open")continue;if(x.getAttribute(at.name)!==at.value)x.setAttribute(at.name,at.value)}
  if(tag==="INPUT"){if(!focus){const t=(y.getAttribute("type")||"").toLowerCase();if(t==="checkbox"||t==="radio")x.checked=y.hasAttribute("checked");else{const v=y.getAttribute("value")??"";if(x.value!==v)x.value=v}}return}
  if(tag==="TEXTAREA"){if(!focus&&x.value!==y.value)x.value=y.value;return}
  morphKids(x,y);
  if(tag==="SELECT"&&!focus){const i=[...x.options].findIndex(o=>o.hasAttribute("selected"));x.selectedIndex=i>=0?i:0}}
/* 主区域的 innerHTML 一律走原地更新 */
(()=>{const m=document.getElementById("main");Object.defineProperty(m,"innerHTML",{configurable:true,get(){return _IH.get.call(this)},set(h){morph(this,h)}})})();
/* 加载状态：只在顶栏放一个小圆点，请求超过 0.4 秒才亮，不改页面内容 */
let BUSYN=0,BUSYT=null;
function busy(d){BUSYN=Math.max(0,BUSYN+d);const el=document.getElementById("busy");if(!el)return;clearTimeout(BUSYT);if(BUSYN>0)BUSYT=setTimeout(()=>el.classList.add("on"),400);else el.classList.remove("on")}
/* 导航（第四轮）：首页、买入、卖出、账户、回测、设置、日志。每个大页可以有子页，地址写成 #买卖/子页 */
const TABS=[["home","首页"],["buy","买入"],["sell","卖出"],["account","账户"],["backtest","回测"],["config","设置"],["notify","日志"]];
const MAIN_TABS=["home","buy","sell","account"],MORE_TABS=["backtest","config","notify"];
const SUBTABS={buy:[["strategy","买入策略"],["pools","股池分配"],["results","选股结果"]],sell:[["strategy","卖出策略"],["pools","股池分配"],["results","卖出结果"]],account:[["overview","资金与持仓"],["risk","仓位与风控"]]};
/* 旧链接、旧 hash 自动跳到新位置 */
const LEGACY={funnel:"buy/results",tiers:"buy/results","buy/tiers":"buy/results","home/pools":"home","home/overview":"home",strategy:"buy/strategy",broker:"account",log:"notify",push:"notify",logs:"notify",pools:"home",
  "config/bind":"buy/pools",bind:"buy/pools","config/select":"buy/strategy",select:"buy/strategy","config/exits":"sell/strategy",exits_cfg:"sell/strategy","config/risk":"account/risk",risk:"account/risk",sizing:"account/risk","config/sizing":"account/risk",plugins:"config/plugins",exits:"sell/results","sell/check":"sell/results","buy/funnel":"buy/results"};
let SUB={};
function parseHash(h){h=(h||"").replace(/^#/,"");if(!h)return ["home",null];if(LEGACY[h])h=LEGACY[h];let [t,sub]=h.split("/");
  if(!TABS.some(x=>x[0]===t)){if(LEGACY[t])[t,sub]=LEGACY[t].split("/");else return ["home",null]}
  if(sub&&SUBTABS[t]&&!SUBTABS[t].some(x=>x[0]===sub))sub=null;return [t,sub||null]}
let TAB="home", META=null, ST=null, W=null, RO=[], CFGTAB="sources", SEQ=0;

function esc(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function toast(m,e){const d=document.createElement("div");d.className="toast"+(e?" err":"");d.textContent=m;document.body.appendChild(d);setTimeout(()=>d.remove(),e?7000:3000)}
/* 所有请求都带超时（默认 8 秒），超时就放弃并提示，页面永远不会被一个慢接口卡住 */
const TIMEOUT=8000;
async function api(path,opt={}){const ms=opt.timeout||TIMEOUT,quiet=opt.quiet,ctl=new AbortController(),tm=setTimeout(()=>ctl.abort(),ms);
  const o={headers:{"Content-Type":"application/json"},...opt,signal:ctl.signal};delete o.timeout;delete o.quiet;if(o.body&&typeof o.body!=="string")o.body=JSON.stringify(o.body);
  let r,j=null;
  try{r=await fetch(path,o);try{j=await r.json()}catch(e){if(e.name==="AbortError")throw e}}
  catch(e){const m=e.name==="AbortError"?`请求超时（${Math.round(ms/1000)} 秒）：${path}`:`连不上本系统的网页服务：${e.message}`;if(!quiet)toast(m,true);throw new Error(m)}
  finally{clearTimeout(tm)}
  if(!r.ok){const m=(j&&(typeof j.detail==="string"?j.detail:JSON.stringify(j.detail)))||r.statusText;if(!quiet)toast(m,true);throw new Error(m)}return j}
/* 缓存：先用上次的数据立刻画出页面，再异步拉最新的替换 */
const CACHE={};
function cget(p){if(p in CACHE)return CACHE[p];try{const s=localStorage.getItem("qf:"+p);if(s)return CACHE[p]=JSON.parse(s)}catch(e){}return null}
function cput(p,j){CACHE[p]=j;try{localStorage.setItem("qf:"+p,JSON.stringify(j))}catch(e){}}
async function load(p,opt={}){busy(1);try{const j=await api(p,{quiet:true,...opt});cput(p,j);return j}finally{busy(-1)}}
async function show(paths,draw){const my=SEQ,m=$("#main"),c=paths.map(cget);let drawn=false;
  if(c.every(x=>x!=null)){try{draw(...c);drawn=true}catch(e){console.warn(e)}}
  try{const f=await Promise.all(paths.map(p=>load(p)));if(my!==SEQ)return null;draw(...f);stale("");return f}
  catch(e){if(my!==SEQ)return null;if(drawn)stale("缓存数据",true,e.message);else m.innerHTML=`<div class="warn">加载失败：${esc(e.message)} <button class="btn small" onclick="render()">重试</button></div>`;return null}}
function stale(t,bad,tip){const el=$("#stale");if(!el)return;el.textContent=t;el.title=tip||"";el.className="pill "+(bad?"amber":"");el.style.display=t?"":"none"}
const f2=v=>v==null||v===""?"-":(+v).toFixed(2), pct=v=>v==null?"-":`<span class="${v>0?"up":v<0?"down":""}">${v>0?"+":""}${(+v).toFixed(2)}%</span>`;
const money=v=>v==null?"-":(+v).toLocaleString("zh-CN",{minimumFractionDigits:2,maximumFractionDigits:2});
const pcode=p=>typeof p==="string"?p:(p&&p.code)||"";
function hashOf(t,sub){if(t==="config")return CFGTAB&&CFGTAB!=="sources"?"config/"+CFGTAB:"config";return SUBTABS[t]?t+"/"+(sub||SUB[t]||SUBTABS[t][0][0]):t}
function curSub(){return SUBTABS[TAB]?(SUB[TAB]||SUBTABS[TAB][0][0]):null}
function go(h){let [t,sub]=parseHash(h);if(t==="config"&&sub){CFGTAB=sub}
  const leaving=(TAB==="buy"||TAB==="sell")&&curSub()==="strategy"&&STRAT.dirty&&(t!==TAB||(sub&&sub!=="strategy"));
  if(leaving&&!confirm(`${SIDE_N[STRAT.side]}有未保存的修改，确定离开？`+((t==="buy"||t==="sell")&&t!==TAB?"（切换会丢弃修改）":"（修改会保留在本页，回来还在）")))return;
  if((t==="buy"||t==="sell")&&STRAT.side!==t)STRAT.dirty=false;
  if(SUBTABS[t])SUB[t]=sub||SUB[t]||SUBTABS[t][0][0];TAB=t;const nh="#"+hashOf(t);if(location.hash!==nh)history.replaceState(null,"",nh);render();window.scrollTo(0,0)}
function drawHeader(s){if(!s)return;ST=s;drawTopCtl(s);const el=$("#hdrs");if(!el)return;
  morph(el,`<span class="pill ${s.mode==="live"?"red":"blue"}" id="modepill" title="运行模式，在「设置 → 运行模式」切换"><span class="d"></span>${esc(s.mode_label)}</span>${s.positions_auto?connPill():""}<span class="mute small num" id="hdrtime">${esc((s.now||"").slice(11,16))}${s.offline?" · 离线":""}</span>`)}
function refreshHeader(){drawHeader(cget("/api/status"));load("/api/status").then(async s=>{if(s.positions_auto)await loadBroker();drawHeader(s)}).catch(()=>{})}
const PAGES={home:()=>home,
  buy:()=>({strategy:m=>{STRAT.side="buy";return strategy(m)},pools:poolAssign,results:funnel})[curSub()]||funnel,
  sell:()=>({strategy:m=>{STRAT.side="sell";return strategy(m)},pools:poolAssign,results:sellResults})[curSub()]||sellResults,
  account:()=>({overview:account,risk:m=>embCfg(m,[cfgRisk],["sizing","risk","paper"])})[curSub()]||account,backtest:()=>backtest,config:()=>config,notify:()=>notify};
function render(){const my=++SEQ;nav();const m=$("#main"),key=TAB+"/"+(curSub()||"");if(m.dataset.tab!==key){m.dataset.tab=key;m.innerHTML='<div class="empty">加载中…</div>'}
  refreshHeader();const fn=(PAGES[TAB]||PAGES.home)();
  Promise.resolve().then(()=>fn(m)).catch(e=>{if(my===SEQ)m.innerHTML=`<div class="warn">页面出错：${esc(e.message)} <button class="btn small" onclick="render()">重试</button></div>`})}

