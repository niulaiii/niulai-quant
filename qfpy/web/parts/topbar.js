/* ---------- 顶栏：唯一的自动交易开关（每页都在） ---------- */
function drawTopCtl(s){const el=$("#tbctl");if(!el||!s)return;const on=!!s.trading_on;
  morph(el,`<span class="tbsw ${on?"on":""}" id="tradesw" title="${on?"到点按策略自动发买卖委托。关闭：不再自动发委托，并撤掉本系统今天还没成交的自动委托":"不自动发委托：照常选股、推送，只记录信号。手动下单不受影响"}"><button class="switch sm ${on?"on":""}" id="autosw" aria-label="自动交易" onclick="toggleTrading(${!on})"></button><b>自动交易 ${on?'<span class="down">开</span>':'<span class="mute">关</span>'}</b></span>`)}
