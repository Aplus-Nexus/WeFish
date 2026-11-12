const line = document.getElementById('mini-line');
const progress = document.getElementById('mini-progress');

window.wefish.onMiniUpdate((value) => {
  const text = String(value?.text || '').replace(/[\t\n\r ]+/g, ' ').trim();
  line.textContent = text;
  const lineProgress = value?.showLineProgress === false ? '' : String(value?.lineProgress || '');
  const percent = value?.showPercent === false ? '' : `${Math.round(Number(value?.percent || 0))}%`;
  progress.textContent = [lineProgress, percent].filter(Boolean).join(' · ');
  document.body.dataset.widthMode = String(value?.widthMode || 'dynamic');
});

window.wefish.miniReady();
