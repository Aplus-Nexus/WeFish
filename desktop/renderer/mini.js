const line = document.getElementById('mini-line');
const progress = document.getElementById('mini-progress');
const ringWrap = document.getElementById('mini-ring-wrap');
const ring = document.getElementById('mini-ring-value');
const ringPercent = document.getElementById('mini-ring-percent');

window.wefish.onMiniUpdate((value) => {
  const text = String(value?.text || '').replace(/[\t\n\r ]+/g, ' ').trim();
  line.textContent = text;
  const lineProgress = value?.showLineProgress === false ? '' : String(value?.lineProgress || '');
  const percentValue = Math.max(0, Math.min(100, Number(value?.percent || 0)));
  const percent = value?.showPercent === false ? '' : `${Math.round(percentValue)}%`;
  progress.textContent = [lineProgress, percent].filter(Boolean).join(' · ');
  const circumference = 2 * Math.PI * 10;
  if (ring) {
    ring.style.strokeDasharray = String(circumference);
    ring.style.strokeDashoffset = String(circumference * (1 - percentValue / 100));
    ring.setAttribute('aria-valuenow', String(Math.round(percentValue)));
  }
  if (ringPercent) ringPercent.textContent = `${Math.round(percentValue)}%`;
  if (ringWrap) ringWrap.hidden = value?.showPercent === false;
  document.body.dataset.widthMode = String(value?.widthMode || 'dynamic');
});

window.wefish.miniReady();
