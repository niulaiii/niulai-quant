/* ================= 新界面：导航图标、主题、下单程序页、漏斗图 ================= */
const ICONS={home:'<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
 funnel:'<path d="M3 4h18l-7 8.5V19l-4 2v-8.5z"/>',account:'<path d="M3 3v18h18"/><path d="m7 14 4-4 3 3 5-6"/>',
 broker:'<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',tiers:'<path d="M4 6h16M4 12h10M4 18h6"/>',
 backtest:'<path d="M12 8v4l3 2"/><path d="M3.05 11a9 9 0 1 1 .5 4M3 4v7h7"/>',config:'<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
 notify:'<path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.9 1.9 0 0 0 3.4 0"/>',
 sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
 moon:'<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',logo:'<path d="M3 4h18l-7 8.5V19l-4 2v-8.5z"/>'};
const ico=(k,s=16)=>`<svg width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICONS[k]||""}</svg>`;

let BK=null;
const NAVGRP=[["交易",MAIN_TABS],["更多",MORE_TABS]];
const SUBS={home:"大盘指数、市场情绪和各股池走势",account:"资金、持仓、委托记录、手动下单、仓位与风控",buy:"买入策略（含选股与打分）、每个股池用哪套策略和先后顺序、选股结果",sell:"卖出策略（含默认参数和按股池覆盖）、每个股池用哪套卖出策略、卖出结果",config:"系统配置：股池数据源、行情数据源、股池列表、时段、下单程序、推送、对外 API、插件、运行模式",backtest:"选策略、选股池，用日K回放",notify:"推送记录、运行日志、系统信息"};
ICONS.strategy='<path d="M4 6h10M4 12h16M4 18h7"/><circle cx="17" cy="6" r="2"/><circle cx="14" cy="18" r="2"/>';
function nav(){const name=Object.fromEntries(TABS);
  $("#nav").innerHTML=NAVGRP.map(([g,ks],gi)=>`<div class="grp ${gi?"more":""}">${g}</div>`+ks.map(k=>`<button class="${k===TAB?"on":""}" onclick="go('${k}')">${ico(k)}<span>${name[k]}</span></button>`).join("")).join("");
  $("#mnav").innerHTML=TABS.map(([k,n])=>`<button class="${k===TAB?"on":""}" onclick="go('${k}')">${n}</button>`).join("");
  $("#ptitle").textContent=name[TAB]||"首页";$("#psub").textContent=SUBS[TAB]||"";
  const sb=$("#subnav"),st=SUBTABS[TAB];if(sb){sb.style.display=st?"":"none";sb.innerHTML=st?st.map(([k,n])=>`<button class="${k===curSub()?"on":""}" data-sub="${k}" onclick="go('${TAB}/${k}')">${n}</button>`).join(""):""}
  drawTopStrat()
  $("#logo").innerHTML=ico("logo",16);$("#themebtn").innerHTML=ico(document.documentElement.dataset.theme==="light"?"moon":"sun",15)}
function toggleTheme(){const r=document.documentElement,t=r.dataset.theme==="light"?"dark":"light";r.dataset.theme=t;try{localStorage.setItem("qf-theme",t)}catch(e){}nav();if(TAB==="account"||TAB==="backtest")render()}
setInterval(()=>{const c=$("#clock");if(c)c.textContent=new Date().toLocaleTimeString("zh-CN",{hour12:false})},1000);
async function posChanged(){await reloadAcct(true);if(TAB!=="account")render()}
