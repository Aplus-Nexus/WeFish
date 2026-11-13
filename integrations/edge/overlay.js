(() => {
  const existing = document.getElementById('wefish-edge-overlay');
  if (existing) {
    existing.remove();
    return;
  }
  const host = document.createElement('div');
  host.id = 'wefish-edge-overlay';
  host.style.cssText = 'position:fixed;top:14px;right:18px;z-index:2147483647;width:360px;';
  const shadow = host.attachShadow({ mode: 'open' });
  shadow.innerHTML = `
    <style>
      *{box-sizing:border-box} .capsule{display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid rgba(238,231,215,.2);border-radius:12px;background:rgba(16,20,19,.95);box-shadow:0 10px 30px rgba(0,0,0,.3);color:#eee7d7;font:12px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.dot{flex:0 0 auto;width:7px;height:7px;border-radius:50%;background:#b8f27c;box-shadow:0 0 0 4px rgba(184,242,124,.12)}.text{min-width:0;flex:1;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;font:15px/1.35 Georgia,"Songti SC",serif}.meta{flex:0 0 auto;color:#b8f27c;font:10px ui-monospace,SFMono-Regular,Menlo,monospace;white-space:nowrap}.close{border:0;background:transparent;color:#aab4ad;font-size:16px;cursor:pointer}.close:hover{color:#fff}
    </style>
    <div class="capsule" role="status" aria-live="polite"><span class="dot"></span><span class="text">WeFish 未连接</span><span class="meta">—</span><button class="close" title="关闭浮层">×</button></div>`;
  const text = shadow.querySelector('.text');
  const meta = shadow.querySelector('.meta');
  shadow.querySelector('.close').addEventListener('click', () => host.remove());
  chrome.runtime.onMessage.addListener((message) => {
    if (message?.type !== 'wefish-state') return;
    const state = message.state || {};
    text.textContent = state.text || '暂无当前行';
    meta.textContent = [state.showLineProgress === false ? '' : state.lineProgress, state.showPercent === false ? '' : `${Math.round(Number(state.percent || 0))}%`].filter(Boolean).join(' · ') || '—';
  });
  chrome.runtime.sendMessage({ type: 'get-state' }).then((state) => {
    text.textContent = state?.text || '暂无当前行';
    meta.textContent = state?.lineProgress || '—';
  }).catch(() => {});
  setInterval(() => {
    chrome.runtime.sendMessage({ type: 'get-state' }).then((state) => {
      if (!state) return;
      text.textContent = state.text || '暂无当前行';
      meta.textContent = [state.showLineProgress === false ? '' : state.lineProgress, state.showPercent === false ? '' : `${Math.round(Number(state.percent || 0))}%`].filter(Boolean).join(' · ') || '—';
    }).catch(() => {});
  }, 1000);
  document.documentElement.appendChild(host);
})();
