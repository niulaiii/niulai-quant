/* ---------- 实盘：自动获取持仓开关 + 手动持仓 ---------- */
async function setPosAuto(on){await api("/api/positions_mode",{method:"POST",body:{auto:on}});toast(on?"已打开自动获取持仓，正在读取最新数据":"已关闭自动获取持仓，改为手动填写");
  delete CACHE["/api/account"];delete CACHE["/api/broker"];try{localStorage.removeItem("qf:/api/account");localStorage.removeItem("qf:/api/broker")}catch(e){}if(!on)BK=null;render();if(on)pollSync()}
async function manSave(b){const c=String(PF.code||"").trim();if(!/^\d{6}$/.test(c)){toast("代码应为 6 位数字",true);return}if(!(+PF.qty>0)){toast("数量要大于 0",true);return}
  b.disabled=true;try{await api("/api/positions/manual",{method:"POST",body:{code:c,name:String(PF.name||"").trim(),quantity:+PF.qty,avg_cost:+PF.cost||0,sellable:+(PF.sell||0)}});toast(`已保存 ${c}`);PF={code:"",name:"",qty:"",cost:"",sell:"",edit:false};await reloadAcct(true)}finally{b.disabled=false}}
async function manDel(c){if(!confirm(`删除 ${c} 的持仓记录？`))return;await api("/api/positions/manual/"+c,{method:"DELETE"});toast(`已删除 ${c}`);await reloadAcct(true)}
async function toggleTrading(on){
  if(on&&ST&&ST.mode==="live"&&!confirm("当前是实盘，打开后到点会自动发真实委托。确定打开？"))return;
  if(!on&&!confirm("关闭自动交易？\n不再自动发委托，并撤掉本系统今天还没成交的自动委托（手动下的单不撤）。"))return;
  const r=await api("/api/trading",{method:"POST",timeout:30000,body:{on}});const n=((r&&r.cancelled)||[]).length;
  toast(on?"自动交易已打开":`自动交易已关闭${n?`，已撤 ${n} 笔未成交的自动委托`:""}`);render()}
