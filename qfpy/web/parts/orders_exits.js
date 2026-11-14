/* ---------------- 委托/退出表、保留与核对、曲线 ---------------- */
function orderTable(list){if(!list||!list.length)return'<div class="mute small">无</div>';
  return `<div class="tw"><table><tr><th>时间</th><th>方向</th><th>代码</th><th>数量</th><th>限价</th><th>状态</th><th>原因 / 结果</th></tr>${list.map(o=>`<tr><td class="small">${esc(o.created_at||"")}</td><td>${o.side==="buy"?'<span class="up">买</span>':'<span class="down">卖</span>'}</td><td>${o.symbol} ${esc(o.name||"")}</td><td>${o.quantity??""}</td><td>${f2(o.price)}</td>
   <td><span class="pill ${({filled:"green",dry_run:"blue",submitted:"green",signal_only:"amber",hold_skipped:"amber",risk_rejected:"red",rejected:"red",unknown:"red"})[o.status]||""}">${esc(o.status_label||o.status)}</span></td>
   <td class="small">${esc(o.reason||"")}<br>${esc(o.message||"")}${o.checks?`<details><summary>风控明细</summary>${o.checks.map(c=>`${c.ok?"✓":"✗"} ${esc(c.name)}：${esc(c.note)}`).join("<br>")}</details>`:""}</td></tr>`).join("")}</table></div>`}
function exitTable(list){return `<div class="tw"><table><tr><th>代码</th><th>现价</th><th>浮盈</th><th>持有</th><th>判断</th><th>动作</th></tr>${list.map(e=>`<tr><td>${e.symbol} ${esc(e.name)}</td><td>${f2(e.price)}</td><td>${pct(e.pnl_pct)}</td><td>${e.held_days} 天</td><td class="small">${esc(e.reason)}</td><td class="small">${esc(e.action||"-")} ${esc(e.note||"")}</td></tr>`).join("")}</table></div>`}
/* 人工保留：点「保留」时选挡多久（模式值要和 hold_policy 插件一致） */
const HOLD_MODES=[["until_next_check","到下一次检查","只挡这一次，下一次检查恢复判断"],["until_day_end","到该交易日结束","当天剩下的卖出检查都挡，日终自动失效"],["until_manual","到手工解除","一直挡，直到你点「取消保留」"]];
const HOLD_MODE_LABEL=Object.fromEntries(HOLD_MODES.map(x=>[x[0],x[1]]));
async function setHold(code,on){
  if(!on){await api(`/api/positions/${code}/hold`,{method:"POST",body:{hold:false}});toast("已取消「保留」");await posChanged();return}
  const css=`<style>
#holdmodal h3{margin:0 0 10px}
#holdmodal .hm-lead{color:var(--text2);line-height:1.6;margin:0 0 8px}
#holdmodal .hm-tip{font-size:11.5px;color:var(--mute);background:var(--card2);border:1px solid var(--line);border-radius:8px;padding:7px 10px;margin:0 0 14px;line-height:1.55}
#holdmodal .hm-seg{position:relative;display:grid;grid-template-columns:repeat(3,1fr);padding:3px;background:var(--card2);border:1px solid var(--line);border-radius:10px;margin:0 0 10px;user-select:none;touch-action:none}
#holdmodal .hm-thumb{position:absolute;left:3px;top:3px;bottom:3px;width:calc((100% - 6px)/3);background:var(--accent);border-radius:7px;transition:transform .22s cubic-bezier(.4,0,.2,1);pointer-events:none}
#holdmodal .hm-seg.drag .hm-thumb{transition:none}
#holdmodal .hm-segi{position:relative;z-index:1;text-align:center;padding:8px 4px;font-size:12px;color:var(--mute);cursor:pointer;white-space:nowrap;transition:color .18s}
#holdmodal .hm-segi.on{color:#fff;font-weight:600}
#holdmodal .hm-segi input{position:absolute;opacity:0;width:0;height:0}
#holdmodal .hm-desc{font-size:12px;color:var(--text2);background:var(--card2);border:1px solid var(--line);border-radius:8px;padding:8px 10px;line-height:1.6;margin:0 0 4px}
#holdmodal .hm-r{margin-top:14px}
#holdmodal .hm-r label{display:block;font-size:12px;color:var(--text2);margin-bottom:6px}
#holdmodal .hm-r input{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--input);color:var(--text);font:inherit;font-size:13px}
</style>`;
  const html=css+`<h3>保留 ${esc(code)}</h3>`+
    `<div class="hm-lead">选挡多久。三种都挡不住止损，碰到止损照卖。</div>`+
    `<div class="hm-tip">后两种要「保留策略」插件启用；插件没启用时，选了也只会按「到下一次检查」处理。</div>`+
    `<div class="hm-seg" id="hmseg"><span class="hm-thumb" id="hmthumb"></span>`+
    HOLD_MODES.map(([v,t],i)=>`<label class="hm-segi"><input type="radio" name="hmode" value="${v}" ${i?"":"checked"}><span>${t}</span></label>`).join("")+
    `</div><div class="hm-desc" id="hmdesc">${HOLD_MODES[0][2]}</div>`+
    `<div class="hm-r"><label>原因（选填）</label><input id="hnote" placeholder="可留空"></div>`;
  const dlg=modal(html,"确定",async d=>{
    const mode=(d.querySelector('input[name="hmode"]:checked')||{}).value||"until_next_check";
    const note=(d.querySelector("#hnote")||{}).value||"";
    try{await api(`/api/positions/${code}/hold`,{method:"POST",body:{hold:true,note,mode}});
      toast(`已点「保留」：${HOLD_MODE_LABEL[mode]||mode}`);await posChanged();return true}
    catch(e){toast(e.message,true);return false}
  },"holdmodal",false);
  const rs=[...dlg.querySelectorAll('input[name="hmode"]')];
  const sync=()=>{
    const i=Math.max(0,rs.findIndex(r=>r.checked));
    dlg.querySelectorAll(".hm-segi").forEach((el,k)=>el.classList.toggle("on",k===i));
    dlg.querySelector("#hmthumb").style.transform=`translateX(${i*100}%)`;
    dlg.querySelector("#hmdesc").textContent=HOLD_MODES[i][2];
  };
  rs.forEach(r=>r.addEventListener("change",sync));sync();
  const seg=dlg.querySelector("#hmseg");
  const pick=e=>{
    const b=seg.getBoundingClientRect(),w=(b.width-6)/3;
    const i=Math.min(2,Math.max(0,Math.floor((e.clientX-b.left-3)/w)));
    if(!rs[i].checked){rs[i].checked=true;sync()}
  };
  let drag=false;
  seg.addEventListener("pointerdown",e=>{drag=true;seg.classList.add("drag");try{seg.setPointerCapture(e.pointerId)}catch(_){};pick(e)});
  seg.addEventListener("pointermove",e=>{if(drag)pick(e)});
  const end=()=>{drag=false;seg.classList.remove("drag")};
  seg.addEventListener("pointerup",end);seg.addEventListener("pointercancel",end);
}
async function confirmPos(code,q,c){const qty=prompt(`按同花顺里的实际持仓填写 ${code} 的股数（0 = 实际没有持仓）`,q);if(qty===null)return;
  let cost=0;if(+qty>0){cost=prompt("成本价",c);if(cost===null)return}await api(`/api/positions/${code}/confirm`,{method:"POST",body:{quantity:+qty,avg_cost:+cost}});toast("已确认");await posChanged()}
async function soldPos(code,q){const qty=prompt(`在同花顺确认已卖出的股数`,q);if(qty===null)return;const px=prompt("成交均价（可空）","");
  await api(`/api/positions/${code}/sold`,{method:"POST",body:{quantity:+qty,price:px?+px:null}});toast("已登记");await posChanged()}
function curve(pts){if(pts.length<2)return'<div class="empty">至少两个交易日的数据才画曲线（当前 '+pts.length+' 个点）</div>';
  const W=460,H=160,P=46,ys=pts.map(p=>p[1]),lo=Math.min(...ys),hi=Math.max(...ys),r=hi-lo||1,base=ys[0];
  const xy=pts.map((p,i)=>[P+i*(W-P-8)/(pts.length-1),H-20-(p[1]-lo)/r*(H-36)]);const up=ys.at(-1)>=base,col=up?"var(--up)":"var(--down)";
  const by=H-20-(base-lo)/r*(H-36),gid="g"+Math.random().toString(36).slice(2,8),line=xy.map(p=>p.map(v=>v.toFixed(1)).join(",")).join(" ");
  return `<svg viewBox="0 0 ${W} ${H}" width="100%"><defs><linearGradient id="${gid}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" style="stop-color:${col};stop-opacity:.28"/><stop offset="1" style="stop-color:${col};stop-opacity:0"/></linearGradient></defs>
   <line x1="${P}" x2="${W-8}" y1="${by.toFixed(1)}" y2="${by.toFixed(1)}" style="stroke:var(--line2);stroke-dasharray:3 3"/>
   <polygon points="${P},${H-20} ${line} ${W-8},${H-20}" fill="url(#${gid})"/>
   <polyline fill="none" style="stroke:${col}" stroke-width="2" stroke-linejoin="round" points="${line}"/>
   <text x="2" y="14">${Math.round(hi)}</text><text x="2" y="${H-20}">${Math.round(lo)}</text><text x="${P}" y="${H-4}">${pts[0][0]}</text><text x="${W-78}" y="${H-4}">${pts[pts.length-1][0]}</text></svg>`}

