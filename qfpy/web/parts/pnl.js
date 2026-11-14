/* ---------------- 持仓与收益 ---------------- */
async function account(m){const my=SEQ;const a0=cget("/api/account");
  const f=await show(["/api/account","/api/sell/preview"],(a,pv)=>drawAccount(m,a,pv));if(!f||my!==SEQ)return;
  pollPreview();acctPoll(my);const b=f[0].broker;if(b&&b.sync&&b.sync.running)pollSync()}
