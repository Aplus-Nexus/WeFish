const line = document.getElementById('mini-line');
const progress = document.getElementById('mini-progress');

function progressRingGlyph(value) {
  const percent = Math.max(0, Math.min(100, Number(value) || 0));
  if (percent >= 99.5) return '●';
  if (percent >= 75) return '◕';
  if (percent >= 50) return '◑';
  if (percent >= 25) return '◔';
  return '○';
}

window.wefish.onMiniUpdate((value) => {
  const text = String(value?.text || '').replace(/[\t\n\r ]+/g, ' ').trim();
  line.textContent = text;
  const lineProgress = value?.showLineProgress === false ? '' : String(value?.lineProgress || '');
  const percent = value?.showPercent === false ? '' : `${Math.round(Number(value?.percent || 0))}% ${progressRingGlyph(value?.percent)}`;
  progress.textContent = [lineProgress, percent].filter(Boolean).join(' · ');
  document.body.dataset.widthMode = String(value?.widthMode || 'dynamic');
});

window.wefish.miniReady();
