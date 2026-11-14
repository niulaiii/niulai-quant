/* ---------- 设置：下单程序 ---------- */
async function testXd(b){const ae=document.activeElement;if(ae&&ae.blur)ae.blur();b.disabled=true;const t=b.textContent;b.textContent="测试中…";const el=$("#tx_live");
  try{const r=await api("/api/test/xiadan",{method:"POST",timeout:30000,body:{live:JSON.parse(JSON.stringify(W.live))}});if(el)morph(el,checksHtml(r));toast(`下单程序：${r.ok?"连接和口令都正常":"有问题，看下方明细"}（用的是表单里当前的值，未保存）`,!r.ok);loadDiag()}
  catch(e){if(el)el.innerHTML=`<span class="pill red">✗</span> <span class="small up">${esc(e.message)}</span>`}finally{b.disabled=false;b.textContent=t}}
/* 设置 → 下单程序：地址、口令、测试连接、连接诊断都在这一处 */
function cfgLive(b){const to=W.live.timeouts||{};
  b.innerHTML=`<div class="card" id="xdcard"><h3>下单程序连接 <span class="r"><button class="btn small primary" id="xdtest" onclick="testXd(this)">测试连接</button></span></h3>
   ${kv([["地址",inp("live.base_url","text","下单程序所在电脑的地址，如 http://127.0.0.1:8765")],["口令（X-Auth）",inp("live.auth_password","text","要和下单程序那边完全一致。已遮蔽，不改就保持原值；环境变量 "+(W.live.auth_env||"QF_XIADAN_PASSWORD")+" 优先")],["默认超时（秒）",inp("live.timeout_secs")]])}
   <div id="tx_live" class="small" style="margin-top:6px"></div><div class="small mute">「测试连接」用表单里当前的地址、口令、超时，不用先保存：先查连接状态，再读一次只读的缓存快照来验证口令。状态能连上、读数据却报 403，就是口令和下单程序那边不一致。</div>
   <div class="subh">连接诊断 <button class="btn small ghost" onclick="loadDiag(true)">刷新</button></div><div id="xddiag">${diagHtml(cget("/api/broker/diag"))}</div></div>
   <div class="card"><h3>后台监控与对账 <span class="r small mute">自动获取持仓打开时才读券商数据</span></h3>${kv([["后台监控",inp("live.monitor","bool","交易时段定时查状态、读快照、对账")],["查状态间隔（秒）",inp("live.poll_secs")],["非交易时段间隔（秒）",inp("live.idle_poll_secs")],["读快照对账间隔（秒）",inp("live.reconcile_secs")],["有在途委托时强制刷新间隔（秒）",inp("live.full_refresh_secs")],
     ["找不到委托的宽限（秒）",inp("live.reconcile.not_found_grace_secs")],["连续几次对不上才标「结果未知」",inp("live.reconcile.mismatch_confirmations")],["两次对账最小间隔（秒）",inp("live.reconcile.mismatch_min_gap_secs")],["采用券商成本",inp("live.reconcile.adopt_broker_cost","bool")]])}</div>
   <details class="card"><summary><b>各接口超时（秒）</b></summary>${kv(Object.keys(to).map(k=>[k,inp("live.timeouts."+k,"number",{refresh:"切页可能要 12~17 秒",requery:"会激活同花顺窗口",order:"超时不重试，标人工核对"}[k]||"")]))}</details>`;
  loadDiag()}
