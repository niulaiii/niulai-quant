/* ---------- 漏斗条形图 ---------- */
function funnelBars(r){const st=r.stages||[];if(!st.length)return "";const top=Math.max(1,...st.map(s=>s.input));
  const rows=st.map(s=>({lab:s.label,ix:s.index,n:s.type==="pool"?s.kept.length:s.kept.length,inp:s.input,drop:s.dropped.length}));
  return `<div class="fun">${rows.map((x,i)=>`<div class="st ${i===rows.length-1?"pick":""}"><div class="lab" title="${esc(x.lab)}"><span class="ix">${esc(x.ix)}</span>${esc(x.lab)}</div><div class="trk"><i style="width:${Math.max(1.5,x.n/top*100)}%"></i><b>${x.n}</b></div><div class="drop">${x.drop?`淘汰 ${x.drop}`:"—"}</div></div>`).join("")}
   <div class="st pick"><div class="lab"><span class="ix">★</span>入选</div><div class="trk"><i style="width:${Math.max(1.5,r.picks.length/top*100)}%"></i><b>${r.picks.length}</b></div><div class="drop num">${esc(r.picks.map(pcode).join(" "))}</div></div></div>`}

